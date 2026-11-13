# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for how a refusal test judges a cook that must be refused, with no Houdini or farm.

They cover pairing the scene's expected refusals with the ones the cook logged, the
error that must be on the scheduler node, the test's failure message, and finding and
canceling a job the cook made. The Deadline Cloud client is ``SearchableQueue``, which
answers SearchJobs from a list of jobs and records UpdateJob. Nothing else is faked.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Sequence, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from test.integ.pdg.farm import FarmQueue
from test.integ.pdg.run_cook import CookResult, LoggedRefusal, cancel_jobs_of_refused_cook
from test.integ.pdg.run_cook import match_refusals, read_cook_id, refusal_failure
from test.integ.pdg.run_cook import refusal_problems
from test.integ.pdg.scenes import ExpectedRefusal, SceneKind, SceneSet, TestScene

from .searchable_queue import FARM_ID, QUEUE_ID, SearchableQueue, searchable_job

# The error a refusal puts on the scheduler node, as the scheduler writes it.
_REFUSAL_ERROR: str = (
    "Deadline Cloud Scheduler (Experimental): The graph was refused (1 refusal(s)):\n"
    "- write_batched_ropfetch1: 10 of this node's work items are batched."
)

# The time a cook in these tests started and returned at.
_TIME: datetime = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)

# The hython log of a cook in these tests. Tests compare with its str(), which uses the
# running platform's separator, so they pass on Windows too.
_HYTHON_LOG: Path = Path("/cooks/rop_batch/hython.log")


def _scene(*expected: ExpectedRefusal) -> TestScene:
    """A refusal scene that expects these refusals."""
    return TestScene(
        scene_id="rop_batch",
        kind=SceneKind.REFUSAL,
        scene_set=SceneSet.SMOKE,
        builder="rop_batch.py",
        external=None,
        sha256=None,
        saved=True,
        cook_node=None,
        requires=(),
        max_minutes=1.0,
        expected_outcome="refused",
        expected_steps=(),
        expected_refusals=tuple(expected),
        expected_host_requirements=None,
    )


def _cook(*logged: LoggedRefusal, errors: Tuple[str, ...] = (_REFUSAL_ERROR,)) -> CookResult:
    """A cook that ended in time, logged these refusals, and left these errors on the node."""
    return CookResult(
        hip_file=Path("/cooks/rop_batch/rop_batch.hip"),
        job_name="pdg-integ-rop_batch-0a1b2c3d",
        started_at=_TIME,
        returned_at=_TIME,
        timed_out=False,
        start_refused=False,
        debug_dir=None,
        logged_refusals=tuple(logged),
        scheduler_errors=errors,
        pdg_work_items=(),
        hython_log=_HYTHON_LOG,
    )


_BATCH: ExpectedRefusal = ExpectedRefusal("rop_batch", "write_batched")
_BATCH_LOGGED: LoggedRefusal = LoggedRefusal("rop_batch", "write_batched_ropfetch1")


def test_a_cook_refused_as_expected_has_no_problems() -> None:
    """The one expected refusal was logged, and the node shows the refusal."""
    assert refusal_problems(_scene(_BATCH), _cook(_BATCH_LOGGED)) == []


def test_a_cook_that_logged_no_refusal_is_a_problem() -> None:
    """A scene the scheduler stopped refusing fails, naming the refusal it expected."""
    assert refusal_problems(_scene(_BATCH), _cook()) == [
        "Not logged: a rop_batch refusal whose subject contains 'write_batched'."
    ]


def test_a_refusal_for_another_reason_is_a_problem() -> None:
    """A cook refused for the wrong scenario fails on both sides: the expected refusal is
    missing, and the logged one wasn't expected.
    """
    logged: LoggedRefusal = LoggedRefusal("service_node", "write_batched_ropfetch1")
    assert refusal_problems(_scene(_BATCH), _cook(logged)) == [
        "Not logged: a rop_batch refusal whose subject contains 'write_batched'.",
        "Logged but not expected: write_batched_ropfetch1 (service_node).",
    ]


def test_a_refusal_of_another_node_is_a_problem() -> None:
    """The right scenario on a node whose name doesn't hold the expected subject fails."""
    logged: LoggedRefusal = LoggedRefusal("rop_batch", "write_other_ropfetch1")
    assert len(refusal_problems(_scene(_BATCH), _cook(logged))) == 2


def test_an_extra_refusal_is_a_problem() -> None:
    """A builder that triggers a second refusal by accident fails, naming the extra one."""
    extra: LoggedRefusal = LoggedRefusal("cached_outputs", "write_batched_ropfetch1")
    assert refusal_problems(_scene(_BATCH), _cook(_BATCH_LOGGED, extra)) == [
        "Logged but not expected: write_batched_ropfetch1 (cached_outputs)."
    ]


def test_one_logged_refusal_meets_only_one_expected_refusal() -> None:
    """A scene that expects the same refusal twice needs it logged twice."""
    assert refusal_problems(_scene(_BATCH, _BATCH), _cook(_BATCH_LOGGED)) == [
        "Not logged: a rop_batch refusal whose subject contains 'write_batched'."
    ]


@pytest.mark.parametrize(
    "errors",
    [
        pytest.param((), id="no_error"),
        pytest.param(("Deadline Cloud Scheduler (Experimental): Set a farm.",), id="setup_error"),
    ],
)
def test_a_node_without_the_refusal_error_is_a_problem(errors: Tuple[str, ...]) -> None:
    """The node must show the refusal itself. A setup error on the node is not a refusal."""
    assert refusal_problems(_scene(_BATCH), _cook(_BATCH_LOGGED, errors=errors)) == [
        "No error on the scheduler node holds 'The graph was refused ('."
    ]


def test_the_refusal_error_may_be_one_of_several() -> None:
    """The node may show other errors too, as long as one of them is the refusal."""
    errors: Tuple[str, ...] = ("Deadline Cloud Scheduler (Experimental): other", _REFUSAL_ERROR)
    assert refusal_problems(_scene(_BATCH), _cook(_BATCH_LOGGED, errors=errors)) == []


def test_a_cook_that_ran_out_of_time_is_a_problem() -> None:
    """A cook stopped at its time limit fails, saying so, along with what it never logged."""
    timed_out: CookResult = replace(_cook(), timed_out=True, scheduler_errors=())
    assert refusal_problems(_scene(_BATCH), timed_out) == [
        "The cook ran past 1 min.",
        "Not logged: a rop_batch refusal whose subject contains 'write_batched'.",
        "No error on the scheduler node holds 'The graph was refused ('.",
    ]


def test_a_broad_expected_refusal_doesnt_take_the_only_match_of_a_narrow_one() -> None:
    """Two expected refusals where one subject holds the other's text still pair up.

    The artist scene that logs ``filecopy_compiled`` and ``filecopy_compiled1`` needs this:
    pairing in order would give ``filecopy_compiled1`` to the first expected refusal and
    leave the second with nothing.
    """
    expected: List[ExpectedRefusal] = [
        ExpectedRefusal("in_process_step", "filecopy_compiled"),
        ExpectedRefusal("in_process_step", "filecopy_compiled1"),
    ]
    logged: List[LoggedRefusal] = [
        LoggedRefusal("in_process_step", "filecopy_compiled1"),
        LoggedRefusal("in_process_step", "filecopy_compiled"),
    ]
    assert match_refusals(expected, logged) == ((), ())


# Small sets of scenarios and subjects, so random refusals often fit each other.
_SCENARIOS: st.SearchStrategy[str] = st.sampled_from(["rop_batch", "service_node"])
_SUBJECT_TEXT: st.SearchStrategy[str] = st.sampled_from(["", "a", "b", "ab", "ba"])


def _most_pairs(expected: Sequence[ExpectedRefusal], logged: Sequence[LoggedRefusal]) -> int:
    """The most expected refusals that can each have their own logged refusal, found by
    trying every order of the longer list against the shorter one."""

    def fits(want: ExpectedRefusal, refusal: LoggedRefusal) -> bool:
        """Whether the logged refusal is one the expected refusal describes."""
        return want.scenario == refusal.scenario and want.subject_contains in refusal.subject

    # Every order of the longer list, against the shorter one in its own order.
    if len(expected) <= len(logged):
        return max(
            sum(fits(w, r) for w, r in zip(expected, order))
            for order in itertools.permutations(logged)
        )
    return max(
        sum(fits(w, r) for w, r in zip(order, logged)) for order in itertools.permutations(expected)
    )


@settings(max_examples=200)
@given(
    expected=st.lists(st.builds(ExpectedRefusal, _SCENARIOS, _SUBJECT_TEXT), max_size=4),
    logged=st.lists(st.builds(LoggedRefusal, _SCENARIOS, _SUBJECT_TEXT), max_size=4),
)
def test_any_refusals_are_paired_as_fully_as_possible(
    expected: List[ExpectedRefusal], logged: List[LoggedRefusal]
) -> None:
    """For any expected and logged refusals, as many pairs are made as a search of every
    pairing finds, and what is left over on each side is what wasn't paired.

    A test that pairs fewer would fail a cook that logged exactly what the scene expects.
    """
    missing, extra = match_refusals(expected, logged)
    pairs: int = _most_pairs(expected, logged)

    assert len(missing) == len(expected) - pairs
    assert len(extra) == len(logged) - pairs
    # The refusals not left over really do pair up with each other, one to one.
    paired_expected: List[ExpectedRefusal] = _without(expected, missing)
    paired_logged: List[LoggedRefusal] = _without(logged, extra)
    assert _most_pairs(paired_expected, paired_logged) == pairs


def _without(items: Sequence[Any], removed: Sequence[Any]) -> List[Any]:
    """``items`` with each of ``removed`` taken out once. Fails if one isn't in ``items``."""
    kept: List[Any] = list(items)
    for item in removed:
        kept.remove(item)
    return kept


def test_the_cook_id_is_read_from_cook_json(tmp_path: Path) -> None:
    """A cook refused inside the cook has an ID, which a job of that cook would carry."""
    (tmp_path / "cook.json").write_text(
        json.dumps({"cook_id": "1ad65a5137e24009abd5a68f86e94763", "outcome": "refused"}),
        encoding="utf-8",
    )
    assert read_cook_id(tmp_path) == "1ad65a5137e24009abd5a68f86e94763"


def test_a_cook_without_a_debug_directory_has_no_id() -> None:
    """A refusal at the cook's start writes no debug directory, so the search uses the name only."""
    assert read_cook_id(None) is None


def test_a_debug_directory_without_cook_json_has_no_id(tmp_path: Path) -> None:
    """A debug directory with no cook.json in it gives no ID, rather than an error."""
    assert read_cook_id(tmp_path) is None


_ROP_BATCH_JOB: str = "job-rop-batch"
_COOK_ID: str = "1ad65a5137e24009abd5a68f86e94763"


def test_a_refused_cook_with_no_job_and_its_refusal_shown_passes() -> None:
    """A cook refused as expected, with no job on the queue, gives no failure."""
    assert refusal_failure(_scene(_BATCH), _cook(_BATCH_LOGGED), canceled_jobs=[]) == ""


def test_the_failure_names_each_problem_and_where_to_look() -> None:
    """A refusal test that fails says what is wrong, and gives the cook's job name and log.

    Without both, a developer can't tell which refusal went missing, or find the cook.
    """
    logged: LoggedRefusal = LoggedRefusal("cached_outputs", "write_cached")
    failure: str = refusal_failure(_scene(_BATCH), _cook(logged), canceled_jobs=[])

    # Each problem on its own line, then the cook's details.
    assert failure.splitlines()[:2] == [
        "Not logged: a rop_batch refusal whose subject contains 'write_batched'.",
        "Logged but not expected: write_cached (cached_outputs).",
    ]
    assert "Job name: pdg-integ-rop_batch-0a1b2c3d" in failure
    assert f"hython log: {_HYTHON_LOG}" in failure


def test_a_cook_that_made_a_job_fails_naming_it_even_when_refused_as_expected() -> None:
    """A cook that logged its refusal but still made a job fails the test, naming the job.

    A refusal that still submits a job is the worst case the test exists to catch.
    """
    failure: str = refusal_failure(
        _scene(_BATCH), _cook(_BATCH_LOGGED), canceled_jobs=[_ROP_BATCH_JOB]
    )
    assert failure.splitlines()[0] == f"The cook made job(s) {_ROP_BATCH_JOB}, now canceled."


def test_a_job_is_named_before_the_refusal_problems_of_the_same_cook() -> None:
    """A cook that made a job and logged no refusal fails with both, the job first.

    The job is the worse problem, so it leads the message, and the missing refusal is
    still listed.
    """
    failure: str = refusal_failure(_scene(_BATCH), _cook(), canceled_jobs=[_ROP_BATCH_JOB])
    assert failure.splitlines()[:2] == [
        f"The cook made job(s) {_ROP_BATCH_JOB}, now canceled.",
        "Not logged: a rop_batch refusal whose subject contains 'write_batched'.",
    ]


def _cancel_for(cook: CookResult, deadline: SearchableQueue) -> List[str]:
    """``cancel_jobs_of_refused_cook`` for this cook, in a test that started at ``_TIME``."""
    return cancel_jobs_of_refused_cook(
        FarmQueue(FARM_ID, QUEUE_ID), cook, test_started_at=_TIME, deadline_client=deadline
    )


def test_a_job_with_the_cooks_name_from_just_before_the_test_is_found_and_canceled() -> None:
    """A job the service dates a minute before the workstation's start is still this cook's.

    A workstation clock ahead of the service's would otherwise hide the job, and the job
    would keep running on the shared queue.
    """
    # The job carries the cook's name, but not its ID: a refusal at the start has none.
    cook: CookResult = _cook(_BATCH_LOGGED)
    created: datetime = _TIME - timedelta(minutes=1)
    deadline: SearchableQueue = SearchableQueue(
        [searchable_job(_ROP_BATCH_JOB, created, name=cook.job_name, cook_id="another-cook")]
    )

    assert _cancel_for(cook, deadline) == [_ROP_BATCH_JOB]
    assert deadline.updates == [
        {
            "farmId": FARM_ID,
            "queueId": QUEUE_ID,
            "jobId": _ROP_BATCH_JOB,
            "targetTaskRunStatus": "CANCELED",
        }
    ]


def test_a_job_found_only_by_the_cooks_id_is_canceled(tmp_path: Path) -> None:
    """A job that carries the ID in the cook's cook.json is canceled, whatever its name.

    A cook refused inside the cook has an ID, and a job it made would carry it.
    """
    # The cook's debug directory names its ID, and the job has another name.
    (tmp_path / "cook.json").write_text(json.dumps({"cook_id": _COOK_ID}), encoding="utf-8")
    cook: CookResult = replace(_cook(_BATCH_LOGGED), debug_dir=tmp_path)
    deadline: SearchableQueue = SearchableQueue(
        [searchable_job(_ROP_BATCH_JOB, _TIME, name="renamed", cook_id=_COOK_ID)]
    )

    assert _cancel_for(cook, deadline) == [_ROP_BATCH_JOB]
    assert [update["jobId"] for update in deadline.updates] == [_ROP_BATCH_JOB]


def test_another_cooks_job_is_neither_returned_nor_canceled() -> None:
    """A job on the shared queue with another name and cook ID is someone else's, and is left alone."""
    deadline: SearchableQueue = SearchableQueue(
        [searchable_job("job-other", _TIME, name="pdg-integ-rop_batch-ffffffff", cook_id="x")]
    )

    assert _cancel_for(_cook(_BATCH_LOGGED), deadline) == []
    assert deadline.updates == []
