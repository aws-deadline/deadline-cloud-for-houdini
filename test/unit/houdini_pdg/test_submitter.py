# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the PDG submitter: the job template, settings, parameter values, attachments, and submission."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from configparser import ConfigParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type

import jsonschema
import pytest
import yaml
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from openjd.model import (
    ParameterValue,
    ParameterValueType,
    StepParameterSpaceIterator,
    create_job,
    decode_job_template,
)

import deadline.houdini_adaptor
from deadline.client.config import config_file
from deadline.client.job_bundle.submission import AssetReferences
from deadline.houdini_pdg import submitter as submitter_module
from deadline.houdini_pdg.errors import (
    CookSetupError,
    JobSubmissionError,
    PDGSchedulerError,
    QuotaExceededError,
)
from deadline.houdini_pdg.graph import DeclaredFile, GraphSnapshot, StepSpec, WorkItemSpec
from deadline.houdini_pdg.ports import SceneFiles
from deadline.houdini_pdg.submitter import PdgSubmitter, PdgSubmitterSettings

WORKING_DIR: str = "/proj/pdgtemp"
# Made-up resource IDs in the service's shape: a prefix and 32 hex digits.
_HEX32: str = "0123456789abcdef" * 2
FARM: str = f"farm-{_HEX32}"
QUEUE: str = f"queue-{_HEX32}"
COOK_ID: str = "c0ffee00c0ffee00c0ffee00c0ffee00"
INT32_MAX: int = 2**31 - 1

# Queue parameter definitions as a node stores them after a queue parameter refresh.
CONDA_DEFINITIONS: List[Dict[str, Any]] = [
    {"name": "CondaPackages", "type": "STRING", "default": ""},
    {"name": "CondaChannels", "type": "STRING", "default": "deadline-cloud"},
]


class FakeParm:
    """A parm that returns one fixed value, as Houdini's ``eval`` and ``evalAsString`` do."""

    def __init__(self, value: Any) -> None:
        self._value: Any = value

    def eval(self) -> Any:
        """The value as stored."""
        return self._value

    def evalAsString(self) -> str:
        """The value as a string."""
        return str(self._value)


class FakeNode:
    """A scheduler node with only the parms and user data it's given.

    When ``allowed`` is given, every read fails unless ``allowed[0]`` is true.
    """

    def __init__(
        self,
        parms: Optional[Dict[str, Any]] = None,
        user_data: Optional[Dict[str, str]] = None,
        allowed: Optional[List[bool]] = None,
    ) -> None:
        self._parms: Dict[str, Any] = parms or {}
        self._user_data: Dict[str, str] = user_data or {}
        self._allowed: Optional[List[bool]] = allowed

    def parm(self, name: str) -> Optional[FakeParm]:
        """The named parm, or None when the node doesn't declare it."""
        self._check()
        return FakeParm(self._parms[name]) if name in self._parms else None

    def userData(self, name: str) -> Optional[str]:
        """The named user data, or None."""
        self._check()
        return self._user_data.get(name)

    def _check(self) -> None:
        # A read outside the allowed window would run off Houdini's main thread.
        if self._allowed is not None and not self._allowed[0]:
            raise AssertionError("The node was read outside read_node.")


class FakeCreateJob:
    """Records each call, then returns ``result`` or raises ``error``."""

    def __init__(self, result: Optional[str] = f"job-{_HEX32}") -> None:
        self.result: Optional[str] = result
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.error: Optional[BaseException] = None

    def __call__(self, bundle_dir: str, **kwargs: Any) -> Optional[str]:
        self.calls.append((bundle_dir, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class CodedError(Exception):
    """An error that carries a service code only in its response, as a botocore error can."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.response: Dict[str, Any] = {"Error": {"Code": code}}


class FakeDeadline:
    """A Deadline Cloud client whose ``get_queue`` returns the given job attachments settings."""

    def __init__(self, attachments: Optional[Dict[str, str]]) -> None:
        self._attachments: Optional[Dict[str, str]] = attachments

    def get_queue(self, farmId: str, queueId: str) -> Dict[str, Any]:
        """The queue, with its job attachments settings when it has any."""
        queue: Dict[str, Any] = {"queueId": queueId}
        if self._attachments is not None:
            queue["jobAttachmentSettings"] = self._attachments
        return queue


def workstation(config: Optional[Dict[str, str]] = None) -> Callable[[str], str]:
    """A workstation setting reader with the test farm and queue, plus ``config``."""
    values: Dict[str, str] = {
        "defaults.farm_id": FARM,
        "defaults.queue_id": QUEUE,
        **(config or {}),
    }
    return lambda name: values.get(name, "")


def item(
    name: str,
    index: int,
    *,
    outputs: Tuple[str, ...] = (),
    inputs: Tuple[str, ...] = (),
) -> WorkItemSpec:
    """A work item that declares the given output and input paths."""
    return WorkItemSpec(
        name=name,
        task_index=index,
        pdg_index=index,
        item_id=index,
        node_name=name.rsplit("_", 1)[0],
        declared_outputs=tuple(DeclaredFile(p, "file") for p in outputs),
        declared_inputs=tuple(DeclaredFile(p, "file") for p in inputs),
    )


def step(name: str, count: int, deps: Tuple[str, ...] = (), **item_kwargs: Any) -> StepSpec:
    """A step of ``count`` items that all declare the same paths."""
    return StepSpec(
        name=name,
        dependencies=deps,
        items=tuple(item(f"{name}_{i}", i, **item_kwargs) for i in range(count)),
    )


def make_submitter(
    snapshot: Optional[GraphSnapshot] = None,
    *,
    node: Optional[FakeNode] = None,
    create_job: Optional[FakeCreateJob] = None,
    scene: Optional[SceneFiles] = None,
    files: Optional[Set[str]] = None,
    log: Optional[List[str]] = None,
    get_setting: Optional[Callable[[str], str]] = None,
    queue_values: Optional[List[Dict[str, Any]]] = None,
    job_attachments: Optional[Callable[[str, str], Tuple[str, str]]] = None,
) -> PdgSubmitter:
    """A submitter whose workstation, scene, file system, queue, and Houdini reads are all fakes."""
    # By default nothing is on disk and the node has no queue parameter values. The file system
    # holds native paths, as the submitter normalizes what it looks up.
    lines: List[str] = log if log is not None else []
    existing: Set[str] = {os.path.normpath(path) for path in files or set()}
    values: List[Dict[str, Any]] = queue_values or []
    return PdgSubmitter(
        node=node or FakeNode(),
        working_dir=WORKING_DIR,
        snapshot=snapshot,
        create_job=create_job or FakeCreateJob(),
        log=lines.append,
        scene_files=lambda: scene or SceneFiles(hip_file="/proj/scene.hip"),
        file_exists=lambda path: path in existing,
        job_attachments=job_attachments or (lambda farm, queue: ("bucket-a", "DeadlineCloud")),
        cook_id=COOK_ID,
        get_setting=get_setting or workstation(),
        houdini_version=lambda: "21.0.631",
        queue_parameter_values=lambda node: values,
    )


@st.composite
def snapshots(
    draw: Any, max_steps: int = 6, max_tasks: int = 4, large: bool = False
) -> GraphSnapshot:
    """Any valid snapshot. With ``large``, a step may have more than 1,024 tasks."""
    count: int = draw(st.integers(1, max_steps))
    names: List[str] = [f"node{i}" for i in range(count)]
    steps: List[StepSpec] = []
    for i, name in enumerate(names):
        # Dependencies come only from earlier names, so the order is valid.
        deps: Tuple[str, ...] = tuple(d for d in names[:i] if draw(st.booleans()))
        tasks: st.SearchStrategy[int] = st.integers(1, max_tasks)
        n: int = draw(st.one_of(tasks, st.integers(1024, 1030)) if large else tasks)
        outputs: Tuple[str, ...] = (
            (f"__PDG_DIR__/{name}/out.bgeo.sc",) if draw(st.booleans()) else ()
        )
        steps.append(step(name, n, deps, outputs=outputs))
    return GraphSnapshot(steps=tuple(steps))


def template_for(snapshot: GraphSnapshot, **kwargs: Any) -> Dict[str, Any]:
    """The job template a submitter builds for ``snapshot``."""
    sub: PdgSubmitter = make_submitter(snapshot, **kwargs)
    return sub.get_job_template(sub.get_settings())


def run_data(step_template: Dict[str, Any]) -> Dict[str, Any]:
    """A step's run data, as the worker reads it for task 7."""
    (embedded,) = step_template["script"]["embeddedFiles"]
    return yaml.safe_load(embedded["data"].replace("{{Task.Param.TaskIndex}}", "7"))


def parse_range(expression: str) -> range:
    """The task indexes an OpenJD ``"a-b"`` range names."""
    start, end = expression.split("-")
    return range(int(start), int(end) + 1)


def read_bundle(bundle_dir: Path) -> Dict[str, Any]:
    """The three bundle files, each loaded with ``safe_load``."""
    return {
        name: yaml.safe_load((bundle_dir / f"{name}.yaml").read_text(encoding="utf8"))
        for name in ("template", "parameter_values", "asset_references")
    }


def values_by_name(values: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Parameter values as a name-to-value map."""
    return {v["name"]: v["value"] for v in values}


# ---------------------------------------------------------------- template


@settings(max_examples=40, deadline=None)
@given(snapshots(large=True))
def test_the_template_has_one_step_per_snapshot_step_with_its_dependencies_and_range(
    snapshot: GraphSnapshot,
) -> None:
    """Each snapshot step must become one step with its dependencies and a range of all its tasks.

    A list range would cap a step at 1,024 tasks, and a wrong range would drop or invent tasks.
    """
    template: Dict[str, Any] = template_for(snapshot)
    steps: List[Dict[str, Any]] = template["steps"]
    rendered_names: List[str] = [s["name"] for s in steps]

    # Same names, in snapshot order, unique.
    assert rendered_names == list(snapshot.step_names)
    assert len(set(rendered_names)) == len(rendered_names)
    counted: int = 0
    for position, (spec, rendered) in enumerate(zip(snapshot.steps, steps)):
        keys: List[str] = list(rendered)
        # Dependencies are present iff the step has some, come before the parameter space,
        # and each names an earlier step.
        if spec.dependencies:
            assert rendered["dependencies"] == [{"dependsOn": d} for d in spec.dependencies]
            assert keys.index("dependencies") < keys.index("parameterSpace")
            assert all(rendered_names.index(d) < position for d in spec.dependencies)
        else:
            assert "dependencies" not in rendered
        # One INT range parameter that names exactly the step's task indexes.
        (task_param,) = rendered["parameterSpace"]["taskParameterDefinitions"]
        assert task_param["name"] == "TaskIndex" and task_param["type"] == "INT"
        assert isinstance(task_param["range"], str)
        assert parse_range(task_param["range"]) == range(spec.task_count)
        counted += len(parse_range(task_param["range"]))
        assert "stepEnvironments" not in rendered
    assert counted == snapshot.total_tasks
    decode_job_template(template=template)


@settings(max_examples=40, deadline=None)
@given(snapshots(), st.booleans(), st.booleans())
def test_every_parameter_the_template_uses_is_declared_and_has_a_value(
    tmp_path_factory: pytest.TempPathFactory,
    snapshot: GraphSnapshot,
    wheels: bool,
    stored_definitions: bool,
) -> None:
    """CreateJob refuses a template that uses an undeclared parameter, or one with no value."""
    # Build with and without the wheels override, and with and without stored definitions.
    user_data: Dict[str, str] = (
        {"queue_parameter_definitions": json.dumps(CONDA_DEFINITIONS)} if stored_definitions else {}
    )
    sub: PdgSubmitter = make_submitter(snapshot, node=FakeNode(user_data=user_data))
    settings_: PdgSubmitterSettings = sub.get_settings()
    if wheels:
        settings_.include_adaptor_wheels = True
        settings_.adaptor_wheels = str(tmp_path_factory.mktemp("wheels"))
    template: Dict[str, Any] = sub.get_job_template(settings_)
    values: List[Dict[str, Any]] = sub.get_parameter_values(settings_, [])

    declared: Dict[str, Dict[str, Any]] = {d["name"]: d for d in template["parameterDefinitions"]}
    used: Set[str] = set(re.findall(r"\{\{\s*Param\.(\w+)\s*\}\}", json.dumps(template)))
    job_values: Dict[str, Any] = {
        v["name"]: v["value"] for v in values if not v["name"].startswith("deadline:")
    }
    assert used <= set(declared)
    assert set(job_values) <= set(declared)
    for name, definition in declared.items():
        assert "default" in definition or name in job_values


@settings(max_examples=25, deadline=None)
@given(snapshots(max_steps=4, max_tasks=6))
def test_the_decoded_job_has_one_task_per_work_item(snapshot: GraphSnapshot) -> None:
    """OpenJD must expand the template to exactly one task per work item, or items go missing."""
    template: Dict[str, Any] = template_for(snapshot)
    # Give every parameter a value of its type, then let OpenJD build the job.
    values: Dict[str, ParameterValue] = {}
    for definition in template["parameterDefinitions"]:
        kind: ParameterValueType = ParameterValueType(definition["type"])
        values[definition["name"]] = ParameterValue(
            type=kind, value="/x" if kind.value == "PATH" else "v"
        )
    job: Any = create_job(
        job_template=decode_job_template(template=template), job_parameter_values=values
    )
    counted: int = sum(len(StepParameterSpaceIterator(space=s.parameterSpace)) for s in job.steps)
    assert counted == snapshot.total_tasks


@settings(max_examples=40, deadline=None)
@given(snapshots())
def test_every_step_runs_the_same_command_with_its_own_name_and_record_flag(
    snapshot: GraphSnapshot,
) -> None:
    """The worker learns which step it runs, and whether to record outputs, only from run data.

    So run data must hold the step's name and the flag, and nothing else may differ between steps.
    """
    # Each step's run data is its name and flag, and every step's command is the same.
    steps: List[Dict[str, Any]] = template_for(snapshot)["steps"]
    for spec, rendered in zip(snapshot.steps, steps):
        assert run_data(rendered) == {
            "pdg": {"step": spec.name, "task_index": 7, "record_outputs": not spec.declares_outputs}
        }
        assert rendered["script"]["actions"] == steps[0]["script"]["actions"]
    assert steps[0]["script"]["actions"]["onRun"]["command"] == "houdini-openjd"
    assert steps[0]["script"]["actions"]["onRun"]["args"] == [
        "daemon",
        "run",
        "--connection-file",
        "{{Session.WorkingDirectory}}/connection.json",
        "--run-data",
        "file://{{Task.File.runData}}",
    ]


def test_one_session_environment_runs_the_daemon_with_the_scene_and_bucket() -> None:
    """One daemon per session serves every task, so it needs the scene, the version, and the bucket."""
    template: Dict[str, Any] = template_for(GraphSnapshot(steps=(step("a", 1),)))
    (environment,) = template["jobEnvironments"]
    assert environment["name"] == "PdgSession"
    actions: Dict[str, Any] = environment["script"]["actions"]
    assert actions["onEnter"]["args"][:2] == ["daemon", "start"]
    # Without the rules file, the worker can't remap the scene's paths to its own.
    assert actions["onEnter"]["args"][2:4] == [
        "--path-mapping-rules",
        "file://{{Session.PathMappingRulesFile}}",
    ]
    assert actions["onExit"]["args"][:2] == ["daemon", "stop"]
    # The init data the adaptor reads when the daemon starts.
    (init_file,) = environment["script"]["embeddedFiles"]
    assert yaml.safe_load(init_file["data"]) == {
        "scene_file": "{{Param.HipFile}}",
        "version": "21.0.631",
        "pdg": {
            "pdg_dir": "{{Param.PdgDir}}",
            "bucket": "{{Param.JaBucket}}",
            "root_prefix": "{{Param.JaRootPrefix}}",
        },
    }
    assert all(line == line.rstrip() for line in init_file["data"].splitlines())


def test_the_session_and_task_data_satisfy_the_worker_schemas() -> None:
    """The adaptor validates its init and run data first, so data it refuses fails every PDG task.

    PDG data has no ``render_node`` or ``frame_range``, so only ROP data may need them.
    """
    schemas: Path = Path(deadline.houdini_adaptor.__file__).parent / "HoudiniAdaptor" / "schemas"
    template: Dict[str, Any] = template_for(GraphSnapshot(steps=(step("a", 2),)))
    # The session's init data, with each parameter reference turned into a non-empty value,
    # as it would be after the job starts.
    (session,) = [e for e in template["jobEnvironments"] if e["name"] == "PdgSession"]
    (init_file,) = session["script"]["embeddedFiles"]
    init_text: str = init_file["data"].replace("{{Param.", "value_of_").replace("}}", "")
    init_schema: Dict[str, Any] = json.loads(
        (schemas / "init_data.schema.json").read_text(encoding="utf8")
    )
    jsonschema.validate(yaml.safe_load(init_text), init_schema)
    # The daemon's front end adds the task's IDs, which it reads from the session environment.
    task_data: Dict[str, Any] = run_data(template["steps"][0])
    task_data["pdg"].update(
        farm_id=FARM, queue_id=QUEUE, job_id="job-a", step_id="step-a", task_id="task-a-0"
    )
    run_schema: Dict[str, Any] = json.loads(
        (schemas / "run_data.schema.json").read_text(encoding="utf8")
    )
    jsonschema.validate(task_data, run_schema)


@pytest.mark.parametrize("include", [True, False])
@pytest.mark.parametrize("exists", [True, False])
def test_the_wheels_override_follows_the_rop_rule(
    tmp_path: Path, include: bool, exists: bool
) -> None:
    """The override applies iff the parm is on and the wheels folder exists, as for the ROP.

    It must run before the session's daemon starts, and its folder must be a known path.
    """
    # Submit with the node's wheels parms set to this case.
    wheels: Path = tmp_path / "wheels"
    if exists:
        wheels.mkdir()
    node: FakeNode = FakeNode(
        {"include_adaptor_wheels": int(include), "adaptor_wheels": str(wheels)}
    )
    create: FakeCreateJob = FakeCreateJob()
    make_submitter(node=node, create_job=create).submit(
        GraphSnapshot(steps=(step("a", 1),)), str(tmp_path / "bundle")
    )

    enabled: bool = include and exists
    bundle: Dict[str, Any] = read_bundle(tmp_path / "bundle")
    environments: List[str] = [e["name"] for e in bundle["template"]["jobEnvironments"]]
    values: Dict[str, Any] = values_by_name(bundle["parameter_values"]["parameterValues"])
    declared: List[str] = [d["name"] for d in bundle["template"]["parameterDefinitions"]]
    assert environments == (["OverrideAdaptor", "PdgSession"] if enabled else ["PdgSession"])
    assert values.get("AdaptorWheels") == (str(wheels) if enabled else None)
    assert ("AdaptorWheels" in declared) == ("OverrideAdaptorName" in declared) == enabled
    assert (str(wheels) in create.calls[0][1]["known_asset_paths"]) == enabled
    decode_job_template(template=bundle["template"])


def test_the_node_queue_parameters_are_declared_first_and_valued() -> None:
    """The Conda packages come from the node's queue parameters, so they must reach the job."""
    # The node stored the Conda definitions, and its queue parameter values name packages.
    node: FakeNode = FakeNode(
        user_data={"queue_parameter_definitions": json.dumps(CONDA_DEFINITIONS)}
    )
    sub: PdgSubmitter = make_submitter(
        GraphSnapshot(steps=(step("a", 1),)),
        node=node,
        queue_values=[{"name": "CondaPackages", "value": "houdini=21.0.* houdini-openjd=0.7.*"}],
    )
    settings_: PdgSubmitterSettings = sub.get_settings()
    names: List[str] = [d["name"] for d in sub.get_job_template(settings_)["parameterDefinitions"]]
    assert names[:2] == ["CondaPackages", "CondaChannels"]
    values: Dict[str, Any] = values_by_name(sub.get_parameter_values(settings_, []))
    assert values["CondaPackages"] == "houdini=21.0.* houdini-openjd=0.7.*"


def test_the_description_reaches_the_template() -> None:
    """The node's description is the job's description in the monitor."""
    node: FakeNode = FakeNode({"description": "smoke and fire"})
    template: Dict[str, Any] = template_for(GraphSnapshot(steps=(step("a", 1),)), node=node)
    assert template["description"] == "smoke and fire"


# The type fields of each parameter every PDG job declares.
_PDG_PARAMETER_TYPES: Dict[str, Dict[str, str]] = {
    "CookName": {"type": "STRING"},
    "PdgCookId": {"type": "STRING"},
    "JaBucket": {"type": "STRING"},
    "JaRootPrefix": {"type": "STRING"},
    "HipFile": {"type": "PATH", "objectType": "FILE", "dataFlow": "IN"},
    "PdgDir": {"type": "PATH", "objectType": "DIRECTORY", "dataFlow": "OUT"},
}


def test_the_pdg_parameters_have_their_types() -> None:
    """Job attachments uploads ``PdgDir`` only if it's an output folder, and syncs it downstream.

    The old worker wheels parameter must never appear.
    """
    # Compare each PDG parameter's type fields with the expected ones, by name.
    template: Dict[str, Any] = template_for(GraphSnapshot(steps=(step("a", 1),)))
    declared: Dict[str, Dict[str, Any]] = {d["name"]: d for d in template["parameterDefinitions"]}

    for name, fields in _PDG_PARAMETER_TYPES.items():
        assert {key: declared[name].get(key) for key in fields} == fields
    assert "PdgWorkerWheels" not in declared


class FileAccessProbe:
    """Records file opens and folder listings while armed. An audit hook, so nothing is patched."""

    armed: bool = False
    installed: bool = False
    events: List[str] = []

    @classmethod
    def hook(cls, event: str, args: Tuple[Any, ...]) -> None:
        """Record the event when armed and it touches the file system."""
        if cls.armed and event in ("open", "os.listdir", "os.scandir", "os.mkdir", "os.remove"):
            cls.events.append(event)

    @classmethod
    def watch(cls, build: Callable[[], Any]) -> Tuple[Any, List[str]]:
        """Run ``build``, and return its result and the file system events it caused."""
        # An audit hook can't be removed, so it's added once and armed only here.
        if not cls.installed:
            sys.addaudithook(cls.hook)
            cls.installed = True
        cls.events = []
        cls.armed = True
        try:
            result: Any = build()
        finally:
            cls.armed = False
        return result, list(cls.events)


@settings(max_examples=40, deadline=None)
@given(snapshots())
def test_building_the_template_does_no_io_and_is_deterministic(snapshot: GraphSnapshot) -> None:
    """The template is built on a PDG thread from the snapshot alone.

    A file read there could stall the cook, and could make two builds of one snapshot differ.
    """
    # Build twice with the wheels override off, watching the file system both times.
    sub: PdgSubmitter = make_submitter(snapshot)
    settings_: PdgSubmitterSettings = sub.get_settings()
    first, first_events = FileAccessProbe.watch(lambda: sub.get_job_template(settings_))
    second, second_events = FileAccessProbe.watch(lambda: sub.get_job_template(settings_))
    assert first_events == [] and second_events == []
    assert json.dumps(first) == json.dumps(second)


def test_the_file_access_probe_sees_a_file_read(tmp_path: Path) -> None:
    """The no I/O test is only worth something if the probe can see a read.

    The wheels override loads its environment from a file, so the probe must report it.
    """
    # Turn the override on, then build the template while the probe watches.
    node: FakeNode = FakeNode({"include_adaptor_wheels": 1, "adaptor_wheels": str(tmp_path)})
    sub: PdgSubmitter = make_submitter(GraphSnapshot(steps=(step("a", 1),)), node=node)
    settings_: PdgSubmitterSettings = sub.get_settings()
    _, events = FileAccessProbe.watch(lambda: sub.get_job_template(settings_))
    assert "open" in events


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "name, hip_file, expected",
    [
        ("Bonfire", "/proj/scene.hip", "Bonfire"),
        ("", "/proj/bonfire.hip", "bonfire.hip"),
        ("", "", "PDG cook c0ffee00"),
    ],
)
def test_the_job_name_falls_back_to_the_scene_then_the_cook(
    name: str, hip_file: str, expected: str
) -> None:
    """A job always has a name: the node's, else the scene file's, else the cook's."""
    # Build with this case's name parm and scene file.
    sub: PdgSubmitter = make_submitter(
        GraphSnapshot(steps=(step("a", 1),)),
        node=FakeNode({"name": name}),
        scene=SceneFiles(hip_file=hip_file),
    )
    settings_: PdgSubmitterSettings = sub.get_settings()
    assert settings_.job_name == expected
    assert sub.get_job_template(settings_)["name"] == "{{Param.CookName}}"
    assert values_by_name(sub.get_parameter_values(settings_, []))["CookName"] == expected


def test_the_job_always_starts_suspended() -> None:
    """A task released before its payload is stored would run without it, so the parm is ignored."""
    settings_: PdgSubmitterSettings = make_submitter(
        node=FakeNode({"initial_status": "READY"})
    ).get_settings()
    assert settings_.initial_status == "SUSPENDED"


def test_a_node_without_job_parms_gets_the_scheduler_defaults_and_the_scene_files() -> None:
    """With no job parms, a failed task fails the cook after one retry, and the scene's files go along."""
    # A node with no parms, over a scene with a file, an asset, and a library folder.
    scene: SceneFiles = SceneFiles(
        hip_file="/proj/bonfire.hip", assets=("/tex/a.png",), directories=("/proj/lib.hda",)
    )
    settings_: PdgSubmitterSettings = make_submitter(scene=scene).get_settings()
    assert settings_.max_retries_per_task == 1
    assert settings_.max_failed_tasks_count == 0
    assert settings_.priority == 50
    assert settings_.hip_file == "/proj/bonfire.hip"
    assert settings_.scene_assets == ["/tex/a.png"]
    assert settings_.input_directories == ["/proj/lib.hda"]
    assert settings_.output_directories == [WORKING_DIR]


# The numeric parms, the settings field each fills, and the largest value the job accepts.
_NUMERIC_PARMS: List[Tuple[str, str, int]] = [
    ("priority", "priority", 100),
    ("failed_tasks_limit", "max_failed_tasks_count", INT32_MAX),
    ("task_retry_limit", "max_retries_per_task", INT32_MAX),
]


@given(st.data())
def test_a_valid_numeric_parm_is_read_as_an_int(data: st.DataObject) -> None:
    """A whole number in range is accepted as an int, whether the parm gives an int, float, or string."""
    # Draw a parm, a whole number in its range or at either end, and a form a parm can give.
    parm, field_name, high = data.draw(st.sampled_from(_NUMERIC_PARMS))
    number: int = data.draw(st.one_of(st.sampled_from([0, high]), st.integers(0, high)))
    value: Any = data.draw(st.sampled_from([number, float(number), f" {number} "]))
    settings_: PdgSubmitterSettings = make_submitter(node=FakeNode({parm: value})).get_settings()
    read: Any = getattr(settings_, field_name)
    assert read == number and type(read) is int


@given(st.data())
def test_an_invalid_numeric_parm_is_named_in_a_setup_error(data: st.DataObject) -> None:
    """A bad priority, retry, or failure limit must stop the cook and say which parm to fix."""
    # Draw a parm and a value it can't carry: just past either end, further out, a bool,
    # text, a fraction, or None.
    parm, _, high = data.draw(st.sampled_from(_NUMERIC_PARMS))
    value: Any = data.draw(
        st.one_of(
            st.sampled_from([-1, high + 1]),
            st.integers(max_value=-1),
            st.integers(min_value=high + 1),
            st.just(True),
            st.text(alphabet="abcxyz.-", min_size=1, max_size=6),
            st.floats(allow_nan=True).filter(lambda f: math.isnan(f) or not f.is_integer()),
            st.none(),
        )
    )
    with pytest.raises(CookSetupError) as raised:
        make_submitter(node=FakeNode({parm: value})).get_settings()
    assert f"'{parm}'" in str(raised.value)


# ---------------------------------------------------------------- parameter values


@settings(max_examples=60, deadline=None)
# Worker counts 1 and 0 sit on each side of "no limit", so they run every time.
@example(50, 1, 0, 1)
@example(50, 1, 0, 0)
@given(
    st.integers(0, 100),
    st.integers(0, INT32_MAX),
    st.integers(0, INT32_MAX),
    st.one_of(st.none(), st.sampled_from([-1, 0, 1]), st.integers(-1, 50)),
)
def test_job_settings_travel_as_typed_deadline_values(
    priority: int, retries: int, failures: int, workers: Optional[int]
) -> None:
    """CreateJob reads these values by type, so they must stay ints through YAML.

    A non-positive worker count means no limit, so it must not be sent.
    """
    sub: PdgSubmitter = make_submitter(GraphSnapshot(steps=(step("a", 1),)))
    # Settings that ask for READY, so the forced SUSPENDED status shows.
    settings_: PdgSubmitterSettings = PdgSubmitterSettings(
        priority=priority,
        max_retries_per_task=retries,
        max_failed_tasks_count=failures,
        initial_status="READY",
    )
    if workers is not None:
        settings_.max_worker_count = workers
    values: Dict[str, Any] = values_by_name(sub.get_parameter_values(settings_, []))

    assert values["deadline:targetTaskRunStatus"] == "SUSPENDED"
    assert values["deadline:priority"] == priority
    assert values["deadline:maxRetriesPerTask"] == retries
    assert values["deadline:maxFailedTasksCount"] == failures
    for name in ("deadline:priority", "deadline:maxRetriesPerTask", "deadline:maxFailedTasksCount"):
        assert type(values[name]) is int
        assert type(yaml.safe_load(yaml.safe_dump(values[name]))) is int
    assert values.get("deadline:maxWorkerCount") == (
        workers if workers is not None and workers > 0 else None
    )


_PARAM: st.SearchStrategy[Dict[str, Any]] = st.fixed_dictionaries(
    {
        "name": st.sampled_from(["CondaPackages", "CondaChannels", "RezPackages", "Extra"]),
        "value": st.one_of(st.none(), st.just(""), st.just(0), st.text(max_size=4)),
    }
)


@settings(max_examples=100, deadline=None)
@given(
    st.lists(_PARAM, unique_by=lambda p: p["name"]),
    st.lists(_PARAM, unique_by=lambda p: p["name"]),
)
def test_a_non_empty_caller_value_wins_the_merge(
    node_values: List[Dict[str, Any]], caller: List[Dict[str, Any]]
) -> None:
    """A caller's value replaces the node's, but an empty one must not blank it.

    CreateJob also refuses a parameter given twice, so every name appears once.
    """
    sub: PdgSubmitter = make_submitter(
        GraphSnapshot(steps=(step("a", 1),)), queue_values=node_values
    )
    merged: List[Dict[str, Any]] = sub.get_parameter_values(sub.get_settings(), caller)
    names: List[str] = [p["name"] for p in merged]
    assert len(names) == len(set(names))
    by_name: Dict[str, Any] = values_by_name(merged)
    node_by_name: Dict[str, Any] = values_by_name(node_values)
    # Check each name against the rule: caller if non-empty, else node, else absent.
    for name in set(node_by_name) | {p["name"] for p in caller}:
        caller_value: Any = next((p["value"] for p in caller if p["name"] == name), None)
        if caller_value not in (None, ""):
            assert by_name[name] == caller_value
        elif name in node_by_name:
            assert by_name[name] == node_by_name[name]
        else:
            assert name not in by_name


@pytest.mark.parametrize(
    "name", ["CookName", "PdgCookId", "JaBucket", "JaRootPrefix", "HipFile", "PdgDir"]
)
def test_a_node_value_named_like_a_pdg_parameter_is_not_sent(name: str) -> None:
    """The template drops a node parameter named like a PDG one, so its value must go too.

    CreateJob refuses a parameter given twice, and the cook's own value must be the one sent.
    """
    # The node's queue parameters hold a value under a PDG parameter's name.
    sub: PdgSubmitter = make_submitter(
        GraphSnapshot(steps=(step("a", 1),)),
        queue_values=[{"name": name, "value": "/from/the/node"}, {"name": "Extra", "value": "x"}],
    )
    values: List[Dict[str, Any]] = sub.get_parameter_values(sub.get_settings(), [])
    names: List[str] = [v["name"] for v in values]
    assert names.count(name) == 1
    assert values_by_name(values)[name] != "/from/the/node"
    assert values_by_name(values)["Extra"] == "x"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("name", ["AdaptorWheels", "OverrideAdaptorName"])
def test_a_node_parameter_named_like_an_override_one_is_dropped(
    tmp_path: Path, name: str, enabled: bool
) -> None:
    """A node parameter named like a wheels override one must not reach the job.

    Kept, it gives CreateJob a duplicate or undeclared value, or makes the override look applied.
    """
    # The node declares and values a queue parameter under an override parameter's name.
    wheels: Path = tmp_path / "wheels"
    wheels.mkdir()
    node: FakeNode = FakeNode(
        {"include_adaptor_wheels": int(enabled), "adaptor_wheels": str(wheels)},
        user_data={"queue_parameter_definitions": json.dumps([{"name": name, "type": "STRING"}])},
    )
    create: FakeCreateJob = FakeCreateJob()
    make_submitter(
        node=node, create_job=create, queue_values=[{"name": name, "value": "/from/the/node"}]
    ).submit(GraphSnapshot(steps=(step("a", 1),)), str(tmp_path / "bundle"))

    # Every value is declared once and sent once, and the override follows the parm alone.
    bundle: Dict[str, Any] = read_bundle(tmp_path / "bundle")
    declared: List[str] = [d["name"] for d in bundle["template"]["parameterDefinitions"]]
    sent: List[Dict[str, Any]] = bundle["parameter_values"]["parameterValues"]
    sent_names: List[str] = [v["name"] for v in sent if not v["name"].startswith("deadline:")]
    environments: List[str] = [e["name"] for e in bundle["template"]["jobEnvironments"]]
    assert len(declared) == len(set(declared))
    assert len(sent_names) == len(set(sent_names))
    assert set(sent_names) <= set(declared)
    assert "/from/the/node" not in values_by_name(sent).values()
    assert environments == (["OverrideAdaptor", "PdgSession"] if enabled else ["PdgSession"])
    assert values_by_name(sent).get("AdaptorWheels") == (str(wheels) if enabled else None)
    decode_job_template(template=bundle["template"])


def test_the_workstation_farm_and_queue_are_read_and_its_config_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The farm and queue come from the workstation's defaults, as for the ROP.

    A cook must never change the artist's configuration file.
    """
    # Point the client library at a configuration file that names the test farm and queue.
    config_path: Path = tmp_path / "config"
    parser: ConfigParser = ConfigParser()
    config_file.set_setting("defaults.farm_id", FARM, config=parser)
    config_file.set_setting("defaults.queue_id", QUEUE, config=parser)
    with open(config_path, "w", encoding="utf8") as f:
        parser.write(f)
    before: bytes = config_path.read_bytes()
    monkeypatch.setenv("DEADLINE_CONFIG_FILE_PATH", str(config_path))
    asked: List[Tuple[str, str]] = []

    def location(farm: str, queue: str) -> Tuple[str, str]:
        asked.append((farm, queue))
        return ("bucket-a", "DeadlineCloud")

    # Submit with the real setting reader.
    create: FakeCreateJob = FakeCreateJob()
    log: List[str] = []
    PdgSubmitter(
        node=FakeNode(),
        working_dir=WORKING_DIR,
        create_job=create,
        log=log.append,
        scene_files=lambda: SceneFiles(hip_file="/proj/scene.hip"),
        file_exists=lambda path: False,
        job_attachments=location,
        houdini_version=lambda: "21.0.631",
        queue_parameter_values=lambda node: [],
    ).submit(GraphSnapshot(steps=(step("a", 1),)), str(tmp_path / "bundle"))

    assert asked == [(FARM, QUEUE)]
    assert len(create.calls) == 1
    assert config_path.read_bytes() == before


# ---------------------------------------------------------------- attachments


def test_attachments_skip_tokens_outputs_missing_files_and_dependent_steps() -> None:
    """Only files the workstation alone can provide are attached.

    A path with an unresolved token, a file a task writes, a missing file, or an input of a step
    with dependencies would ship a stale or broken copy.
    """
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(
            step(
                "pattern",
                1,
                inputs=(
                    "__PDG_DIR__/input/rock.bgeo",  # external, on disk
                    "__PDG_DIR__/input/missing.bgeo",  # not on disk
                    "__PDG_TEMP__/data/item.json",  # unresolved token
                    "__PDG_DIR__/sim/out.bgeo.sc",  # a task writes it
                ),
            ),
            step("sim", 1, outputs=("__PDG_DIR__/sim/out.bgeo.sc",)),
            step("render", 1, ("sim",), inputs=("/elsewhere/tex.png",)),
        )
    )
    files: Set[str] = {
        f"{WORKING_DIR}/input/rock.bgeo",
        f"{WORKING_DIR}/sim/out.bgeo.sc",
        "/elsewhere/tex.png",
    }
    scene: SceneFiles = SceneFiles(
        hip_file="/proj/scene.hip", assets=("/tex/wood.rat",), directories=("/proj/lib.hda",)
    )
    sub: PdgSubmitter = make_submitter(snapshot, scene=scene, files=files)
    refs: AssetReferences = sub.get_asset_references(sub.get_settings())
    assert refs.input_filenames == {
        "/proj/scene.hip",
        "/tex/wood.rat",
        os.path.normpath(f"{WORKING_DIR}/input/rock.bgeo"),
    }
    # A library stored expanded is a directory, and job attachments refuses one listed as a file.
    assert refs.input_directories == {"/proj/lib.hda"}
    assert refs.output_directories == {WORKING_DIR}


_ROOTS: Tuple[str, ...] = ("/in", WORKING_DIR + "/input")


@settings(max_examples=60, deadline=None)
@given(
    snapshots(max_steps=4, max_tasks=3),
    st.lists(st.tuples(st.integers(0, 3), st.sampled_from(_ROOTS), st.integers(0, 3)), max_size=8),
    st.sets(st.integers(0, 3), max_size=4),
)
def test_attachments_follow_the_root_step_rule(
    base: GraphSnapshot, reads: List[Tuple[int, str, int]], on_disk_ids: Set[int]
) -> None:
    """Against a model file system, the attached files are the root steps' inputs that exist.

    The model is a plain scan of root steps, so a step that gains a dependency loses its inputs,
    and a file created for a dependent step's input changes nothing.
    """
    # Add each drawn read as a declared input of a step's first item.
    steps: List[StepSpec] = list(base.steps)
    for step_index, root, file_id in reads:
        # Skip a read drawn for a step this snapshot doesn't have.
        if step_index >= len(steps):
            continue
        s: StepSpec = steps[step_index]
        path: str = f"{root}/f{file_id}.bgeo".replace(WORKING_DIR, "__PDG_DIR__")
        first: WorkItemSpec = s.items[0]
        reading: WorkItemSpec = WorkItemSpec(
            name=first.name,
            task_index=first.task_index,
            pdg_index=first.pdg_index,
            item_id=first.item_id,
            node_name=first.node_name,
            declared_outputs=first.declared_outputs,
            declared_inputs=first.declared_inputs + (DeclaredFile(path, "file"),),
        )
        steps[step_index] = StepSpec(s.name, s.dependencies, (reading,) + s.items[1:])
    snapshot: GraphSnapshot = GraphSnapshot(steps=tuple(steps))
    files: Set[str] = {f"{root}/f{i}.bgeo" for root in _ROOTS for i in on_disk_ids}
    sub: PdgSubmitter = make_submitter(snapshot, files=files)
    refs: AssetReferences = sub.get_asset_references(sub.get_settings())

    # The model: the .hip, plus every existing input of a step with no dependencies.
    expected: Set[str] = {"/proj/scene.hip"}
    for s in snapshot.steps:
        if s.dependencies:
            continue
        for it in s.items:
            for declared in it.declared_inputs:
                resolved: str = declared.path.replace("__PDG_DIR__", WORKING_DIR)
                if resolved in files:
                    expected.add(os.path.normpath(resolved))
    assert refs.input_filenames == expected


# ---------------------------------------------------------------- submit


def test_submit_writes_the_bundle_and_creates_the_job(tmp_path: Path) -> None:
    """The bundle holds the job the snapshot describes, and CreateJob gets the known paths.

    Every attached file's folder must be known, or the library would stop to ask about it.
    """
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(
            step("pattern", 1, inputs=("__PDG_DIR__/input/rock.bgeo",)),
            step("render", 2, ("pattern",)),
        )
    )
    create: FakeCreateJob = FakeCreateJob()
    log: List[str] = []
    sub: PdgSubmitter = make_submitter(
        None,
        create_job=create,
        scene=SceneFiles(
            hip_file="/proj/scene.hip", assets=("/tex/wood.rat",), directories=("/proj/lib",)
        ),
        files={f"{WORKING_DIR}/input/rock.bgeo"},
        log=log,
    )
    bundle: Path = tmp_path / "bundle"
    assert sub.submit(snapshot, str(bundle)) == create.result

    # The bundle files: the template, the cook's values, and sorted, unique attachments.
    files: Dict[str, Any] = read_bundle(bundle)
    assert [s["name"] for s in files["template"]["steps"]] == ["pattern", "render"]
    decode_job_template(template=files["template"])
    values: Dict[str, Any] = values_by_name(files["parameter_values"]["parameterValues"])
    assert values["CookName"] == "scene.hip"
    assert values["PdgCookId"] == COOK_ID
    assert values["JaBucket"] == "bucket-a" and values["JaRootPrefix"] == "DeadlineCloud"
    assert values["HipFile"] == "/proj/scene.hip"
    assert values["PdgDir"] == WORKING_DIR
    # CreateJob reads the job settings by type, so they must still be ints in the file.
    assert all(
        type(values[name]) is int
        for name in (
            "deadline:priority",
            "deadline:maxRetriesPerTask",
            "deadline:maxFailedTasksCount",
        )
    )
    inputs: List[str] = files["asset_references"]["assetReferences"]["inputs"]["filenames"]
    # Sorted as strings, so a native separator can move the declared input in the list.
    assert inputs == sorted(
        [os.path.normpath(f"{WORKING_DIR}/input/rock.bgeo"), "/proj/scene.hip", "/tex/wood.rat"]
    )
    # The scene's folder goes along whole, so the worker sees the files the scene loads from it.
    assert files["asset_references"]["assetReferences"]["inputs"]["directories"] == ["/proj/lib"]
    outputs: List[str] = files["asset_references"]["assetReferences"]["outputs"]["directories"]
    assert outputs == [WORKING_DIR]

    # CreateJob gets the bundle and the known paths, and the job settings stay in the bundle.
    ((bundle_dir, kwargs),) = create.calls
    assert bundle_dir == str(bundle)
    known: List[str] = kwargs["known_asset_paths"]
    assert WORKING_DIR in known
    assert all(os.path.dirname(path) in known for path in inputs)
    assert "priority" not in kwargs
    assert kwargs["print_function_callback"] == log.append
    assert any("2 step(s) and 3 task(s)" in line for line in log)


def test_writing_the_bundle_twice_gives_identical_files(tmp_path: Path) -> None:
    """The same snapshot must give the same bundle bytes, so nothing random or I/O-driven leaks in."""
    # Submit the same snapshot into two folders, and compare each file.
    snapshot: GraphSnapshot = GraphSnapshot(steps=(step("a", 2), step("b", 1, ("a",))))
    first: Path = tmp_path / "1"
    second: Path = tmp_path / "2"
    make_submitter().submit(snapshot, str(first))
    make_submitter().submit(snapshot, str(second))
    for name in ("template.yaml", "parameter_values.yaml", "asset_references.yaml"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_a_confirmation_is_answered_yes_and_logged(tmp_path: Path) -> None:
    """No artist watches a PDG thread, so the library's question is answered and left in the log."""
    # Submit, then ask the confirmation callback that CreateJob was given.
    create: FakeCreateJob = FakeCreateJob()
    log: List[str] = []
    make_submitter(create_job=create, log=log).submit(
        GraphSnapshot(steps=(step("a", 1),)), str(tmp_path)
    )
    confirm: Callable[[str, bool], bool] = create.calls[0][1]["interactive_confirmation_callback"]
    assert confirm("Run the submission hooks?", False) is True
    assert any("Run the submission hooks?" in line for line in log)


def test_progress_is_logged_and_never_cancels(tmp_path: Path) -> None:
    """Hashing and uploading report to the cook log, and returning False would cancel the upload."""
    # Submit, then call the progress callbacks that CreateJob was given.
    create: FakeCreateJob = FakeCreateJob()
    log: List[str] = []
    make_submitter(create_job=create, log=log).submit(
        GraphSnapshot(steps=(step("a", 1),)), str(tmp_path)
    )
    kwargs: Dict[str, Any] = create.calls[0][1]
    assert kwargs["hashing_progress_callback"](type("M", (), {"progressMessage": "1/2"})()) is True
    assert kwargs["upload_progress_callback"]("raw") is True
    assert "Hashing: 1/2" in log and "Uploading: raw" in log


@pytest.mark.parametrize(
    "missing, message",
    [
        ("defaults.farm_id", "Please configure the farm ID"),
        ("defaults.queue_id", "Please configure the queue ID"),
    ],
)
def test_submit_refuses_without_a_farm_or_queue(tmp_path: Path, missing: str, message: str) -> None:
    """With no farm or queue the cook can't start, and the artist sees the ROP's message."""
    create: FakeCreateJob = FakeCreateJob()
    with pytest.raises(CookSetupError, match=message):
        make_submitter(create_job=create, get_setting=workstation({missing: ""})).submit(
            GraphSnapshot(steps=(step("a", 1),)), str(tmp_path)
        )
    assert create.calls == []


@pytest.mark.parametrize("result", [None, ""])
def test_a_missing_job_id_is_a_submission_error(tmp_path: Path, result: Optional[str]) -> None:
    """Without a job ID the cook can't follow the job, so it must fail rather than wait."""
    with pytest.raises(JobSubmissionError, match="no job ID"):
        make_submitter(create_job=FakeCreateJob(result)).submit(
            GraphSnapshot(steps=(step("a", 1),)), str(tmp_path)
        )


_CODES: List[Tuple[str, Type[JobSubmissionError]]] = [
    ("ServiceQuotaExceededException", QuotaExceededError),
    ("AccessDeniedException", JobSubmissionError),
    ("ResourceNotFoundException", JobSubmissionError),
    ("ValidationException", JobSubmissionError),
    ("", JobSubmissionError),
]
_HINTS: Dict[str, str] = {
    "AccessDeniedException": "denied",
    "ResourceNotFoundException": "not found",
    "ValidationException": "rejected",
}


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    st.lists(st.sampled_from([c for c, _ in _CODES]), min_size=1, max_size=3),
    st.text(alphabet="abc xyz", max_size=12),
    st.booleans(),
)
def test_errors_map_in_order_and_keep_their_cause(
    tmp_path: Path, codes: List[str], message: str, in_response: bool
) -> None:
    """Each failure gets the remedy of the first matching code, in a fixed order.

    The code may be in the message or only in the response, and the original error stays the cause.
    """
    # Put every code in the message, or only the first one in the response.
    present: List[str] = codes[:1] if in_response else codes
    error: Exception = (
        CodedError(message, codes[0]) if in_response else RuntimeError(" ".join([message] + codes))
    )
    create: FakeCreateJob = FakeCreateJob()
    create.error = error
    with pytest.raises(JobSubmissionError) as raised:
        make_submitter(create_job=create).submit(
            GraphSnapshot(steps=(step("a", 1),)), str(tmp_path)
        )

    expected: Type[JobSubmissionError] = next(
        (kind for code, kind in _CODES if code and code in present), JobSubmissionError
    )
    assert type(raised.value) is expected
    assert str(error) in str(raised.value)
    assert raised.value.__cause__ is error
    # Below the quota, the first matching code's hint is the one shown.
    first: Optional[str] = next((c for c, _ in _CODES[1:4] if c in present), None)
    if expected is not QuotaExceededError and first is not None:
        assert _HINTS[first] in str(raised.value)


def test_a_scheduler_error_passes_through_unchanged(tmp_path: Path) -> None:
    """An error that already explains itself must reach the artist as it is, not wrapped."""
    # CreateJob raises an error the scheduler already explains.
    original: QuotaExceededError = QuotaExceededError("already explained")
    create: FakeCreateJob = FakeCreateJob()
    create.error = original
    with pytest.raises(PDGSchedulerError) as raised:
        make_submitter(create_job=create).submit(
            GraphSnapshot(steps=(step("a", 1),)), str(tmp_path)
        )
    assert raised.value is original


def test_a_value_error_from_create_job_is_mapped(tmp_path: Path) -> None:
    """The client library raises ValueError for some failures, and the artist still needs a remedy.

    Only a refusal made before CreateJob may pass through as a ValueError.
    """
    # CreateJob raises the plain ValueError the library raises for a bad job parameter.
    message: str = 'Job parameter "X" is missing required key "type"'
    error: ValueError = ValueError(message)
    create: FakeCreateJob = FakeCreateJob()
    create.error = error
    with pytest.raises(JobSubmissionError) as raised:
        make_submitter(create_job=create).submit(
            GraphSnapshot(steps=(step("a", 1),)), str(tmp_path)
        )
    assert type(raised.value) is JobSubmissionError
    assert message in str(raised.value)
    assert raised.value.__cause__ is error


def test_an_expired_session_is_reported_before_any_hashing(tmp_path: Path) -> None:
    """The queue is read before CreateJob, so an expired login fails before files are hashed."""
    expired: ClientError = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "token expired"}}, "GetQueue"
    )

    def refuse(farm: str, queue: str) -> Tuple[str, str]:
        raise expired

    create: FakeCreateJob = FakeCreateJob()
    with pytest.raises(JobSubmissionError) as raised:
        make_submitter(create_job=create, job_attachments=refuse).submit(
            GraphSnapshot(steps=(step("a", 1),)), str(tmp_path)
        )
    assert raised.value.__cause__ is expired
    assert create.calls == []


@pytest.mark.parametrize(
    "snapshot",
    [
        GraphSnapshot(steps=(step("empty", 0),)),
        GraphSnapshot(steps=(step("a", 1, ("ghost",)),)),
    ],
)
def test_an_impossible_job_is_refused_before_the_bundle_is_written(
    tmp_path: Path, snapshot: GraphSnapshot
) -> None:
    """A step with no tasks, or one that depends on a step not in the job, can't be created.

    Refusing it early leaves no bundle and makes no CreateJob call.
    """
    # Submit the impossible job, and check nothing was written or created.
    create: FakeCreateJob = FakeCreateJob()
    with pytest.raises(ValueError):
        make_submitter(create_job=create).submit(snapshot, str(tmp_path / "bundle"))
    assert create.calls == []
    assert not (tmp_path / "bundle").exists()


def test_the_template_needs_a_snapshot() -> None:
    """The job's shape comes from the snapshot, so a template without one is refused."""
    sub: PdgSubmitter = make_submitter(None)
    with pytest.raises(ValueError):
        sub.get_job_template(sub.get_settings())


def test_every_node_read_goes_through_the_injected_reader() -> None:
    """Houdini crashes when a PDG thread reads the scene, so every node read must use read_node."""
    # The node, the version, and the queue values all fail outside read_node.
    allowed: List[bool] = [False]

    def read_node(read: Callable[[], Any]) -> Any:
        allowed[0] = True
        try:
            return read()
        finally:
            allowed[0] = False

    def guarded(value: Any) -> Callable[..., Any]:
        def read(*args: Any) -> Any:
            assert allowed[0], "read outside read_node"
            return value

        return read

    # Use every read path: settings, template, and parameter values.
    sub: PdgSubmitter = PdgSubmitter(
        node=FakeNode({"priority": 10}, allowed=allowed),
        working_dir=WORKING_DIR,
        snapshot=GraphSnapshot(steps=(step("a", 1),)),
        job_attachments=lambda farm, queue: ("b", "p"),
        read_node=read_node,
        get_setting=workstation(),
        houdini_version=guarded("21.0.631"),
        queue_parameter_values=guarded([]),
    )
    settings_: PdgSubmitterSettings = sub.get_settings()
    sub.get_job_template(settings_)
    sub.get_parameter_values(settings_, [])
    assert settings_.priority == 10


# ---------------------------------------------------------------- queue lookup


def test_the_queue_bucket_comes_from_its_job_attachments_settings() -> None:
    """The work item payloads go to the queue's own job attachments bucket and prefix."""
    client: FakeDeadline = FakeDeadline({"s3BucketName": "bucket-a", "rootPrefix": "DeadlineCloud"})
    assert submitter_module._queue_job_attachments(FARM, QUEUE, client=client) == (
        "bucket-a",
        "DeadlineCloud",
    )


@pytest.mark.parametrize("attachments", [None, {"s3BucketName": "bucket-a"}])
def test_a_queue_without_job_attachments_cannot_cook(
    attachments: Optional[Dict[str, str]],
) -> None:
    """Every payload is stored in the bucket, so the cook must refuse a queue without one, naming it."""
    with pytest.raises(CookSetupError, match=QUEUE):
        submitter_module._queue_job_attachments(FARM, QUEUE, client=FakeDeadline(attachments))
