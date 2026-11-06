# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for how the Houdini adaptor runs PDG work items.

A stand-in for the Houdini client serves the adaptor's action queue and prints what
the real client prints, so the adaptor's own handlers read it.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import gzip
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from openjd.adaptor_runtime.app_handlers import RegexHandler
from openjd.adaptor_runtime.application_ipc import ActionsQueue
from openjd.adaptor_runtime_client import Action

from deadline.houdini_adaptor.HoudiniAdaptor import HoudiniAdaptor
from deadline.houdini_adaptor.HoudiniAdaptor.__main__ import _copy_task_ids_into_run_data
from deadline.houdini_adaptor.HoudiniAdaptor.adaptor import HoudiniNotRunningError
from deadline.houdini_adaptor.pdg_actions import EXIT_CODE_MARKER
from deadline.houdini_pdg.errors import PayloadTransferError
from deadline.houdini_pdg.ports import OutputFile, TaskAddress
from deadline.houdini_pdg.task_storage import S3TaskStorage

# One PDG task's address and run data, and a ROP job's init and run data.
ADDRESS: TaskAddress = TaskAddress("farm-1", "queue-2", "job-3", "step-4", "task-4-0")
IDS: Dict[str, str] = dataclasses.asdict(ADDRESS)
PDG_RUN: Dict[str, Any] = {"pdg": {"step": "s", "task_index": 2, "record_outputs": True, **IDS}}
ROP_INIT: Dict[str, Any] = {"scene_file": "a.hip", "version": "21.0", "render_node": "/out/r"}
ROP_RUN: Dict[str, Any] = {"frame_range": {"start": 1, "end": 1, "step": 1}}


def pdg_init(pdg_dir: str) -> Dict[str, Any]:
    """A PDG session's init data, as the submitter writes it."""
    pdg: Dict[str, str] = {"pdg_dir": pdg_dir, "bucket": "bucket", "root_prefix": "root"}
    return {"scene_file": "/session/scene.hip", "version": "21.0", "pdg": pdg}


def work_item(attributes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A work item payload named ropfetch1_7, as PDG writes it."""
    return {
        "workitem": {"nodeName": "ropfetch1", "id": 7, "index": 0, "attributes": attributes or {}}
    }


class FakeStorage:
    """The task storage port, in memory. The worker never stores a payload or reads outputs."""

    def __init__(
        self, payload: Optional[Dict[str, Any]] = None, *, get_error: Optional[Exception] = None
    ) -> None:
        self.payload: Dict[str, Any] = payload or work_item()
        self.get_error: Optional[Exception] = get_error
        self.published: List[Tuple[TaskAddress, List[OutputFile]]] = []

    def put_payload(self, address: TaskAddress, payload_json: str) -> None:
        raise AssertionError("the worker never stores a payload")

    def get_payload(self, address: TaskAddress) -> Dict[str, Any]:
        if self.get_error:
            raise self.get_error
        return copy.deepcopy(self.payload)

    def put_outputs(self, address: TaskAddress, outputs: Sequence[OutputFile]) -> None:
        self.published.append((address, list(outputs)))

    def get_outputs(self, address: TaskAddress) -> Optional[List[OutputFile]]:
        raise AssertionError("the worker never reads an output record")


class FakeTelemetry:
    """Keeps the errors the adaptor records, instead of sending them."""

    def __init__(self) -> None:
        self.errors: List[Dict[str, Any]] = []

    def record_error(self, details: Dict[str, Any], exception_type: str) -> None:
        self.errors.append(details)


class FakeHoudini:
    """The adaptor's running Houdini client process.

    It takes each action off the adaptor's queue. For a work item it writes the outputs
    file and prints the exit code, as the real client does, or it exits first.
    """

    def __init__(
        self, adaptor: HoudiniAdaptor, *, exits_first: bool = False, exit_code: int = 0
    ) -> None:
        # The process state the adaptor reads, and the actions this client was sent.
        self.is_running: bool = True
        self.returncode: Optional[int] = None
        self.received: List[Action] = []
        self._adaptor: HoudiniAdaptor = adaptor
        self._exits_first: bool = exits_first
        self._exit_code: int = exit_code
        self._handler: RegexHandler = RegexHandler(adaptor._get_regex_callbacks())
        self._stop: threading.Event = threading.Event()
        self._thread: threading.Thread = threading.Thread(target=self._serve, daemon=True)
        # The adaptor reads its client from this attribute.
        wiring: Any = adaptor
        wiring._houdini_client = self

    def __enter__(self) -> FakeHoudini:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        # Poll the queue the way the client does, and act on the work item actions.
        while not self._stop.is_set() and self.is_running:
            action: Optional[Action] = self._adaptor._action_queue.dequeue_action()
            if action is None:
                time.sleep(0.01)
                continue
            self.received.append(action)
            if action.name == "pdg_run_work_item":
                self._run(action.args or {})

    def _run(self, args: Dict[str, Any]) -> None:
        # Houdini can die mid-item, and then it prints no exit code.
        if self._exits_first:
            self.returncode = 9
            self.is_running = False
            return
        outputs: Path = Path(args["pdg_temp"], "data", f"{args['item_name']}.outputs.json")
        record: Dict[str, Any] = {"outputs": [{"path": "__PDG_DIR__/a.exr", "tag": "file/image"}]}
        outputs.write_text(json.dumps(record))
        # The command printed a success line of its own, which the client passed on prefixed.
        # The real exit code comes later, after the adaptor has had time to poll.
        self._handler.emit(logging.makeLogRecord({"msg": f"> {EXIT_CODE_MARKER}0"}))
        time.sleep(0.3)
        self._handler.emit(logging.makeLogRecord({"msg": f"{EXIT_CODE_MARKER}{self._exit_code}"}))


def make_adaptor(
    init_data: Dict[str, Any], storage: Optional[FakeStorage] = None, **kwargs: Any
) -> HoudiniAdaptor:
    """An adaptor with its own action queue, no telemetry, and the given storage."""
    adaptor: HoudiniAdaptor = HoudiniAdaptor(init_data, **kwargs)
    # These are the adaptor's collaborators, not the code under test.
    wiring: Any = adaptor
    wiring._action_queue = ActionsQueue()
    wiring._telemetry_client = FakeTelemetry()
    if storage is not None:
        wiring._task_storage = storage
    return adaptor


def queued(adaptor: HoudiniAdaptor) -> List[Tuple[str, Any]]:
    """Take every action off the adaptor's queue, in order."""
    actions: List[Tuple[str, Any]] = []
    while len(adaptor._action_queue):
        action: Optional[Action] = adaptor._action_queue.dequeue_action()
        assert action is not None
        actions.append((action.name, action.args))
    return actions


def test_a_pdg_session_queues_only_the_session_action_and_loads_no_scene(tmp_path: Path) -> None:
    """A PDG session must not load a scene up front or set a render node, because each item loads its own."""
    adaptor: HoudiniAdaptor = make_adaptor(pdg_init(str(tmp_path)))
    adaptor._populate_action_queue()
    assert queued(adaptor) == [("pdg_session", {})]


def _without(data: Dict[str, Any], *path: str) -> Dict[str, Any]:
    """A copy of the data with the key at the path removed."""
    copied: Dict[str, Any] = copy.deepcopy(data)
    del functools.reduce(lambda d, k: d[k], path[:-1], copied)[path[-1]]
    return copied


_PDG_INIT: Dict[str, Any] = pdg_init("/work/pdg")

# Every case is valid or not by the schemas alone: ROP data keeps its required keys, and a
# pdg object missing any field is refused. The submitter's tests check that real PDG data passes.
_SCHEMA_CASES: List[Tuple[str, Dict[str, Any], bool]] = [
    ("init_data", ROP_INIT, True),
    ("init_data", _without(ROP_INIT, "render_node"), False),
    *[("init_data", _without(_PDG_INIT, "pdg", k), False) for k in _PDG_INIT["pdg"]],
    ("run_data", ROP_RUN, True),
    ("run_data", {}, False),
    *[("run_data", _without(PDG_RUN, "pdg", k), False) for k in PDG_RUN["pdg"]],
]


@pytest.mark.parametrize("kind, data, valid", _SCHEMA_CASES)
def test_the_schemas_keep_rop_data_as_it_was_and_refuse_an_incomplete_pdg_object(
    kind: str, data: Dict[str, Any], valid: bool
) -> None:
    """A ROP job's data must validate exactly as before, and a PDG task missing an ID must fail at the start."""
    validator: Any = getattr(make_adaptor(ROP_INIT).validators, kind)
    if valid:
        validator.validate(data)
    else:
        with pytest.raises(Exception):
            validator.validate(data)


@pytest.mark.parametrize("init_data, run_data", [(ROP_INIT, PDG_RUN), (_PDG_INIT, ROP_RUN)])
def test_pdg_and_rop_data_never_mix(init_data: Dict[str, Any], run_data: Dict[str, Any]) -> None:
    """A work item in a ROP session has no PDG actions, and a frame in a PDG session has no scene, so both fail."""
    # Houdini is running, so only the mismatch can stop the task, and nothing reaches the client.
    adaptor: HoudiniAdaptor = make_adaptor(init_data, FakeStorage())
    FakeHoudini(adaptor)
    with pytest.raises(RuntimeError, match="disagree"):
        adaptor.on_run(copy.deepcopy(run_data))
    assert queued(adaptor) == []


@pytest.mark.parametrize("exit_code, record_outputs", [(0, True), (3, True), (0, False)])
def test_the_adaptor_downloads_and_publishes_while_the_client_only_runs(
    tmp_path: Path, exit_code: int, record_outputs: bool
) -> None:
    """Only the adaptor's process can reach S3, so it must download and publish, and send the client plain arguments.

    The item's exit code is the task's outcome, so a failed item fails its task and publishes nothing.
    A step that doesn't record outputs has no outputs file, so publishing it would fail a good task.
    """
    pdg_dir: str = str(tmp_path / "pdg")
    rule: Dict[str, str] = dict(
        source_path_format="POSIX",
        source_path="/studio",
        destination_path="/mnt",
        destination_os="POSIX",
    )
    hip_attribute: Dict[str, Any] = {"type": 3, "flag": 0, "value": [{"data": "/studio/a.hip"}]}
    storage: FakeStorage = FakeStorage(work_item({"hipfile": hip_attribute}))
    adaptor: HoudiniAdaptor = make_adaptor(
        pdg_init(pdg_dir), storage, path_mapping_data={"path_mapping_rules": [rule]}
    )
    run_data: Dict[str, Any] = {"pdg": {**PDG_RUN["pdg"], "record_outputs": record_outputs}}
    with FakeHoudini(adaptor, exit_code=exit_code) as houdini:
        if exit_code == 0:
            adaptor.on_run(run_data)
        else:
            with pytest.raises(RuntimeError, match=f"exit code {exit_code}"):
                adaptor.on_run(run_data)
    # The client gets the item's name and folders, in one action.
    client_args: Dict[str, Any] = {
        "item_name": "ropfetch1_7",
        "pdg_dir": pdg_dir,
        "pdg_temp": os.path.join(pdg_dir, "pdgtemp"),
        "scene_file": "/session/scene.hip",
        "record_outputs": record_outputs,
    }
    assert [(a.name, a.args) for a in houdini.received] == [("pdg_run_work_item", client_args)]
    # The payload was mapped with the session's rules before the client saw it.
    written: Path = Path(pdg_dir, "pdgtemp", "data", "ropfetch1_7.json.gz")
    loaded: Dict[str, Any] = json.loads(gzip.decompress(written.read_bytes()))
    assert loaded["workitem"]["attributes"]["hipfile"]["value"][0]["data"] == "/mnt/a.hip"
    published: List[Tuple[TaskAddress, List[OutputFile]]] = [
        (ADDRESS, [OutputFile("__PDG_DIR__/a.exr", "file/image")])
    ]
    # The fake client always writes an outputs file, so only the step's flag can skip the publish.
    assert storage.published == (published if exit_code == 0 and record_outputs else [])


@pytest.mark.parametrize(
    "storage, run_data, message",
    [
        (
            FakeStorage(get_error=PayloadTransferError("ran before it was ready")),
            PDG_RUN,
            "ran before",
        ),
        (FakeStorage(), {"pdg": {**PDG_RUN["pdg"], "task_id": " "}}, "DEADLINE_TASK_ID"),
    ],
)
def test_a_task_that_cannot_be_downloaded_sends_nothing_to_the_client(
    tmp_path: Path, storage: FakeStorage, run_data: Dict[str, Any], message: str
) -> None:
    """A task whose payload isn't there, or that has no task ID, fails in the adaptor, so the client never runs a stale item."""
    # Houdini is running, so only the missing payload or ID can stop the task.
    adaptor: HoudiniAdaptor = make_adaptor(pdg_init(str(tmp_path)), storage)
    FakeHoudini(adaptor)
    with pytest.raises(PayloadTransferError, match=message):
        adaptor.on_run(copy.deepcopy(run_data))
    assert queued(adaptor) == []


def test_storage_is_built_once_from_the_session_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every task in a session reuses one S3 client, pointed at the bucket and prefix the job named."""
    # A region is enough to build the client. Nothing here calls S3.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    adaptor: HoudiniAdaptor = make_adaptor(pdg_init(str(tmp_path)))
    storage: Any = adaptor._get_task_storage()
    assert adaptor._get_task_storage() is storage
    assert isinstance(storage, S3TaskStorage)
    assert storage._s3.meta.service_model.service_name == "s3"
    assert (storage._bucket, storage._settings.rootPrefix) == ("bucket", "root")


def test_a_task_fails_when_houdini_exits_first(tmp_path: Path) -> None:
    """If Houdini dies mid-item, the task must fail with its exit code instead of waiting forever."""
    # The client exits when it is told to run the item, without printing an exit code.
    adaptor: HoudiniAdaptor = make_adaptor(pdg_init(str(tmp_path / "pdg")), FakeStorage())
    with FakeHoudini(adaptor, exits_first=True):
        with pytest.raises(HoudiniNotRunningError, match="Exit code 9"):
            adaptor.on_run(copy.deepcopy(PDG_RUN))
    telemetry: Any = adaptor._telemetry_client
    assert [e["exit_code"] for e in telemetry.errors] == [9]


def test_a_rop_session_has_no_pdg_callback() -> None:
    """A ROP job's output handling must not change, so it never reacts to the work item exit code."""
    callbacks: List[Any] = make_adaptor(ROP_INIT)._get_regex_callbacks()
    assert not any(c.get_match(f"{EXIT_CODE_MARKER}0") for c in callbacks)


def test_the_run_front_end_adds_the_task_ids(tmp_path: Path) -> None:
    """The daemon started before the task, so the IDs that find its payload must come from the run command."""
    env: Dict[str, str] = {f"DEADLINE_{k.upper()}": v for k, v in IDS.items()}
    pdg: Dict[str, Any] = {"step": "s", "task_index": 2, "record_outputs": False}
    inline: List[str] = ["daemon", "run", "--connection-file", "c.json", "--run-data"]
    inline.append(json.dumps({"pdg": pdg}))
    result: List[str] = _copy_task_ids_into_run_data(inline, env)
    assert result[:5] == inline[:5]
    assert json.loads(result[5]) == {"pdg": {**pdg, **IDS}}
    # Run data in a file comes back inline, with the IDs added.
    run_file: Path = tmp_path / "run.yaml"
    run_file.write_text("pdg:\n  step: s\n  task_index: 2\n  record_outputs: true\n")
    from_file: List[str] = _copy_task_ids_into_run_data(
        ["daemon", "run", f"--run-data=file://{run_file}"], env
    )
    assert from_file[2].startswith("--run-data=")
    assert json.loads(from_file[2][len("--run-data=") :])["pdg"]["task_id"] == "task-4-0"
    # ROP run data, unreadable run data and other commands are left for the runtime.
    rop: List[str] = ["daemon", "run", "--run-data", json.dumps(ROP_RUN)]
    assert _copy_task_ids_into_run_data(rop, env) == rop
    unreadable: List[str] = ["daemon", "run", "--run-data", "file:///no/such/file"]
    assert _copy_task_ids_into_run_data(unreadable, env) == unreadable
    assert _copy_task_ids_into_run_data(["daemon", "start"], env) == ["daemon", "start"]
    # Only the IDs the session has are added.
    partial: List[str] = _copy_task_ids_into_run_data(inline, {"DEADLINE_TASK_ID": "task-9"})
    assert json.loads(partial[5]) == {"pdg": {**pdg, "task_id": "task-9"}}
