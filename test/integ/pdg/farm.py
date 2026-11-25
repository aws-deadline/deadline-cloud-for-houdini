# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The farm the PDG integ tests use, and the farm's side of one PDG cook.

The tests submit only to the farm and queue that ``PDG_INTEG_FARM_ID`` and
``PDG_INTEG_QUEUE_ID`` name, through a copy of the user's Deadline config. Reading a job
changes nothing: ``read_farm_view`` returns the Deadline Cloud job and the files its tasks
uploaded, and ``compare_cook_with_farm`` compares that with the cook's own record.
``jobs_created_since`` finds the jobs one cook made, which a refused cook must not have.
The one change the tests make on the farm is ``cancel_job``, for a cook test's job that
ran out of time, or a job made by a cook that should have been refused.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    DefaultDict,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    Optional,
    Pattern,
    Tuple,
)

from deadline.client import api
from deadline.client.config import config_file
from deadline.job_attachments.asset_manifests.decode import decode_manifest
from deadline.job_attachments.models import JobAttachmentS3Settings

from deadline.houdini_pdg.ports import OutputFile, TaskAddress
from deadline.houdini_pdg.task_storage import S3TaskStorage

#: The variables naming the farm and queue the tests submit to.
FARM_ID_VARIABLE: str = "PDG_INTEG_FARM_ID"
QUEUE_ID_VARIABLE: str = "PDG_INTEG_QUEUE_ID"

#: The file name of the Deadline config copy that ``write_deadline_config`` writes.
DEADLINE_CONFIG_NAME: str = "deadline_config"

# Job attachments ends the key of every output manifest with this.
_MANIFEST_SUFFIX: str = "_output"

# A session action ID is ``sessionaction-<session>-<n>``, and its session is ``session-<session>``.
_SESSION_ACTION_ID: Pattern[str] = re.compile(r"^sessionaction-(?P<session>.+)-[0-9]+$")

# The job parameters every PDG job carries: the job's name, and the cook's ID.
_JOB_NAME_PARAMETER: str = "CookName"
_COOK_ID_PARAMETER: str = "PdgCookId"

# The most results SearchJobs returns in one page.
_SEARCH_PAGE_SIZE: int = 100


@dataclass(frozen=True)
class FarmQueue:
    """A farm and one of its queues, by ID."""

    farm_id: str
    queue_id: str


def farm_queue_from_env(env: Mapping[str, str] = os.environ) -> Optional[FarmQueue]:
    """``PDG_INTEG_FARM_ID`` and ``PDG_INTEG_QUEUE_ID``, or None when either is unset or empty."""
    # An empty value counts as unset, as an exported but blank variable would be.
    farm_id: str = env.get(FARM_ID_VARIABLE, "")
    queue_id: str = env.get(QUEUE_ID_VARIABLE, "")
    if not farm_id or not queue_id:
        return None
    return FarmQueue(farm_id=farm_id, queue_id=queue_id)


def write_deadline_config(farm_queue: FarmQueue, into: Path) -> Path:
    """A copy of the user's Deadline config with the default farm and queue set to ``farm_queue``.

    hython gets the copy's path through ``DEADLINE_CONFIG_FILE_PATH``, so the cook submits
    to ``farm_queue`` with the user's login, and the user's own config is not changed.
    """
    # The copy starts from the user's file, so it keeps their profile and login settings.
    config: ConfigParser = ConfigParser()
    user_config: Path = config_file.get_config_file_path()
    if user_config.is_file():
        config.read_string(user_config.read_text(encoding="utf-8"))

    # The queue's setting is kept under the farm's, so the farm is set first.
    config_file.set_setting("defaults.farm_id", farm_queue.farm_id, config=config)
    config_file.set_setting("defaults.queue_id", farm_queue.queue_id, config=config)
    copy: Path = into / DEADLINE_CONFIG_NAME
    with copy.open("w", encoding="utf-8") as copy_file:
        config.write(copy_file)
    return copy


def cancel_job(
    farm_queue: FarmQueue, job_id: str, *, deadline_client: Optional[Any] = None
) -> None:
    """Cancel a job this test run created: a cook test's job that ran out of time, or a job
    made by a cook that should have been refused.

    The client defaults to the workstation's Deadline Cloud login. Tests pass their own.
    """
    deadline: Any = deadline_client or api.get_boto3_client("deadline")
    deadline.update_job(
        farmId=farm_queue.farm_id,
        queueId=farm_queue.queue_id,
        jobId=job_id,
        targetTaskRunStatus="CANCELED",
    )


def cancel_job_of_cook(
    farm_queue: FarmQueue, debug_dir: Optional[Path], *, deadline_client: Optional[Any] = None
) -> None:
    """Cancel the job a cook submitted, which the cook.json in its debug directory names.

    A cook with no debug directory, no cook.json, or no job ID in it submitted no job, so
    nothing is canceled. Used only when a cook test times out, so its job doesn't keep running.
    """
    # The scheduler records the job's ID in cook.json when it submits the job.
    cook_json_path: Optional[Path] = debug_dir / "cook.json" if debug_dir is not None else None
    if cook_json_path is None or not cook_json_path.is_file():
        return
    job_id: Optional[str] = json.loads(cook_json_path.read_text(encoding="utf-8")).get("job_id")
    if job_id:
        cancel_job(farm_queue, job_id, deadline_client=deadline_client)


def jobs_created_since(
    farm_queue: FarmQueue,
    since: datetime,
    *,
    cook_id: Optional[str],
    job_name: str,
    deadline_client: Optional[Any] = None,
) -> List[str]:
    """IDs of the jobs on the queue created at or after ``since`` that carry this job name
    (job parameter ``CookName``) or, when given, this cook ID (``PdgCookId``). SearchJobs only.

    A refusal test uses it to show that its cook made no job. The client defaults to the
    workstation's Deadline Cloud login. Tests pass their own.
    """
    deadline: Any = deadline_client or api.get_boto3_client("deadline")

    # A job of this cook carries its name, and its cook ID once the cook has one.
    this_cook: List[Dict[str, Any]] = [_parameter_equals(_JOB_NAME_PARAMETER, job_name)]
    if cook_id:
        this_cook.append(_parameter_equals(_COOK_ID_PARAMETER, cook_id))
    filters: Dict[str, Any] = {
        "operator": "AND",
        "filters": [
            {
                "dateTimeFilter": {
                    "name": "CREATED_AT",
                    "operator": "GREATER_THAN_EQUAL_TO",
                    "dateTime": since,
                }
            },
            {"groupFilter": {"operator": "OR", "filters": this_cook}},
        ],
    }

    # Each page names the offset of the next, and the last page names none.
    job_ids: List[str] = []
    offsets: List[int] = [0]
    for offset in offsets:
        page: Mapping[str, Any] = deadline.search_jobs(
            farmId=farm_queue.farm_id,
            queueIds=[farm_queue.queue_id],
            filterExpressions=filters,
            itemOffset=offset,
            pageSize=_SEARCH_PAGE_SIZE,
        )
        job_ids += [job["jobId"] for job in page["jobs"]]
        following: Optional[int] = page.get("nextItemOffset")
        if following is not None:
            offsets.append(following)
    return job_ids


def _parameter_equals(name: str, value: str) -> Dict[str, Any]:
    """A SearchJobs filter that matches a job whose parameter ``name`` is ``value``."""
    return {"parameterFilter": {"name": name, "operator": "EQUAL", "value": value}}


@dataclass(frozen=True)
class FarmTask:
    """One task of a step, as the farm ended it."""

    task_id: str
    #: SUCCEEDED, FAILED, CANCELED, SUSPENDED, and so on.
    run_status: str
    #: The session that ran the task last, or ``""`` when it never ran.
    session_id: str
    #: How many output manifests the task's sessions uploaded.
    manifest_count: int
    #: Every path in those manifests, made absolute with the manifest's asset root.
    uploaded_files: FrozenSet[str]
    #: The task's output record, or None when it published none.
    output_record: Optional[Tuple[OutputFile, ...]]


@dataclass(frozen=True)
class FarmStep:
    """One step of the job, and its tasks by task index."""

    step_id: str
    task_run_status_counts: Mapping[str, int]
    #: The step's host requirements, or None when the job set none.
    required_capabilities: Optional[Mapping[str, Any]]
    tasks: Mapping[int, FarmTask]


@dataclass(frozen=True)
class FarmView:
    """One job as the farm and S3 hold it, with its steps by name."""

    job_id: str
    task_run_status: str
    #: The cook's working directory, which output records name as ``__PDG_DIR__``.
    pdg_dir: str
    steps: Mapping[str, FarmStep]


def read_farm_view(
    farm_queue: FarmQueue,
    job_id: str,
    *,
    deadline_client: Optional[Any] = None,
    s3_client: Optional[Any] = None,
) -> FarmView:
    """The job, its steps and tasks, and each task's uploaded files and output record. Read-only.

    The clients default to the workstation's Deadline Cloud login and the queue role's
    S3 credentials, as the scheduler uses them. Tests pass their own.
    """
    deadline: Any = deadline_client or api.get_boto3_client("deadline")
    job_ids: Dict[str, str] = {
        "farmId": farm_queue.farm_id,
        "queueId": farm_queue.queue_id,
        "jobId": job_id,
    }
    job: Mapping[str, Any] = deadline.get_job(**job_ids)

    # Output manifests and output records live in the queue's job attachments bucket,
    # which only the queue role can read.
    queue: Mapping[str, Any] = deadline.get_queue(
        farmId=farm_queue.farm_id, queueId=farm_queue.queue_id
    )
    s3: Any = s3_client or _queue_s3_client(deadline, farm_queue, queue["displayName"])
    reader: _StepReader = _StepReader(
        deadline=deadline,
        s3=s3,
        bucket=queue["jobAttachmentSettings"]["s3BucketName"],
        root_prefix=queue["jobAttachmentSettings"]["rootPrefix"],
        job_ids=job_ids,
    )

    steps: Dict[str, FarmStep] = {
        step["name"]: reader.read(step)
        for step in _pages(deadline, "list_steps", "steps", **job_ids)
    }
    return FarmView(
        job_id=job_id,
        task_run_status=job["taskRunStatus"],
        pdg_dir=job["parameters"]["PdgDir"]["path"],
        steps=steps,
    )


class _StepReader:
    """Reads one step's tasks and the files each task uploaded, for one job."""

    def __init__(
        self,
        *,
        deadline: Any,
        s3: Any,
        bucket: str,
        root_prefix: str,
        job_ids: Mapping[str, str],
    ) -> None:
        self._deadline: Any = deadline
        self._s3: Any = s3
        self._bucket: str = bucket
        self._job_ids: Mapping[str, str] = job_ids
        self._s3_settings: JobAttachmentS3Settings = JobAttachmentS3Settings(
            s3BucketName=bucket, rootPrefix=root_prefix
        )
        # The record is read through the scheduler's own reader, so the two can't
        # disagree on its key or format.
        self._storage: S3TaskStorage = S3TaskStorage(
            s3_client=s3, bucket=bucket, root_prefix=root_prefix
        )

    def read(self, step: Mapping[str, Any]) -> FarmStep:
        """The step from ListSteps, with its host requirements and every task."""
        # Every task, by its TaskIndex, with the manifests listed under its folder.
        step_id: str = step["stepId"]
        manifests: Mapping[str, List[str]] = self._manifest_keys_by_task(step_id)
        tasks: Dict[int, FarmTask] = {
            int(task["parameters"]["TaskIndex"]["int"]): self._task(
                step_id, task, manifests.get(task["taskId"], [])
            )
            for task in _pages(
                self._deadline, "list_tasks", "tasks", stepId=step_id, **self._job_ids
            )
        }
        # ListSteps doesn't return host requirements, so they come from GetStep.
        details: Mapping[str, Any] = self._deadline.get_step(stepId=step_id, **self._job_ids)
        return FarmStep(
            step_id=step_id,
            task_run_status_counts=dict(step.get("taskRunStatusCounts", {})),
            required_capabilities=details.get("requiredCapabilities"),
            tasks=tasks,
        )

    def _task(self, step_id: str, task: Mapping[str, Any], manifest_keys: List[str]) -> FarmTask:
        """One task from ListTasks, with the files its manifests list and its output record."""
        address: TaskAddress = TaskAddress(
            farm_id=self._job_ids["farmId"],
            queue_id=self._job_ids["queueId"],
            job_id=self._job_ids["jobId"],
            step_id=step_id,
            task_id=task["taskId"],
        )
        record: Optional[List[OutputFile]] = self._storage.get_outputs(address)
        return FarmTask(
            task_id=task["taskId"],
            run_status=task["runStatus"],
            session_id=_session_id(task.get("latestSessionActionId", "")),
            manifest_count=len(manifest_keys),
            uploaded_files=frozenset(
                path for key in manifest_keys for path in self._manifest_paths(key)
            ),
            output_record=None if record is None else tuple(record),
        )

    def _manifest_keys_by_task(self, step_id: str) -> Mapping[str, List[str]]:
        """Each task's output manifest keys under the step's prefix, by task ID."""
        prefix: str = (
            self._s3_settings.full_step_output_prefix(
                self._job_ids["farmId"], self._job_ids["queueId"], self._job_ids["jobId"], step_id
            )
            + "/"
        )
        # A manifest key is <prefix><task id>/<time>_<session action>/<hash>_output. The
        # task's folder also holds its work item payload and output record.
        by_task: DefaultDict[str, List[str]] = defaultdict(list)
        for entry in _pages(
            self._s3, "list_objects_v2", "Contents", Bucket=self._bucket, Prefix=prefix
        ):
            key: str = entry["Key"]
            if key.endswith(_MANIFEST_SUFFIX):
                by_task[key[len(prefix) :].split("/")[0]].append(key)
        return by_task

    def _manifest_paths(self, key: str) -> List[str]:
        """Every path one output manifest lists, made absolute with its asset root."""
        # The manifest lists paths relative to its asset root, which is in the object's metadata.
        response: Mapping[str, Any] = self._s3.get_object(Bucket=self._bucket, Key=key)
        manifest_text: str = response["Body"].read().decode("utf-8")
        root: str = _asset_root(response["Metadata"])
        return [f"{root}/{entry.path}" for entry in decode_manifest(manifest_text).paths]


def _queue_s3_client(deadline: Any, farm_queue: FarmQueue, queue_name: str) -> Any:
    """An S3 client with the queue role's credentials."""
    session: Any = api.get_queue_user_boto3_session(
        deadline=deadline,
        farm_id=farm_queue.farm_id,
        queue_id=farm_queue.queue_id,
        queue_display_name=queue_name,
    )
    return session.client("s3")


def _pages(client: Any, operation: str, key: str, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Every result of a paginated call. A page with no results may omit ``key``."""
    for page in client.get_paginator(operation).paginate(**kwargs):
        yield from page.get(key, [])


def _asset_root(metadata: Mapping[str, str]) -> str:
    """The workstation folder a manifest's paths are relative to."""
    # Job attachments stores a non-ASCII root as JSON under its own key, because S3
    # metadata must be ASCII.
    if "asset-root-json" in metadata:
        return str(json.loads(metadata["asset-root-json"]))
    return metadata["asset-root"]


def _session_id(session_action_id: str) -> str:
    """``session-<id>`` for ``sessionaction-<id>-<n>``, or ``""`` for a task that never ran."""
    match: Optional[re.Match[str]] = _SESSION_ACTION_ID.match(session_action_id)
    return f"session-{match.group('session')}" if match else ""
