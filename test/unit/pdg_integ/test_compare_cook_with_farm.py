# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for comparing a PDG cook with its job on the farm.

Each test starts from a real 26-task cook that succeeded, with its debug directory and
farm reads recorded (``recorded_farm``), and changes one thing the way a bug would. The
farm side is read through ``read_farm_view`` with fake clients. Nothing else is faked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from configparser import ConfigParser
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

import pytest

from deadline.client.config import config_file
from deadline.houdini_pdg.ports import OutputFile
from test.integ.pdg.compare_cook_with_farm import (
    CookFarmComparison,
    CookRecord,
    Difference,
    DifferenceType,
    PdgWorkItem,
    RanBeforeRelease,
    compare_cook_with_farm,
    format_comparison,
    main,
    read_cook_record,
)
from test.integ.pdg.farm import FarmQueue, FarmStep, FarmTask, FarmView

from .recorded_farm import (
    PYRO_BONFIRE_DIR,
    RECORDED_JOB_ID,
    RECORDED_QUEUE,
    read_recorded_farm_view,
    recorded_pdg_work_items,
)

# The folder the hatch script runs the command from.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

# The recorded cook's working directory, and two of its outputs.
ROOT: str = "/projects/fx/shots/bonfire"
VIDEO: str = f"{ROOT}/video/bonfire_montage_video.mp4"
KARMA_0_RENDER: str = f"{ROOT}/render/karma_bonfire/wedge0/karma_bonfire.w0.0001.png"

# What a test of the host requirements tab expects on every step.
HOST_REQUIREMENTS: Dict[str, Any] = {
    "attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux"]}]
}


# -- helpers --------------------------------------------------------------------


def _cook(**cook_json: Any) -> CookRecord:
    """The recorded cook's record, with ``cook_json`` keys overridden."""
    record: CookRecord = read_cook_record(PYRO_BONFIRE_DIR)
    return replace(record, cook_json={**record.cook_json, **cook_json})


def _with_task(farm: FarmView, step: str, task_index: int, **changes: Any) -> FarmView:
    """The farm view with one task's fields changed."""
    farm_step: FarmStep = farm.steps[step]
    tasks: Dict[int, FarmTask] = dict(farm_step.tasks)
    tasks[task_index] = replace(tasks[task_index], **changes)
    return _with_step(farm, step, tasks=tasks)


def _with_step(farm: FarmView, step: str, **changes: Any) -> FarmView:
    """The farm view with one step's fields changed."""
    steps: Dict[str, FarmStep] = dict(farm.steps)
    steps[step] = replace(steps[step], **changes)
    return replace(farm, steps=steps)


def _with_every_step(farm: FarmView, **changes: Any) -> FarmView:
    """The farm view with the same fields changed on every step."""
    return replace(farm, steps={name: replace(s, **changes) for name, s in farm.steps.items()})


def _with_item(items: Sequence[PdgWorkItem], name: str, **changes: Any) -> List[PdgWorkItem]:
    """PDG's work items with one item's fields changed."""
    return [replace(w, **changes) if w.name == name else w for w in items]


def _task_differences(comparison: CookFarmComparison) -> List[Difference]:
    """The differences about one task, leaving out the job's and the totals'."""
    return [d for d in comparison.differences if d.task_index is not None]


def _types(differences: Sequence[Difference]) -> List[DifferenceType]:
    """The type of each difference, in order."""
    return [d.difference_type for d in differences]


def _debug_dir_copy(tmp_path: Path) -> Path:
    """A writable copy of the recorded debug directory."""
    debug_dir: Path = tmp_path / "2026-11-10-17-09-52-01234567"
    shutil.copytree(PYRO_BONFIRE_DIR, debug_dir)
    return debug_dir


def _edit_json(path: Path, **changes: Any) -> None:
    """Change top-level keys of a JSON file; a value of None removes the key."""
    content: Dict[str, Any] = json.loads(path.read_text())
    for key, value in changes.items():
        if value is None:
            content.pop(key, None)
        else:
            content[key] = value
    path.write_text(json.dumps(content))


# -- the whole cook -------------------------------------------------------------


def test_a_clean_cook_has_no_differences() -> None:
    """The recorded cook, compared with its job and PDG's work items, has no difference.

    This is the baseline every other test changes. A false difference here would fail
    every real cook test.
    """
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), read_recorded_farm_view(), pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.differences == ()
    assert comparison.passed
    assert comparison.job_id == RECORDED_JOB_ID
    assert comparison.used_pdg_work_items
    # Measured on the real job: 24 sessions, none of which ran tasks of two steps.
    assert comparison.observations == {
        "sessions_that_ran_more_than_one_step": 0,
        "tasks": 26,
        "steps": 6,
    }


def test_a_clean_cook_compared_without_pdg_work_items_has_no_differences() -> None:
    """Run by hand on a debug directory, without PDG's work items, the clean cook passes too,
    and the result says the per-task status checks were not run."""
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), read_recorded_farm_view())
    assert comparison.passed
    assert not comparison.used_pdg_work_items


def test_a_cook_that_did_not_end_done_is_a_difference() -> None:
    """A cook that ended in any phase but Done didn't finish, whatever the farm did."""
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(final_phase="Draining"), read_recorded_farm_view()
    )
    assert comparison.differences == (
        Difference(DifferenceType.COOK_NOT_DONE, cook_says="Draining"),
    )


def test_a_cook_whose_record_was_never_closed_is_a_difference() -> None:
    """A cook that died before it closed leaves no phase or counts in ``cook.json``.

    That must show as a cook that didn't finish, not stop the comparison.
    """
    record: CookRecord = read_cook_record(PYRO_BONFIRE_DIR)
    unclosed: Dict[str, Any] = {
        k: v
        for k, v in record.cook_json.items()
        if k not in ("final_phase", "succeeded", "failed", "canceled", "stopped_by_user")
    }
    comparison: CookFarmComparison = compare_cook_with_farm(
        replace(record, cook_json=unclosed),
        read_recorded_farm_view(),
        pdg_work_items=recorded_pdg_work_items(),
    )
    # A missing count is read as zero.
    assert comparison.differences == (
        Difference(DifferenceType.COOK_NOT_DONE),
        Difference(DifferenceType.STATUS_COUNTS, cook_says="0 succeeded", farm_says="26 SUCCEEDED"),
        Difference(
            DifferenceType.STATUS_COUNTS,
            cook_says="0 succeeded",
            farm_says="PDG has 26 CookedSuccess",
        ),
    )


def test_a_job_that_did_not_succeed_is_a_difference() -> None:
    """A job the farm doesn't show as SUCCEEDED is a difference even when every task matches."""
    farm: FarmView = replace(read_recorded_farm_view(), task_run_status="RUNNING")
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm)
    assert comparison.differences == (
        Difference(DifferenceType.JOB_NOT_SUCCEEDED, farm_says="RUNNING"),
    )


def test_every_check_runs_after_an_earlier_one_finds_a_difference(tmp_path: Path) -> None:
    """One run reports every difference, so a cook with three problems shows all three."""
    # The cook failed, the job failed, and the log shows a task the farm ran too early.
    debug_dir: Path = _debug_dir_copy(tmp_path)
    _edit_json(debug_dir / "cook.json", final_phase="Failed")
    with open(debug_dir / "orchestrator.log", "a", encoding="utf-8") as log:
        log.write(
            "[2026-11-10T17:15:00+00:00] Task mosaic_bonfire[0] reached RUNNING, but the farm "
            "ran it before the Deadline Cloud Scheduler node started it. It will run again.\n"
        )
    farm: FarmView = replace(read_recorded_farm_view(), task_run_status="FAILED")

    comparison: CookFarmComparison = compare_cook_with_farm(read_cook_record(debug_dir), farm)
    assert _types(comparison.differences) == [
        DifferenceType.COOK_NOT_DONE,
        DifferenceType.JOB_NOT_SUCCEEDED,
        DifferenceType.RAN_BEFORE_RELEASE,
    ]


# -- the job's shape ------------------------------------------------------------


def test_a_step_on_only_one_side_is_a_difference_and_its_tasks_are_not_compared() -> None:
    """A snapshot step the farm lacks, and a farm step the snapshot lacks, are each reported.

    The job then describes another graph. Its tasks can't be joined to work items, so
    they get no per-task differences of their own.
    """
    # The farm has the mosaic step under another name.
    recorded: FarmView = read_recorded_farm_view()
    steps: Dict[str, FarmStep] = dict(recorded.steps)
    steps["mosaic"] = steps.pop("mosaic_bonfire")
    farm: FarmView = replace(recorded, steps=steps)

    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.differences == (
        Difference(
            DifferenceType.STEP_MISSING,
            step="mosaic_bonfire",
            cook_says="1 task(s)",
            farm_says="no such step",
        ),
        Difference(
            DifferenceType.STEP_MISSING,
            step="mosaic",
            cook_says="no such step",
            farm_says="1 task(s)",
        ),
    )
    assert comparison.observations["tasks"] == 25


def test_a_step_with_fewer_tasks_on_the_farm_is_one_difference() -> None:
    """A step whose job has fewer tasks than the snapshot is a task count difference.

    The missing task gets no row, so it isn't reported a second time.
    """
    # The farm has no task 5 in a six-task step.
    recorded: FarmView = read_recorded_farm_view()
    tasks: Dict[int, FarmTask] = dict(recorded.steps["ropfetch_karma"].tasks)
    del tasks[5]
    farm: FarmView = _with_step(recorded, "ropfetch_karma", tasks=tasks)

    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.differences == (
        Difference(DifferenceType.TASK_COUNT, step="ropfetch_karma", cook_says="6", farm_says="5"),
    )


def test_steps_without_the_expected_host_requirements_are_differences() -> None:
    """When a test expects host requirements, every step that doesn't hold them is reported.

    The recorded job set none, so all six steps lack them.
    """
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), read_recorded_farm_view(), expected_host_requirements=HOST_REQUIREMENTS
    )
    assert _types(comparison.differences) == [DifferenceType.HOST_REQUIREMENTS] * 6
    assert comparison.differences[0].farm_says == "null"


def test_steps_that_hold_the_expected_host_requirements_and_more_are_not_a_difference() -> None:
    """A step may carry more requirements than the test names, such as an amount, and still
    pass. Only a missing requirement is a difference."""
    # Every step requires Linux on x86_64 with 4 vCPUs.
    required: Dict[str, Any] = {
        "attributes": [
            {"name": "attr.worker.cpu.arch", "anyOf": ["x86_64"]},
            {"name": "attr.worker.os.family", "anyOf": ["linux"]},
        ],
        "amounts": [{"name": "amount.worker.vcpu", "min": 4}],
    }
    farm: FarmView = _with_every_step(read_recorded_farm_view(), required_capabilities=required)

    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, expected_host_requirements=HOST_REQUIREMENTS
    )
    assert comparison.passed


def test_a_step_with_another_os_than_expected_is_a_difference() -> None:
    """A step that requires Windows workers doesn't hold a Linux requirement."""
    windows: Dict[str, Any] = {
        "attributes": [{"name": "attr.worker.os.family", "anyOf": ["windows"]}]
    }
    # Every step requires Linux workers except one, which requires Windows workers.
    farm: FarmView = _with_step(
        _with_every_step(read_recorded_farm_view(), required_capabilities=HOST_REQUIREMENTS),
        "mosaic_bonfire",
        required_capabilities=windows,
    )

    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, expected_host_requirements=HOST_REQUIREMENTS
    )
    assert [(d.difference_type, d.step) for d in comparison.differences] == [
        (DifferenceType.HOST_REQUIREMENTS, "mosaic_bonfire")
    ]


# -- each task's status ---------------------------------------------------------


def test_a_task_whose_work_item_pdg_does_not_have_is_a_difference() -> None:
    """The snapshot names a work item PDG doesn't have, so the job was built from a graph
    other than the one PDG cooked."""
    items: List[PdgWorkItem] = [
        w for w in recorded_pdg_work_items() if w.name != "ropfetch_karma_37"
    ]
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), read_recorded_farm_view(), pdg_work_items=items
    )
    assert _task_differences(comparison) == [
        Difference(
            DifferenceType.WORK_ITEM_MISSING,
            step="ropfetch_karma",
            task_index=4,
            work_item="ropfetch_karma_37",
            cook_says="no such work item",
            farm_says="SUCCEEDED",
        )
    ]


def test_a_task_the_farm_ran_that_pdg_was_never_told_about_is_a_difference() -> None:
    """A task that succeeded on the farm while its work item is still waiting in PDG is a
    lost result. The cook's success count then disagrees with PDG's too."""
    # PDG still has the work item behind task 4 waiting for a result.
    items: List[PdgWorkItem] = _with_item(
        recorded_pdg_work_items(), "ropfetch_karma_37", state="Scheduled"
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), read_recorded_farm_view(), pdg_work_items=items
    )
    assert _task_differences(comparison) == [
        Difference(
            DifferenceType.RUN_BUT_NOT_REPORTED,
            step="ropfetch_karma",
            task_index=4,
            work_item="ropfetch_karma_37",
            cook_says="Scheduled",
            farm_says="SUCCEEDED",
        )
    ]
    assert (
        Difference(
            DifferenceType.STATUS_COUNTS,
            cook_says="26 succeeded",
            farm_says="PDG has 25 CookedSuccess",
        )
        in comparison.differences
    )


def test_a_work_item_reported_as_succeeded_whose_task_did_not_succeed_is_a_difference() -> None:
    """PDG was told about work the farm didn't finish: the task is still suspended."""
    farm: FarmView = _with_task(
        read_recorded_farm_view(), "ropfetch_karma", 4, run_status="SUSPENDED"
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, pdg_work_items=recorded_pdg_work_items()
    )
    assert _task_differences(comparison) == [
        Difference(
            DifferenceType.REPORTED_BUT_NOT_RUN,
            step="ropfetch_karma",
            task_index=4,
            work_item="ropfetch_karma_37",
            cook_says="CookedSuccess",
            farm_says="SUSPENDED",
        )
    ]


def _canceled_and_failed(job_status: str) -> Tuple[FarmView, List[PdgWorkItem]]:
    """The last step's task canceled on the farm, and its work item failed in PDG."""
    farm: FarmView = replace(
        _with_task(read_recorded_farm_view(), "bonfire_montage_video", 0, run_status="CANCELED"),
        task_run_status=job_status,
    )
    items: List[PdgWorkItem] = _with_item(
        recorded_pdg_work_items(), "bonfire_montage_video_48", state="CookedFail"
    )
    return farm, items


def test_a_canceled_task_reported_as_a_failure_is_a_difference() -> None:
    """A task the farm canceled on its own, while the job could still finish, is not a failure.

    Reporting it to PDG as failed fails the artist's cook for work that never ran.
    """
    farm, items = _canceled_and_failed(job_status="CANCELED")
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm, pdg_work_items=items)
    assert _types(_task_differences(comparison)) == [DifferenceType.CANCELED_REPORTED_AS_FAILED]


def test_canceled_tasks_failed_after_the_job_failed_are_not_a_difference() -> None:
    """Once the job has failed it can't finish, so the cook fails every work item it has no
    result for, and a canceled task's work item is rightly failed."""
    farm, items = _canceled_and_failed(job_status="FAILED")
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm, pdg_work_items=items)
    assert _task_differences(comparison) == []


def test_canceled_tasks_failed_after_the_artist_stopped_the_cook_are_not_a_difference() -> None:
    """When the artist stops the cook, every outstanding work item is failed, and that's right."""
    farm, items = _canceled_and_failed(job_status="CANCELED")
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(stopped_by_user=True), farm, pdg_work_items=items
    )
    assert _task_differences(comparison) == []


def test_a_failure_both_sides_agree_on_is_not_a_task_difference() -> None:
    """A task that failed on the farm, with its work item failed in PDG, is not a per-task
    difference. The job check reports the job not succeeding."""
    # The last task failed on the farm, so the job failed, and PDG was told so.
    farm: FarmView = replace(
        _with_task(read_recorded_farm_view(), "bonfire_montage_video", 0, run_status="FAILED"),
        task_run_status="FAILED",
    )
    items: List[PdgWorkItem] = _with_item(
        recorded_pdg_work_items(), "bonfire_montage_video_48", state="CookedFail"
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(succeeded=25, failed=1), farm, pdg_work_items=items
    )
    assert _task_differences(comparison) == []
    assert DifferenceType.JOB_NOT_SUCCEEDED in _types(comparison.differences)


def test_an_unfinished_task_whose_work_item_pdg_still_waits_on_is_not_a_task_difference() -> None:
    """A task the farm hasn't finished, with its work item still waiting in PDG, is the two
    sides agreeing: nothing was reported for work that didn't happen. Reporting it would fail
    every cook that is stopped while tasks are suspended or queued. The job check reports the
    job not succeeding."""
    # Task 4 is suspended on the farm, so the job is too, and PDG still waits on its work item.
    farm: FarmView = replace(
        _with_task(read_recorded_farm_view(), "ropfetch_karma", 4, run_status="SUSPENDED"),
        task_run_status="SUSPENDED",
    )
    items: List[PdgWorkItem] = _with_item(
        recorded_pdg_work_items(), "ropfetch_karma_37", state="Scheduled"
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(succeeded=25), farm, pdg_work_items=items
    )
    assert _task_differences(comparison) == []
    assert DifferenceType.JOB_NOT_SUCCEEDED in _types(comparison.differences)


def test_task_statuses_are_not_compared_without_pdg_work_items() -> None:
    """Without PDG's work items, a task's status can't be checked against its work item, so a
    suspended task alone is no per-task difference."""
    farm: FarmView = _with_task(
        read_recorded_farm_view(), "ropfetch_karma", 4, run_status="SUSPENDED"
    )
    assert _task_differences(compare_cook_with_farm(_cook(), farm)) == []


# -- which work item each task ran ----------------------------------------------


def test_a_task_whose_manifest_names_another_work_item_is_a_difference() -> None:
    """A task that uploaded another work item's data file ran that work item's payload.

    Here tasks 0 and 1 of a step swapped their uploads.
    """
    # Swap the uploads of tasks 0 and 1.
    recorded: FarmView = read_recorded_farm_view()
    karma: FarmStep = recorded.steps["ropfetch_karma"]
    farm: FarmView = _with_task(
        _with_task(recorded, "ropfetch_karma", 0, uploaded_files=karma.tasks[1].uploaded_files),
        "ropfetch_karma",
        1,
        uploaded_files=karma.tasks[0].uploaded_files,
    )
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm)
    assert comparison.differences == (
        Difference(
            DifferenceType.RAN_WRONG_WORK_ITEM,
            step="ropfetch_karma",
            task_index=0,
            work_item="ropfetch_karma_33",
            cook_says="ropfetch_karma_33",
            farm_says="ropfetch_karma_34",
        ),
        Difference(
            DifferenceType.RAN_WRONG_WORK_ITEM,
            step="ropfetch_karma",
            task_index=1,
            work_item="ropfetch_karma_34",
            cook_says="ropfetch_karma_34",
            farm_says="ropfetch_karma_33",
        ),
    )


def test_a_task_without_a_work_item_data_file_is_not_a_difference() -> None:
    """PDG's job scripts write the data file, not the scheduler, so a task may lack it."""
    farm: FarmView = _with_task(
        read_recorded_farm_view(), "ropfetch_karma", 0, uploaded_files=frozenset({KARMA_0_RENDER})
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.passed


# -- each succeeded task's files ------------------------------------------------


def test_a_succeeded_task_with_no_output_manifest_is_a_difference() -> None:
    """A task of a step that declares outputs proves its work with an output manifest.

    One that reports success and uploads nothing left nothing for the next step.
    """
    farm: FarmView = _with_task(
        read_recorded_farm_view(), "ropfetch_karma", 0, manifest_count=0, uploaded_files=frozenset()
    )
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm)
    assert comparison.differences == (
        Difference(
            DifferenceType.OUTPUT_PROOF_MISSING,
            step="ropfetch_karma",
            task_index=0,
            work_item="ropfetch_karma_33",
            cook_says="an output manifest",
        ),
    )


def test_a_task_that_did_not_succeed_is_not_checked_for_files() -> None:
    """A failed task is expected to leave no files, so missing ones are not a difference."""
    farm: FarmView = replace(
        _with_task(
            read_recorded_farm_view(),
            "ropfetch_karma",
            0,
            run_status="FAILED",
            manifest_count=0,
            uploaded_files=frozenset(),
        ),
        task_run_status="FAILED",
    )
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm)
    assert _task_differences(comparison) == []


def test_a_declared_output_pdg_expects_that_was_not_uploaded_is_a_difference() -> None:
    """Every output PDG holds for the work item must be in the task's manifests, or the step
    after it can't find the file."""
    farm: FarmView = _with_task(
        read_recorded_farm_view(),
        "ropfetch_karma",
        0,
        uploaded_files=frozenset({f"{ROOT}/pdgtemp/data/ropfetch_karma_33.json.gz"}),
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.differences == (
        Difference(
            DifferenceType.OUTPUT_NOT_UPLOADED,
            step="ropfetch_karma",
            task_index=0,
            work_item="ropfetch_karma_33",
            cook_says=KARMA_0_RENDER,
            farm_says="in no output manifest",
        ),
    )


def test_a_succeeded_task_with_no_output_record_is_a_difference() -> None:
    """A task of a step that declares no outputs proves its work with an output record."""
    farm: FarmView = _with_task(
        read_recorded_farm_view(), "bonfire_montage_video", 0, output_record=None
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.differences == (
        Difference(
            DifferenceType.OUTPUT_PROOF_MISSING,
            step="bonfire_montage_video",
            task_index=0,
            work_item="bonfire_montage_video_48",
            cook_says="an output record",
        ),
    )


def test_a_recorded_output_that_was_not_uploaded_is_a_difference() -> None:
    """A file the output record names, with PDG's token made a real path, must be in the
    task's manifests."""
    # The video's task uploaded everything but the video.
    recorded: FarmView = read_recorded_farm_view()
    uploaded: FrozenSet[str] = recorded.steps["bonfire_montage_video"].tasks[0].uploaded_files
    farm: FarmView = _with_task(
        recorded, "bonfire_montage_video", 0, uploaded_files=uploaded - {VIDEO}
    )
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm)
    assert comparison.differences == (
        Difference(
            DifferenceType.OUTPUT_NOT_UPLOADED,
            step="bonfire_montage_video",
            task_index=0,
            work_item="bonfire_montage_video_48",
            cook_says=VIDEO,
            farm_says="in no output manifest",
        ),
    )


def test_a_recorded_output_that_never_reached_pdg_is_a_difference() -> None:
    """A file the worker recorded that isn't an output of the PDG work item was never handed
    to PDG, so the steps after it can't find it."""
    items: List[PdgWorkItem] = _with_item(
        recorded_pdg_work_items(), "bonfire_montage_video_48", outputs=()
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), read_recorded_farm_view(), pdg_work_items=items
    )
    assert comparison.differences == (
        Difference(
            DifferenceType.OUTPUT_NOT_REPORTED,
            step="bonfire_montage_video",
            task_index=0,
            work_item="bonfire_montage_video_48",
            cook_says="not a PDG output",
            farm_says=VIDEO,
        ),
    )


def test_a_recorded_output_with_another_tag_in_pdg_is_a_difference() -> None:
    """PDG routes files to the next step by tag, so a file with the wrong tag goes to the
    wrong place."""
    items: List[PdgWorkItem] = _with_item(
        recorded_pdg_work_items(),
        "bonfire_montage_video_48",
        outputs=(OutputFile(VIDEO, "file/image"),),
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), read_recorded_farm_view(), pdg_work_items=items
    )
    assert comparison.differences == (
        Difference(
            DifferenceType.OUTPUT_TAG_DIFFERS,
            step="bonfire_montage_video",
            task_index=0,
            work_item="bonfire_montage_video_48",
            cook_says=f"{VIDEO}: file/image",
            farm_says=f"{VIDEO}: file/video",
        ),
    )


def test_a_file_recorded_without_a_tag_is_not_a_tag_difference() -> None:
    """A job script may report a file with no tag. The cook hands it to PDG with none, and PDG
    then tags it by its extension, so PDG's tag differs from the empty one after a correct
    cook. Reporting that would fail every cook with an untagged output."""
    # The output record holds the video with no tag, and PDG tagged it file/video.
    recorded: FarmView = read_recorded_farm_view()
    record: Tuple[OutputFile, ...] = (
        recorded.steps["bonfire_montage_video"].tasks[0].output_record or ()
    )
    untagged: Tuple[OutputFile, ...] = tuple(replace(output, tag="") for output in record)
    assert [output.tag for output in untagged] == [""]
    farm: FarmView = _with_task(recorded, "bonfire_montage_video", 0, output_record=untagged)

    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.passed


def test_paths_spelled_differently_for_the_same_file_match() -> None:
    """PDG, the output record and the manifest each spell a path their own way, so paths are
    compared after normalizing. A doubled slash or a ``.`` is the same file."""
    items: List[PdgWorkItem] = _with_item(
        _with_item(
            recorded_pdg_work_items(),
            "bonfire_montage_video_48",
            outputs=(OutputFile(f"{ROOT}/./video//bonfire_montage_video.mp4", "file/video"),),
        ),
        "ropfetch_karma_33",
        outputs=(
            OutputFile(
                f"{ROOT}/render/../render/karma_bonfire/wedge0/karma_bonfire.w0.0001.png",
                "file/image/png",
            ),
        ),
    )
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), read_recorded_farm_view(), pdg_work_items=items
    )
    assert comparison.passed


def _with_video_recorded_as(farm: FarmView, path: str) -> FarmView:
    """The farm view with the video task's output record naming the video by ``path``."""
    return _with_task(
        farm, "bonfire_montage_video", 0, output_record=(OutputFile(path, "file/video"),)
    )


def test_a_recorded_output_is_reported_as_the_record_names_it() -> None:
    """A difference names a recorded file the way the output record does, not normalized.
    Normalizing on Windows turns every "/" into "\\", so a normalized path names a file the
    record never held."""
    # The record spells the video with a "..", which normalizing would remove on any OS,
    # and the task uploaded everything but the video.
    recorded: FarmView = read_recorded_farm_view()
    uploaded: FrozenSet[str] = recorded.steps["bonfire_montage_video"].tasks[0].uploaded_files
    farm: FarmView = _with_video_recorded_as(
        _with_task(recorded, "bonfire_montage_video", 0, uploaded_files=uploaded - {VIDEO}),
        "__PDG_DIR__/video/../video/bonfire_montage_video.mp4",
    )

    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm)
    assert comparison.differences == (
        Difference(
            DifferenceType.OUTPUT_NOT_UPLOADED,
            step="bonfire_montage_video",
            task_index=0,
            work_item="bonfire_montage_video_48",
            cook_says=f"{ROOT}/video/../video/bonfire_montage_video.mp4",
            farm_says="in no output manifest",
        ),
    )


def test_a_recorded_output_spelled_differently_matches_the_upload_and_pdg() -> None:
    """The output record's spelling of a path is matched normalized against the manifest and
    PDG, so a ``..`` in the record is still the file the task uploaded and PDG holds."""
    farm: FarmView = _with_video_recorded_as(
        read_recorded_farm_view(), "__PDG_DIR__/video/../video/bonfire_montage_video.mp4"
    )

    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(), farm, pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.passed


# -- the totals and the log -----------------------------------------------------


def test_a_cook_that_counted_more_successes_than_the_farm_had_is_a_difference() -> None:
    """The cook can't have reported more successes than the farm had tasks succeed."""
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(succeeded=27), read_recorded_farm_view()
    )
    assert comparison.differences == (
        Difference(
            DifferenceType.STATUS_COUNTS, cook_says="27 succeeded", farm_says="26 SUCCEEDED"
        ),
    )


def test_a_cook_that_counted_more_cancellations_than_the_farm_made_is_a_difference() -> None:
    """The cook counts a task as canceled only when the farm canceled it."""
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(canceled=1), read_recorded_farm_view()
    )
    assert comparison.differences == (
        Difference(DifferenceType.STATUS_COUNTS, cook_says="1 canceled", farm_says="0 CANCELED"),
    )


def test_a_cook_that_counted_fewer_cancellations_than_the_farm_made_is_not_a_difference() -> None:
    """A canceled task whose work item got another result isn't in the cook's canceled count,
    so the farm may show more cancellations than the cook counted."""
    farm: FarmView = _with_step(
        read_recorded_farm_view(),
        "mosaic_bonfire",
        task_run_status_counts={"SUCCEEDED": 1, "CANCELED": 1},
    )
    assert compare_cook_with_farm(_cook(), farm).passed


def test_a_failure_count_pdg_does_not_agree_with_is_a_difference() -> None:
    """The cook's counts are what it says it told PDG, so PDG's states must agree with them.

    Without PDG's work items there is nothing to check them against.
    """
    comparison: CookFarmComparison = compare_cook_with_farm(
        _cook(failed=1), read_recorded_farm_view(), pdg_work_items=recorded_pdg_work_items()
    )
    assert comparison.differences == (
        Difference(
            DifferenceType.STATUS_COUNTS, cook_says="1 failed", farm_says="PDG has 0 CookedFail"
        ),
    )
    assert compare_cook_with_farm(_cook(failed=1), read_recorded_farm_view()).passed


def test_a_logged_task_the_farm_ran_before_release_is_a_difference(tmp_path: Path) -> None:
    """The farm ran a task the cook was still holding back. Only the log shows this, because
    the final states look normal."""
    # The line as the cook logs it, with the sentence that follows it.
    debug_dir: Path = _debug_dir_copy(tmp_path)
    with open(debug_dir / "orchestrator.log", "a", encoding="utf-8") as log:
        log.write(
            "[2026-11-10T17:15:00+00:00] Task ropfetch_karma[3] reached SUCCEEDED, but the farm "
            "ran it before the Deadline Cloud Scheduler node started it. Its result is kept.\n"
        )

    record: CookRecord = read_cook_record(debug_dir)
    assert record.ran_before_release_lines == (RanBeforeRelease("ropfetch_karma", 3, "SUCCEEDED"),)
    comparison: CookFarmComparison = compare_cook_with_farm(record, read_recorded_farm_view())
    assert comparison.differences == (
        Difference(
            DifferenceType.RAN_BEFORE_RELEASE,
            step="ropfetch_karma",
            task_index=3,
            farm_says="SUCCEEDED",
        ),
    )


def test_sessions_that_ran_tasks_of_two_steps_are_counted_and_never_fail() -> None:
    """A session that ran tasks of two steps is worth knowing about, but is not a difference."""
    # The mosaic task ran in the session of one of the render tasks.
    recorded: FarmView = read_recorded_farm_view()
    shared: str = recorded.steps["ropfetch_karma"].tasks[0].session_id
    farm: FarmView = _with_task(recorded, "mosaic_bonfire", 0, session_id=shared)

    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm)
    assert comparison.passed
    assert comparison.observations["sessions_that_ran_more_than_one_step"] == 1


def test_tasks_that_never_ran_are_not_counted_as_one_session() -> None:
    """A task that never ran has no session. Two such tasks in different steps must not count
    as one session that ran both steps."""
    # One task in each of two steps never ran.
    farm: FarmView = _with_task(
        _with_task(read_recorded_farm_view(), "ropfetch_karma", 0, session_id=""),
        "mosaic_bonfire",
        0,
        session_id="",
    )
    comparison: CookFarmComparison = compare_cook_with_farm(_cook(), farm)
    assert comparison.observations["sessions_that_ran_more_than_one_step"] == 0


# -- reading the debug directory ------------------------------------------------


def test_a_snapshot_without_items_cannot_be_compared(tmp_path: Path) -> None:
    """A debug directory from a release that didn't list each step's items can't join tasks
    to work items, so reading it fails and names the problem."""
    # One step of the snapshot has no items list.
    debug_dir: Path = _debug_dir_copy(tmp_path)
    snapshot: Dict[str, Any] = json.loads((debug_dir / "snapshot.json").read_text())
    del snapshot["steps"][2]["items"]
    (debug_dir / "snapshot.json").write_text(json.dumps(snapshot))

    with pytest.raises(ValueError, match="no items list for step ropfetch_karma"):
        read_cook_record(debug_dir)


def test_a_snapshot_whose_items_do_not_match_its_task_count_cannot_be_compared(
    tmp_path: Path,
) -> None:
    """An items list shorter than the step would leave tasks without a work item."""
    # A six-task step lists five items.
    debug_dir: Path = _debug_dir_copy(tmp_path)
    snapshot: Dict[str, Any] = json.loads((debug_dir / "snapshot.json").read_text())
    snapshot["steps"][2]["items"].pop()
    (debug_dir / "snapshot.json").write_text(json.dumps(snapshot))

    with pytest.raises(ValueError, match="5 item"):
        read_cook_record(debug_dir)


def test_a_cook_json_that_is_not_an_object_cannot_be_compared(tmp_path: Path) -> None:
    """``cook.json`` must hold an object, or no field of it can be read."""
    debug_dir: Path = _debug_dir_copy(tmp_path)
    (debug_dir / "cook.json").write_text("[]")
    with pytest.raises(ValueError, match="cook.json"):
        read_cook_record(debug_dir)


# -- the report -----------------------------------------------------------------


def test_the_report_has_one_line_per_difference_then_the_verdict() -> None:
    """Each difference is one line naming its type, task, work item and both sides, and the
    last line starts with FAIL and the number of differences."""
    farm: FarmView = replace(
        _with_task(read_recorded_farm_view(), "ropfetch_karma", 4, run_status="SUSPENDED"),
        task_run_status="SUSPENDED",
    )
    text: str = format_comparison(
        compare_cook_with_farm(_cook(), farm, pdg_work_items=recorded_pdg_work_items())
    )
    assert text.splitlines() == [
        "job_not_succeeded: farm says SUSPENDED",
        "reported_but_not_run: ropfetch_karma[4] (ropfetch_karma_37): "
        "cook says CookedSuccess, farm says SUSPENDED",
        f"FAIL: 2 difference(s) for job {RECORDED_JOB_ID}: 26 task(s) in 6 step(s). "
        "0 session(s) ran tasks from more than one step.",
    ]


def test_the_report_of_a_clean_hand_run_passes_and_says_task_statuses_were_not_checked() -> None:
    """A hand run says that PDG's work items were missing, so a PASS isn't over-read."""
    text: str = format_comparison(compare_cook_with_farm(_cook(), read_recorded_farm_view()))
    assert text.splitlines() == [
        "PDG's work items were not given, so each task's status was checked only through the "
        "cook's totals.",
        f"PASS for job {RECORDED_JOB_ID}: 26 task(s) in 6 step(s). "
        "0 session(s) ran tasks from more than one step.",
    ]


# -- the command ----------------------------------------------------------------


class _RecordedFarm:
    """Stands in for ``read_farm_view`` in the command: answers with a fixed view, and keeps
    the farm, queue and job it was asked for."""

    def __init__(self, view: Optional[FarmView] = None, error: Optional[Exception] = None) -> None:
        self.asked: List[Tuple[FarmQueue, str]] = []
        self._view: Optional[FarmView] = view
        self._error: Optional[Exception] = error

    def __call__(self, farm_queue: FarmQueue, job_id: str) -> FarmView:
        # Keep what was asked for before failing, so a test can check what was read.
        self.asked.append((farm_queue, job_id))
        if self._error is not None:
            raise self._error
        return self._view or read_recorded_farm_view()


@pytest.fixture
def workstation_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Deadline config for the command to read the default farm and queue from."""
    # The client library reads its config from this variable's path.
    path: Path = tmp_path / "deadline" / "config"
    path.parent.mkdir()
    path.write_text("")
    monkeypatch.setenv("DEADLINE_CONFIG_FILE_PATH", str(path))
    return path


def _set_defaults(path: Path, farm_id: str, queue_id: str) -> None:
    """Write a default farm and queue into the Deadline config at ``path``."""
    # Written with the client library's own setter, so the file has the layout it reads.
    parser: ConfigParser = ConfigParser()
    config_file.set_setting("defaults.farm_id", farm_id, config=parser)
    config_file.set_setting("defaults.queue_id", queue_id, config=parser)
    with open(path, "w", encoding="utf8") as f:
        parser.write(f)


def test_the_command_passes_a_clean_cook_and_writes_the_json_report(
    tmp_path: Path, workstation_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a farm and queue given, the command reads the job the debug directory names,
    prints PASS, exits 0 and writes the report in its documented shape."""
    farm: _RecordedFarm = _RecordedFarm()
    # Name the job's farm and queue on the command line, and ask for the JSON report.
    report_path: Path = tmp_path / "comparison.json"
    argv: List[str] = [
        str(PYRO_BONFIRE_DIR),
        "--farm",
        RECORDED_QUEUE.farm_id,
        "--queue",
        RECORDED_QUEUE.queue_id,
        "--json",
        str(report_path),
    ]

    assert main(argv, read_farm=farm) == 0
    assert farm.asked == [(RECORDED_QUEUE, RECORDED_JOB_ID)]
    assert capsys.readouterr().out.splitlines()[-1].startswith("PASS")
    assert json.loads(report_path.read_text()) == {
        "version": 1,
        "job_id": RECORDED_JOB_ID,
        "passed": True,
        "used_pdg_work_items": False,
        "differences": [],
        "observations": {"sessions_that_ran_more_than_one_step": 0, "tasks": 26, "steps": 6},
    }


def test_the_command_exits_1_and_reports_each_difference(
    tmp_path: Path, workstation_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A difference makes the command exit 1, print it, and put it in the report."""
    farm: _RecordedFarm = _RecordedFarm(
        replace(read_recorded_farm_view(), task_run_status="FAILED")
    )
    # Name the job's farm and queue on the command line, and ask for the JSON report.
    report_path: Path = tmp_path / "comparison.json"
    argv: List[str] = [
        str(PYRO_BONFIRE_DIR),
        "--farm",
        RECORDED_QUEUE.farm_id,
        "--queue",
        RECORDED_QUEUE.queue_id,
        "--json",
        str(report_path),
    ]

    assert main(argv, read_farm=farm) == 1
    assert "job_not_succeeded: farm says FAILED" in capsys.readouterr().out
    report: Dict[str, Any] = json.loads(report_path.read_text())
    assert report["passed"] is False
    assert report["differences"] == [
        {
            "difference_type": "job_not_succeeded",
            "step": "",
            "task_index": None,
            "work_item": "",
            "cook_says": "",
            "farm_says": "FAILED",
        }
    ]


def test_the_command_reads_the_job_from_the_workstation_defaults_when_no_flags_are_given(
    workstation_config: Path,
) -> None:
    """A debug directory names the job but not its farm or queue, so the command uses the
    workstation's defaults, which the cook submitted with."""
    # The defaults name the recorded job's farm and queue, and no flag is given.
    _set_defaults(workstation_config, RECORDED_QUEUE.farm_id, RECORDED_QUEUE.queue_id)
    farm: _RecordedFarm = _RecordedFarm()

    assert main([str(PYRO_BONFIRE_DIR)], read_farm=farm) == 0
    assert farm.asked == [(RECORDED_QUEUE, RECORDED_JOB_ID)]


def test_the_commands_flags_win_over_the_workstation_defaults(workstation_config: Path) -> None:
    """``--farm`` and ``--queue`` name the job's farm and queue even when defaults are set."""
    # The defaults name another farm and queue than the flags.
    _set_defaults(workstation_config, "farm-other", "queue-other")
    farm: _RecordedFarm = _RecordedFarm()
    argv: List[str] = [
        str(PYRO_BONFIRE_DIR),
        "--farm",
        RECORDED_QUEUE.farm_id,
        "--queue",
        RECORDED_QUEUE.queue_id,
    ]

    assert main(argv, read_farm=farm) == 0
    assert farm.asked == [(RECORDED_QUEUE, RECORDED_JOB_ID)]


@pytest.mark.parametrize(
    "flags, defaults",
    [
        ([], ("", "")),
        (["--farm", "farm-a"], ("", "")),
        ([], ("farm-a", "")),
    ],
    ids=["nothing set", "only a farm flag", "only a default farm"],
)
def test_the_command_exits_2_without_both_a_farm_and_a_queue(
    flags: List[str],
    defaults: Tuple[str, str],
    workstation_config: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no farm or no queue from either the flags or the defaults, the job can't be found.
    The command exits 2, names the flags, and reads nothing."""
    # Neither the flags nor the defaults give both IDs.
    _set_defaults(workstation_config, *defaults)
    farm: _RecordedFarm = _RecordedFarm()

    assert main([str(PYRO_BONFIRE_DIR), *flags], read_farm=farm) == 2
    assert "--farm and --queue" in capsys.readouterr().err
    assert farm.asked == []


def test_the_command_exits_2_on_a_debug_directory_it_cannot_read(
    tmp_path: Path, workstation_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A folder without the cook's files is not a cook, so nothing is compared."""
    # An empty folder stands in for the debug directory.
    farm: _RecordedFarm = _RecordedFarm()
    assert main([str(tmp_path), "--farm", "f", "--queue", "q"], read_farm=farm) == 2
    assert "Cannot read the debug directory" in capsys.readouterr().err
    assert farm.asked == []


@pytest.mark.parametrize("missing", ["steps", "name", "task_count", "declares_outputs"])
def test_the_command_exits_2_on_a_snapshot_without_the_fields_the_comparison_reads(
    missing: str, tmp_path: Path, workstation_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A snapshot.json without its steps list, or with a step that lacks a field the join or
    the checks read, can't be compared. The command must exit 2 and name the file. Exit 1
    would say the cook differs from the farm."""
    # Remove the steps list, or one field of the six-task render step.
    debug_dir: Path = _debug_dir_copy(tmp_path)
    snapshot: Dict[str, Any] = json.loads((debug_dir / "snapshot.json").read_text())
    if missing == "steps":
        del snapshot["steps"]
    else:
        del snapshot["steps"][2][missing]
    (debug_dir / "snapshot.json").write_text(json.dumps(snapshot))
    farm: _RecordedFarm = _RecordedFarm()

    assert main([str(debug_dir), "--farm", "f", "--queue", "q"], read_farm=farm) == 2
    error: str = capsys.readouterr().err
    assert "Cannot read the debug directory" in error
    assert "snapshot.json" in error
    assert farm.asked == []


def test_the_command_exits_2_when_it_cannot_write_the_report(
    tmp_path: Path, workstation_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report path in a folder that doesn't exist is a mistake in the arguments. The command
    must exit 2 and say so. Exit 1 would say the cook differs from the farm."""
    argv: List[str] = [
        str(PYRO_BONFIRE_DIR),
        "--farm",
        RECORDED_QUEUE.farm_id,
        "--queue",
        RECORDED_QUEUE.queue_id,
        "--json",
        str(tmp_path / "no-such-folder" / "comparison.json"),
    ]

    assert main(argv, read_farm=_RecordedFarm()) == 2
    assert "Cannot write the report" in capsys.readouterr().err


def test_the_module_runs_as_the_command_and_reads_the_debug_directory_it_is_given(
    tmp_path: Path,
) -> None:
    """The hatch script runs ``python -m`` on the module from the repo root. The debug
    directory must reach ``main``: its message about the empty folder proves that, and no
    farm is read before it."""
    # An empty folder stands in for the debug directory, so the command stops before any
    # farm call.
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [
            sys.executable,
            "-m",
            "test.integ.pdg.compare_cook_with_farm",
            str(tmp_path),
            "--farm",
            "f",
            "--queue",
            "q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stderr
    assert f"Cannot read the debug directory {tmp_path}" in result.stderr


def test_the_command_exits_2_when_the_cook_never_submitted_a_job(
    tmp_path: Path, workstation_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused cook writes a debug directory with no job, so there is nothing to compare."""
    # cook.json has no job ID.
    debug_dir: Path = _debug_dir_copy(tmp_path)
    _edit_json(debug_dir / "cook.json", job_id=None)
    farm: _RecordedFarm = _RecordedFarm()

    assert main([str(debug_dir), "--farm", "f", "--queue", "q"], read_farm=farm) == 2
    assert "never submitted" in capsys.readouterr().err
    assert farm.asked == []


def test_the_command_exits_2_when_the_farm_cannot_be_read(
    workstation_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An expired login or missing access leaves the cook unchecked, which is not a failure
    of the cook, so the exit code differs from a difference's."""
    # Reading the farm raises, as boto3 does on an expired login.
    farm: _RecordedFarm = _RecordedFarm(error=RuntimeError("expired token"))
    argv: List[str] = [str(PYRO_BONFIRE_DIR), "--farm", "f", "--queue", "q"]

    assert main(argv, read_farm=farm) == 2
    assert "RuntimeError: expired token" in capsys.readouterr().err
