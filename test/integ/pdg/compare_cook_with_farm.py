# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Compare what a PDG cook says happened with what the farm says happened, task by task.

The cook's side is its debug directory, plus PDG's work items when a test has them. The
farm's side is the job on Deadline Cloud and the files its tasks uploaded to S3. Each
task is joined to its work item through the snapshot's ``items`` list: the task with
TaskIndex i ran ``items[i]``.

Run it by hand on any cook's debug directory::

    hatch run integ:compare_pdg_cook_with_farm <debug_dir> [--farm F --queue Q] [--json out.json]

It prints one line per difference, then PASS or FAIL, and exits 0 when it finds no
difference, 1 when it finds one, and 2 when it can't read the debug directory or the farm,
or can't write the JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, List, Mapping, Optional, Pattern, Sequence
from typing import Set, Tuple

from deadline.houdini_pdg.graph import PDG_DIR_TOKEN
from deadline.houdini_pdg.ports import OutputFile
from deadline.houdini_pdg.submitter import get_workstation_setting

from .farm import FarmQueue, FarmStep, FarmTask, FarmView, read_farm_view

# PDG's names for a work item's final state.
_COOKED_SUCCESS: str = "CookedSuccess"
_COOKED_FAIL: str = "CookedFail"

# Deadline Cloud's run statuses that the checks single out.
_SUCCEEDED: str = "SUCCEEDED"
_FAILED: str = "FAILED"
_CANCELED: str = "CANCELED"

# The phase a cook ends in when it ran to the end.
_DONE: str = "Done"

# The line the cook logs when the farm ran a task the cook was still holding back. The
# line goes on to say what the cook did about it, so it is searched, not matched whole.
_RAN_BEFORE_RELEASE_LINE: Pattern[str] = re.compile(
    r"Task (?P<step>\S+)\[(?P<task_index>\d+)\] reached (?P<status>[A-Z_]+), but the farm ran "
    r"it before the Deadline Cloud Scheduler node started it\."
)

# PDG's job scripts write each work item's data to this file, and the task uploads it.
_WORK_ITEM_DATA_FILE: Pattern[str] = re.compile(r"/pdgtemp/data/(?P<name>[^/]+)\.json\.gz$")

# What the farm says about a file that no output manifest lists.
_NOT_UPLOADED: str = "in no output manifest"

# The version of the JSON report's format.
_REPORT_VERSION: int = 1


class DifferenceType(Enum):
    """What kind of difference the comparison found."""

    #: The cook did not end in its Done phase.
    COOK_NOT_DONE = "cook_not_done"
    #: The job did not succeed on the farm.
    JOB_NOT_SUCCEEDED = "job_not_succeeded"
    #: A step is in the snapshot but not on the farm, or on the farm but not in the snapshot.
    STEP_MISSING = "step_missing"
    #: A step has a different number of tasks on the farm than in the snapshot.
    TASK_COUNT = "task_count"
    #: A step's host requirements do not hold the ones the test expected.
    HOST_REQUIREMENTS = "host_requirements"
    #: The snapshot names a work item PDG doesn't have, so the job described another graph.
    WORK_ITEM_MISSING = "work_item_missing"
    #: PDG was told a work item succeeded, but its task did not succeed on the farm.
    REPORTED_BUT_NOT_RUN = "reported_but_not_run"
    #: A task succeeded on the farm, but PDG was never told its work item succeeded.
    RUN_BUT_NOT_REPORTED = "run_but_not_reported"
    #: The farm canceled a task on its own, and PDG was told its work item failed.
    CANCELED_REPORTED_AS_FAILED = "canceled_reported_as_failed"
    #: A task uploaded the data file of a work item other than the one it should have run.
    RAN_WRONG_WORK_ITEM = "ran_wrong_work_item"
    #: A succeeded task left no output manifest, or no output record, to show its work.
    OUTPUT_PROOF_MISSING = "output_proof_missing"
    #: A file the cook or PDG expects is in none of the task's output manifests.
    OUTPUT_NOT_UPLOADED = "output_not_uploaded"
    #: A file in the task's output record is not an output of its PDG work item.
    OUTPUT_NOT_REPORTED = "output_not_reported"
    #: A file's tag in the output record differs from its tag in PDG, which routes files by
    #: tag. A file recorded with no tag is never this difference, because PDG picks its tag.
    OUTPUT_TAG_DIFFERS = "output_tag_differs"
    #: The cook's closing counts differ from the farm's task statuses or from PDG's states.
    STATUS_COUNTS = "status_counts"
    #: The cook logged that the farm ran a task it was still holding back.
    RAN_BEFORE_RELEASE = "ran_before_release"


@dataclass(frozen=True)
class Difference:
    """One way the cook's side and the farm's side disagree."""

    difference_type: DifferenceType
    #: The step, task index and work item the difference is about, when it is about one.
    step: str = ""
    task_index: Optional[int] = None
    work_item: str = ""
    #: What each side says, in a few words or a path.
    cook_says: str = ""
    farm_says: str = ""


@dataclass(frozen=True)
class CookFarmComparison:
    """Every difference found for one job, and counts that are worth knowing but never fail."""

    job_id: str
    #: Whether PDG's work items were given, so each task's status could be checked.
    used_pdg_work_items: bool
    differences: Tuple[Difference, ...]
    observations: Mapping[str, int]

    @property
    def passed(self) -> bool:
        """True when the comparison found no difference."""
        return not self.differences


@dataclass(frozen=True)
class RanBeforeRelease:
    """One logged task the farm ran before the cook let it run."""

    step: str
    task_index: int
    #: The status the farm reported for the task.
    status: str


@dataclass(frozen=True)
class PdgWorkItem:
    """One PDG work item as it ended, copied out of hython."""

    node: str
    name: str
    #: PDG's own index, not the task index.
    index: int
    #: A ``pdg.workItemState`` name, such as ``CookedSuccess``.
    state: str
    #: Absolute local paths, with their tags.
    outputs: Tuple[OutputFile, ...]


@dataclass(frozen=True)
class CookRecord:
    """The cook's own account of itself, read from its debug directory."""

    cook_json: Mapping[str, Any]
    #: Every step has an ``items`` list with one name per task.
    snapshot: Mapping[str, Any]
    ran_before_release_lines: Tuple[RanBeforeRelease, ...]


@dataclass(frozen=True)
class _TaskRow:
    """One farm task joined to its work item."""

    step: str
    task_index: int
    declares_outputs: bool
    farm_task: FarmTask
    #: ``items[task_index]`` from the snapshot.
    work_item: str
    #: PDG's work item of that name, or None when PDG's work items weren't given or lack it.
    pdg_item: Optional[PdgWorkItem]


def read_cook_record(debug_dir: Path) -> CookRecord:
    """Read a cook's ``cook.json``, ``snapshot.json`` and ``orchestrator.log``.

    Raises ``OSError`` for a missing or unreadable file, and ``ValueError`` for content
    the comparison can't use, naming the file and the problem.
    """
    # The snapshot must list each step's fields and items, or no task can be joined to its
    # work item.
    cook_json: Dict[str, Any] = _read_json_object(debug_dir / "cook.json")
    snapshot: Dict[str, Any] = _read_json_object(debug_dir / "snapshot.json")
    _check_steps(snapshot)

    # Of the log, only the lines about tasks the farm ran too early are compared.
    log_text: str = (debug_dir / "orchestrator.log").read_text(encoding="utf-8", errors="replace")
    lines: Tuple[RanBeforeRelease, ...] = tuple(
        RanBeforeRelease(m["step"], int(m["task_index"]), m["status"])
        for m in _RAN_BEFORE_RELEASE_LINE.finditer(log_text)
    )
    return CookRecord(cook_json=cook_json, snapshot=snapshot, ran_before_release_lines=lines)


def compare_cook_with_farm(
    cook: CookRecord,
    farm: FarmView,
    *,
    pdg_work_items: Optional[Sequence[PdgWorkItem]] = None,
    expected_host_requirements: Optional[Mapping[str, Any]] = None,
) -> CookFarmComparison:
    """Every difference between the cook's side and the farm's side. Pure: no I/O.

    Every check runs even when an earlier one found a difference, so one run reports
    everything that is wrong. Without PDG's work items, a task's status is checked only
    through the cook's totals.
    """
    snapshot_steps: List[Mapping[str, Any]] = list(cook.snapshot["steps"])
    work_items_by_name: Optional[Dict[str, PdgWorkItem]] = (
        None if pdg_work_items is None else {w.name: w for w in pdg_work_items}
    )
    differences: List[Difference] = []

    # The job as a whole, then its steps and task counts.
    differences += _job_differences(cook.cook_json, farm)
    differences += _shape_differences(snapshot_steps, farm, expected_host_requirements)

    # Each task the farm has, joined to its work item, then its status and its files.
    rows: List[_TaskRow] = _task_rows(snapshot_steps, farm, work_items_by_name)
    job_could_not_finish: bool = farm.task_run_status == _FAILED or bool(
        cook.cook_json.get("stopped_by_user")
    )
    for row in rows:
        if work_items_by_name is not None:
            differences += _status_differences(row, job_could_not_finish)
        differences += _work_item_data_differences(row)
        if row.farm_task.run_status == _SUCCEEDED:
            differences += _file_differences(row, farm.pdg_dir)

    # The cook's totals, then what only its log can show.
    differences += _count_differences(cook.cook_json, snapshot_steps, farm, work_items_by_name)
    differences += [
        Difference(
            DifferenceType.RAN_BEFORE_RELEASE,
            step=line.step,
            task_index=line.task_index,
            farm_says=line.status,
        )
        for line in cook.ran_before_release_lines
    ]

    observations: Dict[str, int] = {
        "sessions_that_ran_more_than_one_step": _sessions_that_ran_more_than_one_step(farm),
        "tasks": len(rows),
        "steps": len(snapshot_steps),
    }
    return CookFarmComparison(
        job_id=farm.job_id,
        used_pdg_work_items=work_items_by_name is not None,
        differences=tuple(differences),
        observations=observations,
    )


def format_comparison(comparison: CookFarmComparison) -> str:
    """One line per difference, then a line that starts with PASS or FAIL."""
    lines: List[str] = [_describe(d) for d in comparison.differences]
    if not comparison.used_pdg_work_items:
        lines.append(
            "PDG's work items were not given, so each task's status was checked only "
            "through the cook's totals."
        )

    # The verdict, with the counts that say how much was compared.
    verdict: str = (
        "PASS" if comparison.passed else f"FAIL: {len(comparison.differences)} difference(s)"
    )
    observed: Mapping[str, int] = comparison.observations
    lines.append(
        f"{verdict} for job {comparison.job_id}: {observed['tasks']} task(s) in "
        f"{observed['steps']} step(s). {observed['sessions_that_ran_more_than_one_step']} "
        "session(s) ran tasks from more than one step."
    )
    return "\n".join(lines)


def main(
    argv: Sequence[str],
    read_farm: Callable[[FarmQueue, str], FarmView] = read_farm_view,
) -> int:
    """Compare one cook's debug directory with its job. Returns 0, 1 or 2, as the module says.

    ``read_farm`` reads the job; tests pass one that reads recorded data.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="compare_pdg_cook_with_farm",
        description="Compare a PDG cook's debug directory with its job on Deadline Cloud.",
    )
    parser.add_argument("debug_dir", type=Path)
    parser.add_argument("--farm", default="", help="the job's farm; the workstation's by default")
    parser.add_argument("--queue", default="", help="the job's queue; the workstation's by default")
    parser.add_argument("--json", dest="json_out", type=Path, help="also write the report here")
    args: argparse.Namespace = parser.parse_args(argv)

    # The cook's side, and the job it names.
    try:
        cook: CookRecord = read_cook_record(args.debug_dir)
    except (OSError, ValueError) as e:
        print(f"Cannot read the debug directory {args.debug_dir}: {e}", file=sys.stderr)
        return 2
    job_id: str = str(cook.cook_json.get("job_id") or "")
    if not job_id:
        print(
            f"{args.debug_dir}/cook.json names no job, so the cook never submitted one.",
            file=sys.stderr,
        )
        return 2

    # A debug directory doesn't record the farm or queue. The workstation's defaults are
    # what the cook submitted with.
    farm_id: str = args.farm or get_workstation_setting("defaults.farm_id")
    queue_id: str = args.queue or get_workstation_setting("defaults.queue_id")
    if not farm_id or not queue_id:
        print(
            "Pass --farm and --queue, or set the workstation's default farm and queue.",
            file=sys.stderr,
        )
        return 2

    # The farm's side. An expired login or missing access leaves the cook unchecked, not failed.
    try:
        farm: FarmView = read_farm(FarmQueue(farm_id=farm_id, queue_id=queue_id), job_id)
    except Exception as e:
        print(f"Cannot read job {job_id} from the farm: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    comparison: CookFarmComparison = compare_cook_with_farm(cook, farm)
    print(format_comparison(comparison))

    # A report path that can't be written is a mistake in the arguments, not a difference.
    if args.json_out is not None:
        try:
            args.json_out.write_text(json.dumps(_report(comparison), indent=2), encoding="utf-8")
        except OSError as e:
            print(f"Cannot write the report to {args.json_out}: {e}", file=sys.stderr)
            return 2
    return 0 if comparison.passed else 1


def _read_json_object(path: Path) -> Dict[str, Any]:
    """A JSON file that must hold an object."""
    content: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError(f"{path.name} must hold a JSON object.")
    return content


def _check_steps(snapshot: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless every step has the fields the comparison reads, and lists
    one work item per task."""
    steps: Any = snapshot.get("steps")
    if not isinstance(steps, list):
        raise ValueError("snapshot.json has no steps list.")

    for position, step in enumerate(steps):
        # Every message below names the step, so a step must first be an object with a name.
        name: Any = step.get("name") if isinstance(step, dict) else None
        if not isinstance(name, str):
            raise ValueError(f"snapshot.json step {position} is not an object with a name.")

        # A snapshot written before items were recorded lacks only that list, so it gets
        # its own message.
        items: Any = step.get("items")
        if items is None:
            raise ValueError(
                f"snapshot.json has no items list for step {name}. It was written "
                "by a release that doesn't record which work item each task runs."
            )
        missing: List[str] = [f for f in ("task_count", "declares_outputs") if f not in step]
        if missing:
            raise ValueError(f"snapshot.json has no {' or '.join(missing)} for step {name}.")

        # The task with TaskIndex i ran items[i], so every task needs an entry.
        if len(items) != step["task_count"]:
            raise ValueError(
                f"snapshot.json lists {len(items)} item(s) for step {name}, "
                f"which has {step['task_count']} task(s)."
            )


def _job_differences(cook_json: Mapping[str, Any], farm: FarmView) -> List[Difference]:
    """Whether the cook ended Done and the job succeeded."""
    # A cook that died before it closed has no phase in cook.json.
    differences: List[Difference] = []
    final_phase: str = str(cook_json.get("final_phase", ""))
    if final_phase != _DONE:
        differences.append(Difference(DifferenceType.COOK_NOT_DONE, cook_says=final_phase))
    if farm.task_run_status != _SUCCEEDED:
        differences.append(
            Difference(DifferenceType.JOB_NOT_SUCCEEDED, farm_says=farm.task_run_status)
        )
    return differences


def _shape_differences(
    snapshot_steps: Sequence[Mapping[str, Any]],
    farm: FarmView,
    expected_host_requirements: Optional[Mapping[str, Any]],
) -> List[Difference]:
    """Steps on one side only, task counts that differ, and missing host requirements."""
    # Each snapshot step must be on the farm, with its task count and host requirements.
    differences: List[Difference] = []
    for step in snapshot_steps:
        name: str = step["name"]
        farm_step: Optional[FarmStep] = farm.steps.get(name)
        if farm_step is None:
            differences.append(
                Difference(
                    DifferenceType.STEP_MISSING,
                    step=name,
                    cook_says=f"{step['task_count']} task(s)",
                    farm_says="no such step",
                )
            )
            continue

        # A task only one side has gets no row later, so it is reported only here.
        if len(farm_step.tasks) != step["task_count"]:
            differences.append(
                Difference(
                    DifferenceType.TASK_COUNT,
                    step=name,
                    cook_says=str(step["task_count"]),
                    farm_says=str(len(farm_step.tasks)),
                )
            )

        # Host requirements are checked only for a test that expects some.
        if expected_host_requirements is not None and not _holds(
            farm_step.required_capabilities, expected_host_requirements
        ):
            differences.append(
                Difference(
                    DifferenceType.HOST_REQUIREMENTS,
                    step=name,
                    cook_says=json.dumps(expected_host_requirements),
                    farm_says=json.dumps(farm_step.required_capabilities),
                )
            )

    # A step only the farm has means the job was built from another graph.
    snapshot_names: Set[str] = {step["name"] for step in snapshot_steps}
    differences += [
        Difference(
            DifferenceType.STEP_MISSING,
            step=name,
            cook_says="no such step",
            farm_says=f"{len(farm_step.tasks)} task(s)",
        )
        for name, farm_step in farm.steps.items()
        if name not in snapshot_names
    ]
    return differences


def _holds(required: Optional[Mapping[str, Any]], expected: Mapping[str, Any]) -> bool:
    """Whether every entry the test expects, under each key, is among the step's requirements."""
    # The farm may hold more requirements than the test names, so only containment counts.
    actual: Mapping[str, Any] = required or {}
    return all(
        entry in actual.get(key, []) for key, entries in expected.items() for entry in entries
    )


def _task_rows(
    snapshot_steps: Sequence[Mapping[str, Any]],
    farm: FarmView,
    work_items_by_name: Optional[Mapping[str, PdgWorkItem]],
) -> List[_TaskRow]:
    """One row per task that both the snapshot and the farm have, in snapshot order."""
    # A task only one side has is already a step or task count difference, so it gets no row.
    rows: List[_TaskRow] = []
    for step in snapshot_steps:
        farm_step: Optional[FarmStep] = farm.steps.get(step["name"])
        if farm_step is None:
            continue
        for task_index, work_item in enumerate(step["items"]):
            farm_task: Optional[FarmTask] = farm_step.tasks.get(task_index)
            if farm_task is None:
                continue
            rows.append(
                _TaskRow(
                    step=step["name"],
                    task_index=task_index,
                    declares_outputs=bool(step["declares_outputs"]),
                    farm_task=farm_task,
                    work_item=work_item,
                    pdg_item=(work_items_by_name or {}).get(work_item),
                )
            )
    return rows


def _status_differences(row: _TaskRow, job_could_not_finish: bool) -> List[Difference]:
    """The task's farm status against its work item's PDG state. The first matching case wins."""
    status: str = row.farm_task.run_status
    if row.pdg_item is None:
        return [_row_difference(DifferenceType.WORK_ITEM_MISSING, row, "no such work item", status)]

    state: str = row.pdg_item.state
    if status == _SUCCEEDED and state == _COOKED_SUCCESS:
        return []
    if status == _SUCCEEDED:
        return [_row_difference(DifferenceType.RUN_BUT_NOT_REPORTED, row, state, status)]
    if state == _COOKED_SUCCESS:
        return [_row_difference(DifferenceType.REPORTED_BUT_NOT_RUN, row, state, status)]
    # Once the job can't finish, the cook fails every work item it has no result for, so a
    # canceled task reported as failed is right only then.
    if status == _CANCELED and state == _COOKED_FAIL and not job_could_not_finish:
        return [_row_difference(DifferenceType.CANCELED_REPORTED_AS_FAILED, row, state, status)]
    # A failure both sides agree on, or a task that never finished and wasn't reported as a
    # success. The job check already reports the job not succeeding.
    return []


def _work_item_data_differences(row: _TaskRow) -> List[Difference]:
    """A work item data file in the task's uploads that names another work item."""
    # A task without the data file is not a difference: PDG's job scripts write it, not
    # the scheduler.
    names: Set[str] = set()
    for path in row.farm_task.uploaded_files:
        match: Optional[re.Match[str]] = _WORK_ITEM_DATA_FILE.search(path)
        if match is not None:
            names.add(match["name"])
    return [
        _row_difference(DifferenceType.RAN_WRONG_WORK_ITEM, row, row.work_item, name)
        for name in sorted(names - {row.work_item})
    ]


def _file_differences(row: _TaskRow, pdg_dir: str) -> List[Difference]:
    """For a succeeded task, the proof of its work, and its files against PDG's outputs."""
    farm_task: FarmTask = row.farm_task
    uploaded: Set[str] = {os.path.normpath(path) for path in farm_task.uploaded_files}

    # A step that declares its outputs proves its work with an output manifest. The cook
    # hands PDG the declared outputs, so each must have been uploaded.
    if row.declares_outputs:
        if farm_task.manifest_count == 0:
            return [_row_difference(DifferenceType.OUTPUT_PROOF_MISSING, row, "an output manifest")]
        expected: Tuple[OutputFile, ...] = row.pdg_item.outputs if row.pdg_item else ()
        return [
            _row_difference(DifferenceType.OUTPUT_NOT_UPLOADED, row, output.path, _NOT_UPLOADED)
            for output in expected
            if os.path.normpath(output.path) not in uploaded
        ]

    # A step that declares no outputs publishes an output record, and the cook hands PDG
    # the files the record names. Paths are matched normalized, because each side spells
    # them its own way, but reported as the record names them, with PDG's token replaced.
    # Normalizing on Windows also turns every "/" into "\".
    if farm_task.output_record is None:
        return [_row_difference(DifferenceType.OUTPUT_PROOF_MISSING, row, "an output record")]
    record: Dict[str, OutputFile] = {}
    for output in farm_task.output_record:
        path: str = output.path.replace(PDG_DIR_TOKEN, pdg_dir)
        record[os.path.normpath(path)] = OutputFile(path, output.tag)
    differences: List[Difference] = [
        _row_difference(DifferenceType.OUTPUT_NOT_UPLOADED, row, recorded.path, _NOT_UPLOADED)
        for normalized, recorded in record.items()
        if normalized not in uploaded
    ]

    # With PDG's work item, each recorded file must have reached PDG, with the same tag. A
    # file recorded with no tag is handed to PDG with none, and PDG then tags it by its
    # extension, so only a tag the worker set is compared.
    if row.pdg_item is not None:
        pdg_tags: Dict[str, str] = {
            os.path.normpath(output.path): output.tag for output in row.pdg_item.outputs
        }
        for normalized, recorded in record.items():
            if normalized not in pdg_tags:
                differences.append(
                    _row_difference(
                        DifferenceType.OUTPUT_NOT_REPORTED, row, "not a PDG output", recorded.path
                    )
                )
            elif recorded.tag and pdg_tags[normalized] != recorded.tag:
                differences.append(
                    _row_difference(
                        DifferenceType.OUTPUT_TAG_DIFFERS,
                        row,
                        f"{recorded.path}: {pdg_tags[normalized]}",
                        f"{recorded.path}: {recorded.tag}",
                    )
                )
    return differences


def _count_differences(
    cook_json: Mapping[str, Any],
    snapshot_steps: Sequence[Mapping[str, Any]],
    farm: FarmView,
    work_items_by_name: Optional[Mapping[str, PdgWorkItem]],
) -> List[Difference]:
    """The cook's closing counts against the farm's task statuses, and against PDG's states."""
    succeeded: int = int(cook_json.get("succeeded", 0))
    failed: int = int(cook_json.get("failed", 0))
    canceled: int = int(cook_json.get("canceled", 0))
    farm_counts: Counter[str] = Counter()
    for farm_step in farm.steps.values():
        farm_counts.update(farm_step.task_run_status_counts)

    # The cook can't have reported more successes than the farm had, or fewer, and can't
    # have counted more cancellations than the farm made.
    differences: List[Difference] = []
    if succeeded != farm_counts[_SUCCEEDED]:
        differences.append(
            _count_difference(f"{succeeded} succeeded", f"{farm_counts[_SUCCEEDED]} SUCCEEDED")
        )
    if canceled > farm_counts[_CANCELED]:
        differences.append(
            _count_difference(f"{canceled} canceled", f"{farm_counts[_CANCELED]} CANCELED")
        )

    # The counts are what the cook says it told PDG, so PDG's states must agree.
    if work_items_by_name is not None:
        states: Counter[str] = Counter(
            work_items_by_name[name].state
            for step in snapshot_steps
            for name in step["items"]
            if name in work_items_by_name
        )
        if succeeded != states[_COOKED_SUCCESS]:
            differences.append(
                _count_difference(
                    f"{succeeded} succeeded", f"PDG has {states[_COOKED_SUCCESS]} {_COOKED_SUCCESS}"
                )
            )
        if failed != states[_COOKED_FAIL]:
            differences.append(
                _count_difference(
                    f"{failed} failed", f"PDG has {states[_COOKED_FAIL]} {_COOKED_FAIL}"
                )
            )
    return differences


def _sessions_that_ran_more_than_one_step(farm: FarmView) -> int:
    """How many sessions last ran tasks of more than one step. A retried task shows only its last."""
    steps_by_session: DefaultDict[str, Set[str]] = defaultdict(set)
    for step_name, farm_step in farm.steps.items():
        for farm_task in farm_step.tasks.values():
            if farm_task.session_id:
                steps_by_session[farm_task.session_id].add(step_name)
    return sum(1 for steps in steps_by_session.values() if len(steps) > 1)


def _row_difference(
    difference_type: DifferenceType, row: _TaskRow, cook_says: str, farm_says: str = ""
) -> Difference:
    """A difference about one task and its work item."""
    return Difference(
        difference_type,
        step=row.step,
        task_index=row.task_index,
        work_item=row.work_item,
        cook_says=cook_says,
        farm_says=farm_says,
    )


def _count_difference(cook_says: str, farm_says: str) -> Difference:
    """A difference in the cook's closing counts."""
    return Difference(DifferenceType.STATUS_COUNTS, cook_says=cook_says, farm_says=farm_says)


def _describe(difference: Difference) -> str:
    """One line, such as ``reported_but_not_run: render[4] (render_37): cook says ..., farm says ...``."""
    # Where: the step, the task index and the work item, when the difference has them.
    where: str = difference.step
    if difference.task_index is not None:
        where += f"[{difference.task_index}]"
    if difference.work_item:
        where += f" ({difference.work_item})"
    # What each side says, leaving out a side with nothing to say.
    sides: List[str] = []
    if difference.cook_says:
        sides.append(f"cook says {difference.cook_says}")
    if difference.farm_says:
        sides.append(f"farm says {difference.farm_says}")
    parts: List[str] = [part for part in (where, ", ".join(sides)) if part]
    return f"{difference.difference_type.value}: {': '.join(parts)}"


def _report(comparison: CookFarmComparison) -> Dict[str, Any]:
    """The comparison as the JSON report the command writes."""
    return {
        "version": _REPORT_VERSION,
        "job_id": comparison.job_id,
        "passed": comparison.passed,
        "used_pdg_work_items": comparison.used_pdg_work_items,
        "differences": [
            {
                "difference_type": d.difference_type.value,
                "step": d.step,
                "task_index": d.task_index,
                "work_item": d.work_item,
                "cook_says": d.cook_says,
                "farm_says": d.farm_says,
            }
            for d in comparison.differences
        ],
        "observations": dict(comparison.observations),
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
