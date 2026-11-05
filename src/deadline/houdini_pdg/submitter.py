# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Submit a cook as one Deadline Cloud job, through the same ``BaseSubmitter`` path as the ROP.

Farm, queue, and queue parameters, including the Conda packages, come from the
same place as the ROP's: the workstation defaults and the node's queue parameters.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, cast

import yaml

from deadline.client.api import (
    BaseSubmitter,
    BaseSubmitterSettings,
    SubmissionContext,
    create_job_from_job_bundle,
)
from deadline.client.config import get_setting as _workstation_setting
from deadline.client.job_bundle.submission import AssetReferences
from deadline.houdini_submitter.python.deadline_cloud_for_houdini._job_bundle import (
    _ADAPTOR_OVERRIDE_PARAM_NAMES,
    _apply_adaptor_wheels_override,
    common_job_parameter_values,
    get_houdini_environments,
    merge_parameter_values,
    read_common_job_settings,
    read_queue_parameter_definitions,
    write_job_bundle,
)

from .errors import (
    CookSetupError,
    JobSubmissionError,
    PDGSchedulerError,
    QuotaExceededError,
)
from .graph import PDG_DIR_TOKEN, PDG_TOKEN_PREFIX, GraphSnapshot, StepSpec
from .ports import SceneFiles

_SESSION_ENVIRONMENT: str = "PdgSession"
_OVERRIDE_ENVIRONMENT: str = "OverrideAdaptor"
_TASK_INDEX: str = "{{Task.Param.TaskIndex}}"
_INT32_MAX: int = 2**31 - 1

# The ROP's messages, so an artist sees the same text from either node.
_NO_FARM: str = "Please configure the farm ID in the AWS Deadline Cloud render node (ROP) settings"
_NO_QUEUE: str = (
    "Please configure the queue ID in the AWS Deadline Cloud render node (ROP) settings"
)

#: The parameters every PDG job declares. The values come from the cook.
_PDG_PARAMETER_DEFINITIONS: List[Dict[str, Any]] = [
    {"name": "CookName", "type": "STRING"},
    {"name": "PdgCookId", "type": "STRING"},
    # The worker computes its payload's key from these, so they must be known
    # before the job, step, and task IDs are.
    {"name": "JaBucket", "type": "STRING"},
    {"name": "JaRootPrefix", "type": "STRING"},
    {"name": "HipFile", "type": "PATH", "objectType": "FILE", "dataFlow": "IN"},
    # $PDG_DIR on the worker. Declared OUT so whatever a step writes there is
    # uploaded, and so synced to the steps that depend on it.
    {"name": "PdgDir", "type": "PATH", "objectType": "DIRECTORY", "dataFlow": "OUT"},
]
_PDG_PARAMETER_NAMES: Set[str] = {definition["name"] for definition in _PDG_PARAMETER_DEFINITIONS}
# Names only the cook may set. A node parameter with one of them is dropped, so no
# value is sent twice or undeclared, and the wheels override always applies cleanly.
_RESERVED_PARAMETER_NAMES: Set[str] = _PDG_PARAMETER_NAMES | _ADAPTOR_OVERRIDE_PARAM_NAMES


def _houdini_version() -> str:
    """The running Houdini's version, computed as the ROP computes it."""
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
        _get_houdini_version,
    )

    return _get_houdini_version()


def _node_queue_parameter_values(node: Any) -> List[Dict[str, Any]]:
    """The node's queue parameter values, read as the ROP reads them. Needs a Houdini session."""
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini.queue_parameters import (
        get_queue_parameter_values_as_openjd,
    )

    return get_queue_parameter_values_as_openjd(node)


@dataclass
class PdgSubmitterSettings(BaseSubmitterSettings):
    """The job's settings. The shared fields use the ROP's parm names."""

    # The cook releases each task once its payload is stored, so the job starts
    # suspended. A failed work item fails the cook, so no failure is tolerated.
    initial_status: str = "SUSPENDED"
    max_retries_per_task: int = 1
    max_failed_tasks_count: int = 0
    hip_file: str = ""
    scene_assets: List[str] = field(default_factory=list)
    #: Same names as the ROP's settings, so the shared adaptor-wheels override applies.
    include_adaptor_wheels: bool = False
    adaptor_wheels: str = ""


class PdgSubmitter(BaseSubmitter):
    """Builds and submits the job for one snapshot. Implements ``JobSubmitter``.

    Runs on a PDG thread with no dialog, so it never prompts and reports progress
    through ``log``.
    """

    def __init__(
        self,
        *,
        node: Any,
        working_dir: str,
        snapshot: Optional[GraphSnapshot] = None,
        create_job: Callable[..., Optional[str]] = create_job_from_job_bundle,
        log: Callable[[str], None] = print,
        scene_files: Optional[Callable[[], SceneFiles]] = None,
        file_exists: Callable[[str], bool] = os.path.isfile,
        job_attachments: Optional[Callable[[str, str], Tuple[str, str]]] = None,
        cook_id: str = "",
        read_node: Callable[[Callable[[], Any]], Any] = lambda read: read(),
        get_setting: Callable[[str], str] = _workstation_setting,
        houdini_version: Callable[[], str] = _houdini_version,
        queue_parameter_values: Callable[[Any], List[Dict[str, Any]]] = (
            _node_queue_parameter_values
        ),
    ) -> None:
        # Every read of ``node`` goes through ``read_node``, so a host whose scene
        # must be read on one thread can route them there.
        self._read_node: Callable[[Callable[[], Any]], Any] = read_node
        self._node: Any = node
        self._working_dir: str = working_dir
        self._snapshot: Optional[GraphSnapshot] = snapshot
        self._create_job: Callable[..., Optional[str]] = create_job
        self._log: Callable[[str], None] = log
        self._scene_files: Callable[[], SceneFiles] = scene_files or (
            lambda: SceneFiles(hip_file="")
        )
        self._file_exists: Callable[[str], bool] = file_exists
        self._job_attachments: Callable[[str, str], Tuple[str, str]] = (
            job_attachments or _queue_job_attachments
        )
        self._cook_id: str = cook_id or uuid.uuid4().hex
        self._get_setting: Callable[[str], str] = get_setting
        self._houdini_version: Callable[[], str] = houdini_version
        self._queue_parameter_values: Callable[[Any], List[Dict[str, Any]]] = queue_parameter_values
        self._location: Optional[Tuple[str, str]] = None

    def get_settings(self) -> PdgSubmitterSettings:
        """The job settings, read from the scheduler node with the ROP's shared reader."""
        # Read the shared parms, then refuse any numeric one a job can't carry.
        settings: PdgSubmitterSettings = PdgSubmitterSettings()
        self._read_node(lambda: read_common_job_settings(self._node, settings))
        settings.priority = _whole_number(settings.priority, "priority", 0, 100)
        settings.max_failed_tasks_count = _whole_number(
            settings.max_failed_tasks_count, "failed_tasks_limit", 0, _INT32_MAX
        )
        settings.max_retries_per_task = _whole_number(
            settings.max_retries_per_task, "task_retry_limit", 0, _INT32_MAX
        )
        # The node's initial_status parm does not apply: a task released before
        # its payload is stored would run without it.
        settings.initial_status = "SUSPENDED"
        # The scene's files and folders go with every task. An unnamed job takes the scene's name.
        scene: SceneFiles = self._scene_files()
        settings.hip_file = scene.hip_file
        settings.scene_assets = list(scene.assets)
        settings.input_directories.extend(scene.directories)
        settings.job_name = (
            settings.job_name or os.path.basename(scene.hip_file) or f"PDG cook {self._cook_id[:8]}"
        )
        settings.output_directories = [self._working_dir]
        return settings

    def get_job_template(
        self,
        settings: BaseSubmitterSettings,
        host_requirements: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One step per snapshot step, each with an INT ``TaskIndex`` parameter, plus the shared wheels override."""
        # The scheduler offers no host requirements, so the argument is ignored.
        template: Dict[str, Any] = _job_template(
            self._require_snapshot(),
            houdini_version=self._read_node(self._houdini_version),
            queue_parameter_definitions=self._read_node(
                lambda: read_queue_parameter_definitions(self._node)
            ),
        )
        if settings.description:
            template["description"] = settings.description
        _apply_adaptor_wheels_override(template, _wheels_enabled(settings))
        # The override activates a venv holding the dev adaptor, so the session's
        # daemon must start after it.
        environments: List[Dict[str, Any]] = template.setdefault("jobEnvironments", [])
        environments.sort(key=lambda env: env.get("name") != _OVERRIDE_ENVIRONMENT)
        return template

    def get_parameter_values(
        self,
        settings: BaseSubmitterSettings,
        queue_parameters: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """The ``deadline:*`` values and the node's queue parameters. A non-empty caller value wins."""
        # The job always starts suspended, whatever the settings say.
        pdg_settings: PdgSubmitterSettings = cast(PdgSubmitterSettings, settings)
        bucket, root_prefix = self._job_attachments_location()
        values: List[Dict[str, Any]] = common_job_parameter_values(
            dataclasses.replace(settings, initial_status="SUSPENDED")
        )
        # A non-positive worker count means no limit, so the value is left out.
        if settings.max_worker_count > 0:
            values.append({"name": "deadline:maxWorkerCount", "value": settings.max_worker_count})
        # The values of the PDG parameters, then the node's queue parameters. The template
        # drops a node parameter with a reserved name, so its value is dropped too.
        node_values: List[Dict[str, Any]] = self._read_node(
            lambda: self._queue_parameter_values(self._node)
        )
        values += [
            {"name": "CookName", "value": settings.job_name},
            {"name": "PdgCookId", "value": self._cook_id},
            {"name": "JaBucket", "value": bucket},
            {"name": "JaRootPrefix", "value": root_prefix},
            {"name": "HipFile", "value": getattr(pdg_settings, "hip_file", "")},
            {"name": "PdgDir", "value": self._working_dir},
            *(v for v in node_values if v.get("name") not in _RESERVED_PARAMETER_NAMES),
        ]
        if _wheels_enabled(settings):
            values.append({"name": "AdaptorWheels", "value": pdg_settings.adaptor_wheels})
        return merge_parameter_values(values, queue_parameters)

    def get_asset_references(self, settings: BaseSubmitterSettings) -> AssetReferences:
        """The ``.hip``, the scene assets, and the declared inputs of steps with no dependencies.

        A step with dependencies gets its inputs from its upstream steps' outputs.
        """
        # Attach the scene's files and the root steps' inputs, and collect what tasks write.
        files: Set[str] = set(settings.input_filenames)
        hip_file: str = getattr(settings, "hip_file", "")
        if hip_file:
            files.add(hip_file)
        files.update(getattr(settings, "scene_assets", []))
        files.update(self._root_step_inputs(self._require_snapshot()))
        return AssetReferences(
            input_filenames=files,
            input_directories=set(settings.input_directories),
            output_directories={self._working_dir},
        )

    def submit(self, snapshot: GraphSnapshot, bundle_dir: str) -> str:
        """Write the bundle with the shared writer, create the job, and return its ID.

        Raises :class:`~deadline.houdini_pdg.errors.QuotaExceededError` or
        :class:`~deadline.houdini_pdg.errors.JobSubmissionError`, chained to the cause.
        """
        # Refuse before any work when the workstation names no farm or queue.
        if not self._get_setting("defaults.farm_id"):
            raise CookSetupError(_NO_FARM)
        if not self._get_setting("defaults.queue_id"):
            raise CookSetupError(_NO_QUEUE)
        self._snapshot = snapshot
        try:
            # The node's queue parameters are declared in the template, and the
            # queue's own are merged in on creation, so none are fetched here.
            settings: PdgSubmitterSettings = self.get_settings()
            context: SubmissionContext = self.get_submission_context(settings, queue_parameters=[])
            os.makedirs(bundle_dir, exist_ok=True)
            write_job_bundle(context, bundle_dir)
        except (PDGSchedulerError, ValueError):
            # A refused setting or an impossible job already says what to change.
            raise
        except Exception as e:
            # Everything the job needs from the queue is read before the first
            # byte is hashed, so an expired session is reported here.
            raise _submission_error(e) from e
        refs: AssetReferences = context.asset_references
        self._log(
            f"Submitting {len(snapshot.steps)} step(s) and {snapshot.total_tasks} task(s), "
            f"attaching {len(refs.input_filenames)} file(s)."
        )
        try:
            # Anything the library raises gets the remedy for its code, even a ValueError.
            job_id: Optional[str] = self._create_job(
                bundle_dir,
                known_asset_paths=self._known_asset_paths(settings, refs),
                print_function_callback=self._log,
                interactive_confirmation_callback=self._confirm,
                hashing_progress_callback=self._progress("Hashing"),
                upload_progress_callback=self._progress("Uploading"),
            )
        except PDGSchedulerError:
            raise
        except Exception as e:
            raise _submission_error(e) from e
        if not job_id:
            raise JobSubmissionError("The job was not created: the submission returned no job ID.")
        self._log(f"Submitted job {job_id}.")
        return job_id

    def _require_snapshot(self) -> GraphSnapshot:
        # The template and the attachments both come from the snapshot.
        if self._snapshot is None:
            raise ValueError("The job's shape is unknown until a snapshot is given.")
        return self._snapshot

    def _job_attachments_location(self) -> Tuple[str, str]:
        # Read once: the queue's bucket does not change during a cook.
        if self._location is None:
            farm_id: str = self._get_setting("defaults.farm_id")
            queue_id: str = self._get_setting("defaults.queue_id")
            self._location = self._job_attachments(farm_id, queue_id)
        return self._location

    def _root_step_inputs(self, snapshot: GraphSnapshot) -> Set[str]:
        """Declared inputs that only the workstation can provide: those of steps with no dependencies.

        It skips a file a task writes, and a missing one, so the worker reports it as not found.
        """
        # Paths some item declares as an output are written on the farm.
        produced: Set[str] = {
            self._resolve(declared.path)
            for step in snapshot.steps
            for item in step.items
            for declared in item.declared_outputs
        }
        # Keep a root step's input only when it resolves here, no task writes it, and it exists.
        inputs: Set[str] = set()
        for step in snapshot.steps:
            if step.dependencies:
                continue
            for item in step.items:
                for declared in item.declared_inputs:
                    path: str = self._resolve(declared.path)
                    if PDG_TOKEN_PREFIX in path or path in produced:
                        continue
                    if self._file_exists(path):
                        inputs.add(path)
        return inputs

    def _resolve(self, path: str) -> str:
        # A token that does not resolve here stays in the path, and the caller drops it.
        return os.path.normpath(os.path.expandvars(path.replace(PDG_DIR_TOKEN, self._working_dir)))

    def _known_asset_paths(
        self, settings: BaseSubmitterSettings, refs: AssetReferences
    ) -> List[str]:
        # Every path here was chosen by the scheduler. An unknown one would make
        # the library ask for confirmation.
        known: Set[str] = {self._working_dir}
        known.update(os.path.dirname(path) for path in refs.input_filenames)
        known.update(refs.input_directories)
        if _wheels_enabled(settings):
            known.add(cast(PdgSubmitterSettings, settings).adaptor_wheels)
        return sorted(path for path in known if path)

    def _confirm(self, message: str, default: bool) -> bool:
        # No artist is watching a PDG thread, and every path was chosen on purpose.
        self._log(f"{message.strip()} Continuing without a prompt.")
        return True

    def _progress(self, phase: str) -> Callable[[Any], bool]:
        # The library's progress reports go to the cook log. True lets it carry on.
        def report(metadata: Any) -> bool:
            self._log(f"{phase}: {getattr(metadata, 'progressMessage', metadata)}")
            return True

        return report


def _job_template(
    snapshot: GraphSnapshot,
    *,
    houdini_version: str,
    queue_parameter_definitions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The template for a snapshot. Pure, so it can be validated without a farm or a scene."""
    # A dependency outside the job would make CreateJob refuse it, so refuse it here.
    names: Set[str] = set(snapshot.step_names)
    for step in snapshot.steps:
        unknown: List[str] = [d for d in step.dependencies if d not in names]
        if unknown:
            raise ValueError(f"Step '{step.name}' depends on steps not in the job: {unknown}")
    # The node's queue parameters come first, minus any with a name the cook reserves.
    definitions: List[Dict[str, Any]] = [
        d for d in queue_parameter_definitions if d.get("name") not in _RESERVED_PARAMETER_NAMES
    ]
    definitions += [dict(definition) for definition in _PDG_PARAMETER_DEFINITIONS]
    return {
        "specificationVersion": "jobtemplate-2023-09",
        "name": "{{Param.CookName}}",
        "parameterDefinitions": definitions,
        "jobEnvironments": [_session_environment(houdini_version)],
        "steps": [_step(step) for step in snapshot.steps],
    }


def _session_environment(houdini_version: str) -> Dict[str, Any]:
    """Starts the adaptor's daemon once per session, so one warm Houdini serves every task."""
    # The init data names the scene and where the work item payloads are stored.
    init_data: Dict[str, Any] = {
        "scene_file": "{{Param.HipFile}}",
        "version": houdini_version,
        "pdg": {
            "pdg_dir": "{{Param.PdgDir}}",
            "bucket": "{{Param.JaBucket}}",
            "root_prefix": "{{Param.JaRootPrefix}}",
        },
    }
    # Reuse the ROP's daemon environment, renamed for PDG.
    (environment,) = get_houdini_environments(
        {
            "name": "initData",
            "filename": "init-data.yaml",
            "type": "TEXT",
            "data": yaml.safe_dump(init_data, default_flow_style=False),
        }
    )
    environment["name"] = _SESSION_ENVIRONMENT
    environment["description"] = "Runs Houdini in the background for every PDG task in the session."
    return environment


def _step(step: StepSpec) -> Dict[str, Any]:
    """One step that runs every task through the session's daemon."""
    # Every step runs the same command. The run data differs only in the step's
    # name and in whether the worker must record what the task wrote.
    dumped: str = yaml.safe_dump(
        {
            "pdg": {
                "step": step.name,
                "task_index": _TASK_INDEX,
                "record_outputs": not step.declares_outputs,
            }
        },
        default_flow_style=False,
    )
    # Unquoted, so the substituted index loads as an int.
    run_data: str = dumped.replace(f"'{_TASK_INDEX}'", _TASK_INDEX)
    definition: Dict[str, Any] = {"name": step.name}
    if step.dependencies:
        definition["dependencies"] = [{"dependsOn": d} for d in step.dependencies]
    # A range expression, not a list: a list caps the step at 1,024 tasks.
    definition["parameterSpace"] = {
        "taskParameterDefinitions": [{"name": "TaskIndex", "type": "INT", "range": step.task_range}]
    }
    definition["script"] = {
        "embeddedFiles": [
            {"name": "runData", "filename": "run-data.yaml", "type": "TEXT", "data": run_data}
        ],
        "actions": {
            "onRun": {
                "command": "houdini-openjd",
                "args": [
                    "daemon",
                    "run",
                    "--connection-file",
                    "{{Session.WorkingDirectory}}/connection.json",
                    "--run-data",
                    "file://{{Task.File.runData}}",
                ],
                "cancelation": {"mode": "NOTIFY_THEN_TERMINATE"},
            }
        },
    }
    return definition


def _wheels_enabled(settings: BaseSubmitterSettings) -> bool:
    # The ROP's rule, so the override and the AdaptorWheels value agree.
    return bool(getattr(settings, "include_adaptor_wheels", False)) and os.path.exists(
        getattr(settings, "adaptor_wheels", "")
    )


def _whole_number(value: Any, parm: str, low: int, high: int) -> int:
    """A parm's value as an int in ``[low, high]``, or a :class:`CookSetupError` naming the parm."""
    number: Optional[int] = _as_whole_number(value)
    if number is None or not low <= number <= high:
        raise CookSetupError(
            f"The '{parm}' parm must be a whole number from {low} to {high}, not {value!r}."
        )
    return number


def _as_whole_number(value: Any) -> Optional[int]:
    """``value`` as an int when it's an int, a whole float, or a numeric string, else None."""
    # A bool is an int in Python, but True is no priority.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _submission_error(error: Exception) -> JobSubmissionError:
    """The error to show for a failed submission. Checked in order, because the remedies differ."""
    # A botocore error carries its code in the response, and others only in the text.
    response: Any = getattr(error, "response", None)
    code: str = str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""
    text: str = f"{code} {type(error).__name__} {error}"
    if "ServiceQuotaExceeded" in text:
        return QuotaExceededError(
            "A Deadline Cloud service quota was exceeded. For a large cook this is most often "
            "tasks per job or steps per job. Request an increase in Service Quotas, or make the "
            f"cook smaller. Detail: {error}"
        )
    if "AccessDenied" in text:
        return JobSubmissionError(
            f"Access to the queue was denied. Check your IAM permissions. Detail: {error}"
        )
    if "ResourceNotFound" in text:
        return JobSubmissionError(
            f"The farm or queue was not found. Check the workstation's Deadline Cloud settings. "
            f"Detail: {error}"
        )
    if "ValidationException" in text:
        return JobSubmissionError(
            "Deadline Cloud rejected the job the scheduler generated. This is a bug in the "
            f"scheduler, not a problem with your graph. Detail: {error}"
        )
    return JobSubmissionError(f"The job could not be submitted: {error}")


def _queue_job_attachments(farm_id: str, queue_id: str, *, client: Any = None) -> Tuple[str, str]:
    """The queue's job attachments bucket and root prefix, where the work item payloads go.

    ``client`` is a Deadline Cloud client, made from the workstation's credentials when absent.
    """
    from deadline.client import api

    # Every work item's payload is stored in the bucket, so a queue without one can't cook.
    deadline: Any = client if client is not None else api.get_boto3_client("deadline")
    queue: Dict[str, Any] = deadline.get_queue(farmId=farm_id, queueId=queue_id)
    settings: Dict[str, Any] = queue.get("jobAttachmentSettings") or {}
    bucket: str = settings.get("s3BucketName", "")
    root_prefix: str = settings.get("rootPrefix", "")
    if not bucket or not root_prefix:
        raise CookSetupError(
            f"Queue {queue_id} has no job attachments bucket. The scheduler stores each "
            "work item's data there, so choose a queue that has one."
        )
    return bucket, root_prefix
