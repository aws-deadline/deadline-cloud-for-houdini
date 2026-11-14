# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the farm module: the test farm and queue, and reading a job for the comparison.

``read_farm_view``'s Deadline Cloud and S3 clients are fakes that answer from reads
recorded on a real job (``recorded_farm``). ``cancel_job``'s client records its calls.
Nothing else is faked.
"""

from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from deadline.client.config import config_file

from deadline.houdini_pdg.ports import OutputFile
from test.integ.pdg.farm import (
    FarmQueue,
    FarmStep,
    FarmTask,
    FarmView,
    cancel_job,
    cancel_job_of_cook,
    farm_queue_from_env,
    read_farm_view,
    write_deadline_config,
)

from .recorded_farm import (
    FakeDeadline,
    FakeS3,
    RECORDED_JOB_ID,
    RECORDED_QUEUE,
    read_recorded_farm_view,
    recorded_reads,
)

ROOT: str = "/projects/fx/shots/bonfire"


def _step_id(reads: Dict[str, Any], name: str) -> str:
    """The recorded step ID of the step with this name."""
    return next(s["stepId"] for s in reads["list_steps"] if s["name"] == name)


def _manifests_of(reads: Dict[str, Any], task_id: str) -> List[Dict[str, Any]]:
    """The recorded output manifests of one task."""
    return [
        o
        for o in reads["s3_objects"]
        if f"/{task_id}/" in o["Key"] and o["Key"].endswith("_output")
    ]


def test_the_recorded_job_reads_as_its_steps_tasks_and_uploads() -> None:
    """The view holds the job's status and working directory, every step by name, and for each
    task its status, session, manifests, absolute uploaded paths and output record.

    These values were checked by hand against the real job. A wrong prefix, key split, or
    asset root would show here as a missing or relative path.
    """
    view: FarmView = read_recorded_farm_view()

    assert view.job_id == RECORDED_JOB_ID
    assert view.task_run_status == "SUCCEEDED"
    assert view.pdg_dir == ROOT
    assert sorted(view.steps) == sorted(
        [
            "ropfetch_pyro_cache",
            "ropfetch_sparks_cache",
            "ropfetch_karma",
            "overlaytext_pyro_bonfire_ropfetch1",
            "mosaic_bonfire",
            "bonfire_montage_video",
        ]
    )

    # A step that declares its outputs: one manifest per task, no output record.
    karma: FarmStep = view.steps["ropfetch_karma"]
    assert karma.step_id == "step-exstep00000000000000000000000004"
    assert karma.task_run_status_counts["SUCCEEDED"] == 6
    assert karma.required_capabilities is None
    assert sorted(karma.tasks) == [0, 1, 2, 3, 4, 5]
    assert karma.tasks[0] == FarmTask(
        task_id="task-exstep00000000000000000000000004-0",
        run_status="SUCCEEDED",
        session_id="session-exsession00000000000000000000013",
        manifest_count=1,
        uploaded_files=frozenset(
            {
                f"{ROOT}/pdgtemp/data/ropfetch_karma_33.json.gz",
                f"{ROOT}/render/karma_bonfire/wedge0/karma_bonfire.w0.0001.png",
            }
        ),
        output_record=None,
    )

    # A step that declares no outputs publishes its record, with PDG's token in the path.
    video: FarmTask = view.steps["bonfire_montage_video"].tasks[0]
    assert video.output_record == (
        OutputFile("__PDG_DIR__/video/bonfire_montage_video.mp4", "file/video"),
    )
    assert f"{ROOT}/video/bonfire_montage_video.mp4" in video.uploaded_files


def test_reading_one_result_per_page_gives_the_same_view() -> None:
    """Every page of ListSteps, ListTasks and ListObjectsV2 is read.

    A job with more than 100 tasks in a step, or 1,000 objects under one, spans pages, and
    reading only the first would drop tasks and files.
    """
    reads: Dict[str, Any] = recorded_reads()
    paged: FarmView = read_farm_view(
        RECORDED_QUEUE,
        RECORDED_JOB_ID,
        deadline_client=FakeDeadline(reads, page_size=1),
        s3_client=FakeS3(reads["s3_objects"], page_size=1),
    )
    assert paged == read_recorded_farm_view()


def test_tasks_are_keyed_by_their_task_index_not_by_listing_order() -> None:
    """ListTasks returns tasks in no promised order, so each is keyed by its TaskIndex.

    The comparison joins task i to the snapshot's ``items[i]``, so a task under the wrong
    index would be compared with another work item.
    """
    # ListTasks returns one step's tasks last to first.
    reads: Dict[str, Any] = recorded_reads()
    step_id: str = _step_id(reads, "ropfetch_karma")
    reads["list_tasks"][step_id].reverse()

    tasks: Dict[int, FarmTask] = dict(read_recorded_farm_view(reads).steps["ropfetch_karma"].tasks)
    assert tasks[0].task_id.endswith("-0")
    assert tasks[5].task_id.endswith("-5")


def test_a_retried_task_counts_every_manifest_and_lists_every_file() -> None:
    """A task that ran twice uploaded a manifest from each session, and both count.

    Keeping only one would report a file the first run uploaded as missing.
    """
    # A second session of the task uploaded a manifest with one more file.
    reads: Dict[str, Any] = recorded_reads()
    task_id: str = "task-exstep00000000000000000000000004-0"
    first: Dict[str, Any] = _manifests_of(reads, task_id)[0]
    retry: Dict[str, Any] = dict(first)
    retry["Key"] = first["Key"].replace("2026-11-10T", "2026-11-11T")
    retry["Body"] = json.dumps(
        {
            "hashAlg": "xxh128",
            "manifestVersion": "2023-03-03",
            "paths": [
                {"hash": "0" * 32, "mtime": 1, "path": "shots/bonfire/render/extra.png", "size": 1}
            ],
            "totalSize": 1,
        }
    )
    reads["s3_objects"].append(retry)

    task: FarmTask = read_recorded_farm_view(reads).steps["ropfetch_karma"].tasks[0]
    assert task.manifest_count == 2
    assert f"{ROOT}/render/extra.png" in task.uploaded_files
    assert f"{ROOT}/render/karma_bonfire/wedge0/karma_bonfire.w0.0001.png" in task.uploaded_files


def test_a_step_that_uploaded_nothing_has_tasks_with_no_files() -> None:
    """A step with no objects in S3 reads as tasks with no manifest, no files and no record.

    S3 omits the result list from an empty listing, which must not be read as an error.
    """
    # Every S3 object of one step is gone.
    reads: Dict[str, Any] = recorded_reads()
    step_id: str = _step_id(reads, "bonfire_montage_video")
    reads["s3_objects"] = [o for o in reads["s3_objects"] if f"/{step_id}/" not in o["Key"]]

    task: FarmTask = read_recorded_farm_view(reads).steps["bonfire_montage_video"].tasks[0]
    assert (task.manifest_count, task.uploaded_files, task.output_record) == (0, frozenset(), None)


def test_a_non_ascii_asset_root_is_read_from_its_json_key() -> None:
    """Job attachments stores a non-ASCII root JSON-encoded under ``asset-root-json``.

    Reading only ``asset-root`` would fail on such a manifest.
    """
    # One manifest's root holds a non-ASCII character.
    reads: Dict[str, Any] = recorded_reads()
    manifest: Dict[str, Any] = _manifests_of(reads, "task-exstep00000000000000000000000004-0")[0]
    manifest["Metadata"] = {"asset-root-json": json.dumps("/projects/caf\u00e9", ensure_ascii=True)}

    task: FarmTask = read_recorded_farm_view(reads).steps["ropfetch_karma"].tasks[0]
    assert (
        "/projects/caf\u00e9/shots/bonfire/render/karma_bonfire/wedge0/karma_bonfire.w0.0001.png"
        in task.uploaded_files
    )


def test_a_task_that_never_ran_has_no_session() -> None:
    """A task with no session action, such as one still held back, has an empty session.

    The session count in the comparison must not group such tasks into one session.
    """
    # One task has no session action.
    reads: Dict[str, Any] = recorded_reads()
    step_id: str = _step_id(reads, "mosaic_bonfire")
    del reads["list_tasks"][step_id][0]["latestSessionActionId"]

    assert read_recorded_farm_view(reads).steps["mosaic_bonfire"].tasks[0].session_id == ""


def test_a_steps_host_requirements_come_from_get_step() -> None:
    """ListSteps doesn't return a step's host requirements, so they are read with GetStep."""
    # One step's GetStep response carries host requirements.
    reads: Dict[str, Any] = recorded_reads()
    step_id: str = _step_id(reads, "mosaic_bonfire")
    required: Dict[str, Any] = {
        "attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux"]}]
    }
    reads["get_step"][step_id]["requiredCapabilities"] = required

    view: FarmView = read_recorded_farm_view(reads)
    assert view.steps["mosaic_bonfire"].required_capabilities == required
    assert view.steps["ropfetch_karma"].required_capabilities is None


def test_the_farm_and_queue_come_from_the_two_variables() -> None:
    """The tests submit only where PDG_INTEG_FARM_ID and PDG_INTEG_QUEUE_ID say."""
    env: Dict[str, str] = {"PDG_INTEG_FARM_ID": "farm-a", "PDG_INTEG_QUEUE_ID": "queue-b"}
    assert farm_queue_from_env(env) == FarmQueue(farm_id="farm-a", queue_id="queue-b")


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="neither"),
        pytest.param({"PDG_INTEG_FARM_ID": "farm-a"}, id="no_queue"),
        pytest.param({"PDG_INTEG_QUEUE_ID": "queue-b"}, id="no_farm"),
        pytest.param({"PDG_INTEG_FARM_ID": "", "PDG_INTEG_QUEUE_ID": "queue-b"}, id="empty_farm"),
        pytest.param({"PDG_INTEG_FARM_ID": "farm-a", "PDG_INTEG_QUEUE_ID": ""}, id="empty_queue"),
    ],
)
def test_without_both_variables_there_is_no_farm(env: Dict[str, str]) -> None:
    """Either one missing or empty means no farm, so the tests skip instead of guessing one."""
    assert farm_queue_from_env(env) is None


# A user's Deadline config, with a profile, its own default farm and queue, and other settings.
_USER_CONFIG: str = """\
[defaults]
aws_profile_name = studio

[profile-studio defaults]
farm_id = farm-users

[profile-studio farm-users defaults]
queue_id = queue-users

[settings]
auto_accept = true
"""


def test_the_copied_deadline_config_points_at_the_test_farm_and_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hython reads the copy, so the cook submits to the test queue with the user's login.

    The user's file is left as it was, so their own default queue doesn't change.
    """
    user_config: Path = tmp_path / "user_config"
    user_config.write_text(_USER_CONFIG, encoding="utf-8")
    monkeypatch.setenv("DEADLINE_CONFIG_FILE_PATH", str(user_config))
    into: Path = tmp_path / "copy"
    into.mkdir()

    copy: Path = write_deadline_config(FarmQueue("farm-tests", "queue-tests"), into)

    # The copy as the client library reads it in hython.
    copied: ConfigParser = ConfigParser()
    copied.read_string(copy.read_text(encoding="utf-8"))
    assert config_file.get_setting("defaults.farm_id", config=copied) == "farm-tests"
    assert config_file.get_setting("defaults.queue_id", config=copied) == "queue-tests"
    assert config_file.get_setting("defaults.aws_profile_name", config=copied) == "studio"
    assert config_file.get_setting("settings.auto_accept", config=copied) == "true"
    assert user_config.read_text(encoding="utf-8") == _USER_CONFIG


def test_a_user_with_no_deadline_config_gets_one_with_only_the_test_farm_and_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workstation that never ran the Deadline config still gets a config for the test queue."""
    # The user's config path names no file.
    monkeypatch.setenv("DEADLINE_CONFIG_FILE_PATH", str(tmp_path / "missing"))
    copy: Path = write_deadline_config(FarmQueue("farm-tests", "queue-tests"), tmp_path)

    # The copy holds the test farm and queue, and no file was made at the user's path.
    copied: ConfigParser = ConfigParser()
    copied.read_string(copy.read_text(encoding="utf-8"))
    assert config_file.get_setting("defaults.farm_id", config=copied) == "farm-tests"
    assert config_file.get_setting("defaults.queue_id", config=copied) == "queue-tests"
    assert not (tmp_path / "missing").exists()


class _RecordingDeadline:
    """A Deadline Cloud client that records UpdateJob calls."""

    def __init__(self) -> None:
        self.updates: List[Dict[str, str]] = []

    def update_job(self, **kwargs: str) -> Dict[str, Any]:
        """Record the update."""
        self.updates.append(kwargs)
        return {}


def test_canceling_a_job_cancels_only_that_job_on_the_test_queue() -> None:
    """A timed-out cook's job is canceled, so it doesn't keep running on the farm."""
    deadline: _RecordingDeadline = _RecordingDeadline()
    cancel_job(FarmQueue("farm-tests", "queue-tests"), "job-hung", deadline_client=deadline)
    assert deadline.updates == [
        {
            "farmId": "farm-tests",
            "queueId": "queue-tests",
            "jobId": "job-hung",
            "targetTaskRunStatus": "CANCELED",
        }
    ]


def test_a_cooks_job_is_canceled_by_the_id_in_its_cook_json(tmp_path: Path) -> None:
    """A timed-out cook that submitted a job has that job canceled, and no other.

    Without this, a hung cook's job would keep using the farm after the test failed.
    """
    # The debug directory of a cook that submitted job-hung.
    (tmp_path / "cook.json").write_text(
        json.dumps({"outcome": None, "job_id": "job-hung"}), encoding="utf-8"
    )
    deadline: _RecordingDeadline = _RecordingDeadline()

    cancel_job_of_cook(FarmQueue("farm-tests", "queue-tests"), tmp_path, deadline_client=deadline)

    assert deadline.updates == [
        {
            "farmId": "farm-tests",
            "queueId": "queue-tests",
            "jobId": "job-hung",
            "targetTaskRunStatus": "CANCELED",
        }
    ]


@pytest.mark.parametrize(
    "cook_json",
    [
        pytest.param(None, id="no_debug_directory"),
        pytest.param("", id="no_cook_json"),
        pytest.param(json.dumps({"outcome": None}), id="no_job_id"),
        pytest.param(json.dumps({"outcome": None, "job_id": None}), id="null_job_id"),
    ],
)
def test_a_cook_that_submitted_no_job_cancels_nothing(
    tmp_path: Path, cook_json: Optional[str]
) -> None:
    """A cook that hung before it submitted a job has nothing to cancel, and doesn't fail on it.

    ``None`` stands for no debug directory, and ``""`` for a debug directory without cook.json.
    """
    # The cook's debug directory, as far as the cook got.
    debug_dir: Optional[Path] = None if cook_json is None else tmp_path
    if cook_json:
        (tmp_path / "cook.json").write_text(cook_json, encoding="utf-8")
    deadline: _RecordingDeadline = _RecordingDeadline()

    cancel_job_of_cook(FarmQueue("farm-tests", "queue-tests"), debug_dir, deadline_client=deadline)

    assert deadline.updates == []
