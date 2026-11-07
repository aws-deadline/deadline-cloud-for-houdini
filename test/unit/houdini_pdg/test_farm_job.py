# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for releasing a job's tasks and finding their results through the Deadline Cloud API.

The Deadline Cloud client is the only thing faked: a dict-backed farm that checks
every request against botocore's own model of the service, so a request the real
service would refuse fails the test. The worker log reader is a fake passed to the
constructor. The tests drive ``DeadlineFarmJob`` through its public methods, and
read what it did from the requests the fake farm received.
"""

from __future__ import annotations

# Standard library, botocore for the service model and its error type, hypothesis
# for the "for any" properties, and the public surface of ``errors``, ``graph``,
# ``ports``, and ``farm_job``.
import hashlib
import inspect
import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.model import ServiceModel
from botocore.validate import ParamValidator
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from typing_extensions import TypeAlias

from deadline.client import api
from deadline.houdini_pdg.errors import TaskMapIncompleteError, TaskSuspensionError
from deadline.houdini_pdg.farm_job import DeadlineFarmJob
from deadline.houdini_pdg.graph import GraphSnapshot, StepSpec, TaskKey, WorkItemSpec
from deadline.houdini_pdg.ports import FarmJob, JobStatus, TaskOutcome, TaskStatus

_MODEL: ServiceModel = botocore.session.get_session().get_service_model("deadline")
_T0: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
FARM: str = "farm-" + "f" * 32
QUEUE: str = "queue-" + "e" * 32
JOB: str = "job-" + "d" * 32

# Steps as (name, task count, dependencies): a chain of three.
StepsShape: TypeAlias = Sequence[Tuple[str, int, Sequence[str]]]
CHAIN: StepsShape = [("a", 3, ()), ("b", 2, ("a",)), ("c", 1, ("b",))]

# The statuses the service reports for a task that has ended.
FINAL_STATUSES: List[str] = ["CANCELED", "FAILED", "NOT_COMPATIBLE", "SUCCEEDED"]
# The statuses the service reports for a task a worker is running or is about to run.
IN_PROGRESS_STATUSES: List[str] = ["ASSIGNED", "STARTING", "SCHEDULED", "RUNNING", "INTERRUPTING"]


def _hex(text: str) -> str:
    """A 32-character hex body, the shape the service uses in its IDs."""
    return hashlib.md5(text.encode()).hexdigest()


def step_id_of(name: str) -> str:
    """The fake farm's ID for the step with this name."""
    return "step-" + _hex(name)


def task_id_of(name: str, index: int) -> str:
    """The fake farm's ID for a task: its step's hex body and its index, as the service makes them."""
    return f"task-{_hex(name)}-{index}"


def _client_error(code: str, operation: str) -> ClientError:
    """The error botocore raises for an error response with this code."""
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class Clock:
    """A clock that moves only when a test moves it."""

    def __init__(self) -> None:
        self.at: datetime = _T0

    def now(self) -> datetime:
        """The current fake time."""
        return self.at

    def advance(self, seconds: float) -> None:
        """Move the fake time forward."""
        self.at += timedelta(seconds=seconds)


def snapshot_of(steps: StepsShape) -> GraphSnapshot:
    """A snapshot with one work item per task, in the given steps."""
    return GraphSnapshot(
        steps=tuple(
            StepSpec(
                name=name,
                dependencies=tuple(deps),
                items=tuple(
                    WorkItemSpec(
                        name=f"{name}_{i}", task_index=i, pdg_index=i, item_id=i, node_name=name
                    )
                    for i in range(count)
                ),
            )
            for name, count, deps in steps
        )
    )


class FakeDeadline:
    """A dict-backed Deadline Cloud client. Every request is checked against botocore's model.

    Tests steer it through its public attributes: ``faults`` makes an operation
    raise, ``rejected`` makes ``BatchUpdateTask`` report those task IDs as errors,
    ``refused_job_statuses`` makes ``UpdateJob`` refuse those targets,
    ``listing_extras`` adds entries to the first step page, ``lost`` makes
    ``BatchGetTask`` answer those task IDs with an error,
    ``extra_results`` adds results to every first search page, ``endless_search``
    makes every search page full, ``fail_search_at_offset`` makes the search fail
    from that offset on, ``reconcile_blind`` makes ``BatchGetTask`` show no task as ended,
    ``malformed`` replaces an operation's response, and ``skew`` puts the
    service's clock ahead of the job's.

    As on the service, ``BatchUpdateTask`` sets a finished task back to ``READY``,
    so it runs again. The search index still shows that task's last result
    until the task ends again.
    """

    def __init__(
        self,
        snapshot: GraphSnapshot,
        clock: Clock,
        *,
        page_size: int = 2,
        max_retries: Optional[int] = 1,
        validate: bool = True,
    ) -> None:
        # Settings, and the record of every request in call order.
        self.clock: Clock = clock
        self.page_size: int = page_size
        self.max_retries: Optional[int] = max_retries
        self.validate: bool = validate
        # How far the service's clock, which stamps endedAt, is ahead of the job's.
        self.skew: timedelta = timedelta(0)
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

        # What tests can make go wrong.
        self.faults: Dict[str, Exception] = {}
        self.rejected: Set[str] = set()
        self.refused_job_statuses: Set[str] = set()
        self.extra_results: List[Dict[str, Any]] = []
        self.endless_search: bool = False
        self.fail_search_at_offset: Optional[int] = None
        self.reconcile_blind: bool = False
        self.lost: Set[str] = set()
        self.listing_extras: List[Dict[str, Any]] = []
        self.index_override: Dict[str, Any] = {}
        self.malformed: Dict[str, Any] = {}
        # Set by a test to hold BatchUpdateTask until released, and set by the
        # fake when such a call has started.
        self.hold_activation: Optional[threading.Event] = None
        self.activation_started: threading.Event = threading.Event()

        # What the job learned and did: the last step listing it was served, and
        # each activation entry with the step listing it had read by then.
        self.last_step_read: Dict[str, Tuple[str, str]] = {}
        self.ready_requests: List[Tuple[str, Dict[str, Tuple[str, str]]]] = []
        self.accepted: List[str] = []
        self.refused: Set[str] = set()

        # The farm itself: the job, its steps, and its tasks, all suspended.
        self.job: Dict[str, Any] = {
            "lifecycleStatus": "CREATE_COMPLETE",
            "taskRunStatus": "SUSPENDED",
            "taskRunStatusCounts": {"SUSPENDED": snapshot.total_tasks},
        }
        self.steps: Dict[str, Dict[str, Any]] = {}
        self.tasks: Dict[str, Dict[str, Any]] = {}
        # The last result of each finished task set back to READY, by task ID,
        # which the search index still shows.
        self.stale_results: Dict[str, Dict[str, Any]] = {}
        self.session_actions: Dict[str, List[Dict[str, Any]]] = {}
        for step in snapshot.steps:
            self.steps[step.name] = {
                "name": step.name,
                "stepId": step_id_of(step.name),
                "lifecycle": "CREATE_COMPLETE",
            }
            for i in range(step.task_count):
                self.tasks[task_id_of(step.name, i)] = {
                    "stepId": step_id_of(step.name),
                    "taskId": task_id_of(step.name, i),
                    "runStatus": "SUSPENDED",
                    "parameters": {"TaskIndex": {"int": str(i)}},
                    "published": None,
                }

    # -- the fake's own controls --

    def finish(
        self,
        name: str,
        index: int,
        status: str = "SUCCEEDED",
        *,
        ended: Optional[datetime] = None,
        lag: float = 0.0,
        retries_used: Optional[int] = None,
    ) -> None:
        """End a task on the farm. The search index shows it ``lag`` seconds after it ended."""
        # The end time defaults to now, and the service stamps it on its own
        # clock, ``skew`` ahead of the job's. The retry count is set only when
        # given, as the service omits it for a task that never failed. A new
        # result replaces any stale one in the index.
        task: Dict[str, Any] = self.tasks[task_id_of(name, index)]
        end: datetime = ended or self.clock.now()
        task.update(
            runStatus=status, endedAt=end + self.skew, published=end + timedelta(seconds=lag)
        )
        self.stale_results.pop(task["taskId"], None)
        if retries_used is not None:
            task["failureRetryCount"] = retries_used

    def step_status(self, name: str) -> str:
        """A step's run status, from its tasks, as the service sums them up."""
        # Succeeded only when every task did; a failure or a suspended task wins next.
        statuses: Set[str] = {
            t["runStatus"] for t in self.tasks.values() if t["stepId"] == step_id_of(name)
        }
        if statuses == {"SUCCEEDED"}:
            return "SUCCEEDED"
        if "FAILED" in statuses:
            return "FAILED"
        return "SUSPENDED" if "SUSPENDED" in statuses else "RUNNING"

    def ops(self, name: str) -> List[Dict[str, Any]]:
        """The parameters of every request of one operation, in call order."""
        return [params for op, params in self.calls if op == name]

    # -- the API --

    def _call(self, op: str, params: Dict[str, Any]) -> None:
        """Check the request against the service model, record it, and raise a planted fault."""
        # A request the real service would refuse fails the test here.
        if self.validate:
            report: Any = ParamValidator().validate(params, _MODEL.operation_model(op).input_shape)
            assert not report.has_errors(), report.generate_report()
        assert params.get("farmId", FARM) == FARM
        self.calls.append((op, params))
        fault: Optional[Exception] = self.faults.get(op)
        if fault is not None:
            raise fault

    def list_steps(self, **params: Any) -> Dict[str, Any]:
        """One page of steps, with each step's run and lifecycle status."""
        # A planted response replaces the real one, after the request is checked.
        self._call("ListSteps", params)
        if "ListSteps" in self.malformed:
            return self.malformed["ListSteps"]

        # Pages by position; the token is the next position.
        names: List[str] = list(self.steps)
        start: int = int(params.get("nextToken") or 0)
        page: List[Dict[str, Any]] = [
            {
                "name": n,
                "stepId": self.steps[n]["stepId"],
                "taskRunStatus": self.step_status(n),
                "lifecycleStatus": self.steps[n]["lifecycle"],
            }
            for n in names[start : start + self.page_size]
        ]

        # Remember what the job was told, for checks on what it does next.
        if start == 0:
            self.last_step_read = {}
        self.last_step_read.update(
            {s["name"]: (s["taskRunStatus"], s["lifecycleStatus"]) for s in page}
        )

        # Every page but the last names the next. A test's extra entries come
        # on the first page.
        response: Dict[str, Any] = {"steps": page + (self.listing_extras if start == 0 else [])}
        if start + self.page_size < len(names):
            response["nextToken"] = str(start + self.page_size)
        return response

    def get_task(self, **params: Any) -> Dict[str, Any]:
        """One task, or the service's not-found error."""
        # A test can make the task report another index.
        self._call("GetTask", params)
        task: Optional[Dict[str, Any]] = self.tasks.get(params["taskId"])
        if task is None or task["stepId"] != params["stepId"]:
            raise _client_error("ResourceNotFoundException", "GetTask")
        if params["taskId"] in self.index_override:
            return {**task, "parameters": {"TaskIndex": self.index_override[params["taskId"]]}}
        return dict(task)

    def update_step(self, **params: Any) -> Dict[str, Any]:
        """Accept a step update."""
        self._call("UpdateStep", params)
        return {}

    def batch_update_task(self, **params: Any) -> Dict[str, Any]:
        """Mark suspended and finished tasks ready, except the ones a test rejects."""
        # A raising call refuses every task in it. Each entry is logged with the
        # step listing the job had read when it asked.
        ids: List[str] = [entry["taskId"] for entry in params["tasks"]]
        for task_id in ids:
            self.ready_requests.append((task_id, dict(self.last_step_read)))
        if "BatchUpdateTask" in self.faults:
            self.refused.update(ids)
        self._call("BatchUpdateTask", params)

        # Optionally wait, so a test can call the job from another thread meanwhile.
        if self.hold_activation is not None:
            self.activation_started.set()
            self.hold_activation.wait(10)

        # Rejected tasks come back as errors and stay as they were.
        errors: List[Dict[str, Any]] = []
        for entry, task_id in zip(params["tasks"], ids):
            if task_id in self.rejected:
                self.refused.add(task_id)
                errors.append({**entry, "code": "ConflictException", "message": "busy"})
                continue
            self.accepted.append(task_id)
            self._set_ready(self.tasks[task_id])
        if "BatchUpdateTask" in self.malformed:
            return self.malformed["BatchUpdateTask"]
        return {"errors": errors}

    def _set_ready(self, task: Dict[str, Any]) -> None:
        """Make a suspended or finished task ready to run; leave a waiting or running one alone."""
        # A finished task runs again, and its last result stays in the index.
        if task["runStatus"] in FINAL_STATUSES:
            self.stale_results[task["taskId"]] = dict(task)
            task.update(runStatus="READY", published=None)
        elif task["runStatus"] == "SUSPENDED":
            task["runStatus"] = "READY"

    def search_tasks(self, **params: Any) -> Dict[str, Any]:
        """Tasks the index has published, filtered and sorted as the request asks."""
        self._call("SearchTasks", params)
        assert params["queueIds"] == [QUEUE] and params["jobId"] == JOB
        if "SearchTasks" in self.malformed:
            return self.malformed["SearchTasks"]
        offset: int = params["itemOffset"]
        size: int = params["pageSize"]
        if self.fail_search_at_offset is not None and offset >= self.fail_search_at_offset:
            raise _client_error("ThrottlingException", "SearchTasks")

        # Full pages forever, of results no job can map.
        if self.endless_search:
            return {
                "tasks": [
                    {
                        "stepId": "step-x",
                        "taskId": "task-x",
                        "runStatus": "SUCCEEDED",
                        "endedAt": _T0 + timedelta(seconds=offset + i),
                    }
                    for i in range(size)
                ],
                "nextItemOffset": offset + size,
                "totalResults": 10_000,
            }

        # Read the two filters the request may carry, and check they are the
        # only kinds it carries.
        filters: List[Dict[str, Any]] = params["filterExpressions"]["filters"]
        lists: List[Dict[str, Any]] = [
            f["stringListFilter"] for f in filters if "stringListFilter" in f
        ]
        dates: List[Dict[str, Any]] = [
            f["dateTimeFilter"] for f in filters if "dateTimeFilter" in f
        ]
        assert len(lists) == 1 and len(dates) <= 1 and len(filters) == len(lists) + len(dates)
        assert lists[0]["name"] == "RUN_STATUS"
        assert all(d["name"] == "ENDED_AT" and d["operator"] == "GREATER_THAN" for d in dates)
        statuses: Set[str] = set(lists[0]["values"])
        after: Optional[datetime] = dates[0]["dateTime"] if dates else None
        assert params["sortExpressions"] == [
            {"fieldSort": {"name": "ENDED_AT", "sortOrder": "ASCENDING"}}
        ]

        # Only what the index has published by now, ended after the search's
        # time filter, oldest first. Stale results count as published.
        now: datetime = self.clock.now()
        visible: List[Dict[str, Any]] = sorted(
            (
                t
                for t in [*self.tasks.values(), *self.stale_results.values()]
                if t["runStatus"] in statuses
                and t["published"] is not None
                and t["published"] <= now
                and (after is None or t["endedAt"] > after)
            ),
            key=lambda t: t["endedAt"],
        )
        # A test's extra results come first on the first page, whatever the filters say.
        extras: List[Dict[str, Any]] = list(self.extra_results) if offset == 0 else []
        page: List[Dict[str, Any]] = extras + [
            {k: v for k, v in t.items() if k != "published"}
            for t in visible[offset : offset + size]
        ]

        # Every page but the last names the next offset.
        response: Dict[str, Any] = {"tasks": page, "totalResults": min(len(visible), 10_000)}
        if offset + size < len(visible):
            response["nextItemOffset"] = offset + size
        return response

    def batch_get_task(self, **params: Any) -> Dict[str, Any]:
        """The current state of each task asked for, published or not."""
        # A lost task comes back as an error, the rest as they are. A blind farm
        # shows every ended task as still running, so only the search can
        # report results.
        self._call("BatchGetTask", params)
        if "BatchGetTask" in self.malformed:
            return self.malformed["BatchGetTask"]
        idents: List[Dict[str, str]] = params["identifiers"]
        tasks: List[Dict[str, Any]] = [
            self._as_read(self.tasks[ident["taskId"]])
            for ident in idents
            if ident["taskId"] not in self.lost
        ]
        errors: List[Dict[str, Any]] = [
            {**ident, "code": "ResourceNotFoundException", "message": "gone"}
            for ident in idents
            if ident["taskId"] in self.lost
        ]
        return {"tasks": tasks, "errors": errors}

    def _as_read(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """A task as ``BatchGetTask`` returns it, without the fake's own fields."""
        read: Dict[str, Any] = {k: v for k, v in task.items() if k != "published"}
        if self.reconcile_blind and read["runStatus"] in FINAL_STATUSES:
            read["runStatus"] = "RUNNING"
            read.pop("endedAt", None)
        return read

    def get_job(self, **params: Any) -> Dict[str, Any]:
        """The job, with its retry budget when it has one."""
        # A planted response replaces the real one, after the request is checked.
        self._call("GetJob", params)
        if "GetJob" in self.malformed:
            return self.malformed["GetJob"]
        budget: Dict[str, Any] = (
            {} if self.max_retries is None else {"maxRetriesPerTask": self.max_retries}
        )
        return {**self.job, **budget}

    def update_job(self, **params: Any) -> Dict[str, Any]:
        """Accept a job update, unless a test makes the service refuse its target."""
        self._call("UpdateJob", params)
        if params.get("targetTaskRunStatus") in self.refused_job_statuses:
            raise _client_error("ValidationException", "UpdateJob")
        return {}

    def list_session_actions(self, **params: Any) -> Dict[str, Any]:
        """The session actions planted for the task."""
        self._call("ListSessionActions", params)
        return {"sessionActions": self.session_actions.get(params.get("taskId", ""), [])}


def make(
    steps: StepsShape, *, log_reader: Any = None, **fake_options: Any
) -> Tuple[DeadlineFarmJob, FakeDeadline, Clock, GraphSnapshot]:
    """A farm job over a fake farm holding these steps, not yet started."""
    # The log reader is passed only when a test supplies one, so every other job
    # is built the way the scheduler builds it.
    clock: Clock = Clock()
    snapshot: GraphSnapshot = snapshot_of(steps)
    fake: FakeDeadline = FakeDeadline(snapshot, clock, **fake_options)
    reader: Dict[str, Any] = {} if log_reader is None else {"get_session_logs": log_reader}
    job: DeadlineFarmJob = DeadlineFarmJob(
        deadline_client=fake,
        farm_id=FARM,
        queue_id=QUEUE,
        job_id=JOB,
        snapshot=snapshot,
        clock=clock,
        **reader,
    )
    return job, fake, clock, snapshot


def started(
    steps: StepsShape = CHAIN, **options: Any
) -> Tuple[DeadlineFarmJob, FakeDeadline, Clock, GraphSnapshot]:
    """A started farm job, with the fake's record of the start cleared."""
    # The step listing start reads is not one the release gate uses, so it is forgotten too.
    job, fake, clock, snapshot = make(steps, **options)
    job.start()
    fake.calls.clear()
    fake.last_step_read = {}
    return job, fake, clock, snapshot


def requested_ids(fake: FakeDeadline) -> List[str]:
    """Every task ID the job asked to activate, in order, whatever the answer."""
    return [t["taskId"] for call in fake.ops("BatchUpdateTask") for t in call["tasks"]]


def ended_filter(fake: FakeDeadline) -> Optional[datetime]:
    """The ``ENDED_AT`` bound of the last search, or None when it had none."""
    filters: List[Dict[str, Any]] = fake.ops("SearchTasks")[-1]["filterExpressions"]["filters"]
    bounds: List[datetime] = [
        f["dateTimeFilter"]["dateTime"] for f in filters if "dateTimeFilter" in f
    ]
    return bounds[0] if bounds else None


def statuses_of(outcomes: List[TaskOutcome]) -> List[Tuple[str, int, TaskStatus]]:
    """Outcomes as sorted (step, index, status) triples."""
    return sorted((o.key.step_name, o.key.task_index, o.status) for o in outcomes)


# -- start --


@st.composite
def step_dags(draw: Any) -> List[Tuple[str, int, Tuple[str, ...]]]:
    """Steps whose dependencies are earlier steps, with 1 to 2,000 tasks each."""
    count: int = draw(st.integers(min_value=1, max_value=6))
    steps: List[Tuple[str, int, Tuple[str, ...]]] = []
    for i in range(count):
        # Only earlier steps can be dependencies, so the steps form a DAG.
        earlier: List[str] = [name for name, _, _ in steps]
        deps: List[str] = draw(st.lists(st.sampled_from(earlier), unique=True)) if earlier else []
        tasks: int = draw(st.integers(min_value=1, max_value=2_000))
        steps.append((f"s{i}", tasks, tuple(sorted(deps))))
    return steps


@settings(max_examples=30, deadline=None)
@given(step_dags())
def test_start_proves_each_step_at_both_ends_and_suspends_only_dependent_steps(
    steps: List[Tuple[str, int, Tuple[str, ...]]],
) -> None:
    """Start reads every page of steps, checks the first and last task of each, then suspends each dependent step once.

    A root step suspended again would overwrite its first activations, and a
    job-wide suspension lands late enough to undo activations made after it.
    """
    job, fake, _, _ = make(steps)
    job.start()

    # Every page of steps, and one GetTask per distinct end of each step.
    assert len(fake.ops("ListSteps")) == -(-len(steps) // fake.page_size)
    gets: List[Dict[str, Any]] = fake.ops("GetTask")
    assert len(gets) == sum(len({0, n - 1}) for _, n, _ in steps)
    assert {(g["stepId"], g["taskId"]) for g in gets} == {
        (step_id_of(name), task_id_of(name, i)) for name, n, _ in steps for i in {0, n - 1}
    }
    assert not any(op == "ListTasks" for op, _ in fake.calls)

    # One suspension per dependent step, none for the job or a root, after the proof.
    suspended: List[str] = [u["stepId"] for u in fake.ops("UpdateStep")]
    assert sorted(suspended) == sorted(step_id_of(n) for n, _, deps in steps if deps)
    assert all(u["targetTaskRunStatus"] == "SUSPENDED" for u in fake.ops("UpdateStep"))
    assert not fake.ops("UpdateJob")
    ops: List[str] = [op for op, _ in fake.calls]
    if "UpdateStep" in ops:
        assert ops.index("UpdateStep") > max(i for i, op in enumerate(ops) if op == "GetTask")


@settings(max_examples=40, deadline=None)
@given(step_dags(), st.data())
def test_start_refuses_the_map_exactly_when_a_step_is_missing_or_an_end_has_another_index(
    steps: List[Tuple[str, int, Tuple[str, ...]]], data: Any
) -> None:
    """The derived task IDs are trusted only when every step is listed and both ends of each carry their own index.

    A wrong ID would store a work item's data where its task never looks, and
    activate the wrong task.
    """
    job, fake, _, _ = make(steps)

    # Some steps go missing, and some ends report another index.
    missing: List[str] = data.draw(st.lists(st.sampled_from([n for n, _, _ in steps]), unique=True))
    for name in missing:
        del fake.steps[name]
    wrong: List[Tuple[str, int]] = data.draw(
        st.lists(st.sampled_from([(n, i) for n, c, _ in steps for i in {0, c - 1}]), unique=True)
    )
    for name, index in wrong:
        fake.index_override[task_id_of(name, index)] = {"int": str(index + 1)}

    # A missing step makes start refuse; so does a wrong index on a listed step.
    broken: bool = bool(missing) or any(name not in missing for name, _ in wrong)
    if broken:
        with pytest.raises(TaskMapIncompleteError):
            job.start()
        assert not fake.ops("UpdateStep")
    else:
        job.start()
    listed_ends: int = sum(len({0, n - 1}) for name, n, _ in steps if name not in missing)
    assert len(fake.ops("GetTask")) == listed_ends


@pytest.mark.parametrize(
    "reported, message",
    [({"int": "1"}, "TaskIndex 1"), ({}, "TaskIndex None"), ("bad", "TaskIndex None")],
)
def test_a_task_index_that_is_wrong_or_unreadable_refuses_the_map(
    reported: Any, message: str
) -> None:
    """A task whose ``TaskIndex`` differs from, or cannot be read as, its derived index refuses the map, naming what it read."""
    # The first task of step a reports something other than index 0.
    job, fake, _, _ = make(CHAIN)
    fake.index_override[task_id_of("a", 0)] = reported
    with pytest.raises(TaskMapIncompleteError, match=message):
        job.start()
    assert not fake.ops("UpdateStep")


def test_one_refusal_names_every_missing_step_and_unreadable_task() -> None:
    """Every problem is in the one error, so the artist does not fix them one cook at a time."""
    # Step b is missing and every GetTask is refused: two ends of a and one of c.
    job, fake, _, _ = make(CHAIN)
    del fake.steps["b"]
    fake.faults["GetTask"] = _client_error("AccessDeniedException", "GetTask")
    with pytest.raises(TaskMapIncompleteError) as raised:
        job.start()
    assert "step 'b' is missing" in str(raised.value)
    assert str(raised.value).count("could not be read: ") == 3


def test_a_listing_entry_without_a_name_or_an_id_is_not_a_step() -> None:
    """An entry in the step listing that lacks a name or an ID is skipped, so it neither names a step nor stops the start."""
    # Entries the job has no use for, each missing one of the two fields.
    job, fake, _, _ = make([("a", 1, ())])
    fake.listing_extras = [{"name": "other"}, {"stepId": step_id_of("other")}]
    job.start()
    assert job.address(TaskKey("a", 0)).step_id == step_id_of("a")


def test_a_failed_step_listing_refuses_the_map() -> None:
    """Without the step IDs no payload can be stored, so a failed listing ends the start with the service's reason."""
    job, fake, _, _ = make(CHAIN)
    fake.faults["ListSteps"] = _client_error("ThrottlingException", "ListSteps")
    with pytest.raises(TaskMapIncompleteError, match="ThrottlingException"):
        job.start()


def test_an_unexpected_step_id_refuses_the_map() -> None:
    """A step ID of another shape cannot give task IDs, so start refuses it rather than derive nonsense."""
    # Unvalidated, because the model refuses the malformed ID on the wire, which
    # is the point: the job must stop before sending it.
    job, fake, _, _ = make(CHAIN, validate=False)
    fake.steps["a"]["stepId"] = "step-NOTHEX"
    with pytest.raises(TaskMapIncompleteError, match="unexpected ID 'step-NOTHEX'"):
        job.start()
    assert not any(g["stepId"] == "step-NOTHEX" for g in fake.ops("GetTask"))


def test_a_failed_suspension_fails_start_after_trying_every_step() -> None:
    """An unsuspended step would run before its payloads exist, so start fails, naming how many steps it could not suspend."""
    # Both dependent steps refuse the suspension.
    job, fake, _, _ = make(CHAIN)
    fake.faults["UpdateStep"] = _client_error("ConflictException", "UpdateStep")
    with pytest.raises(TaskSuspensionError, match="2 step"):
        job.start()
    assert len(fake.ops("UpdateStep")) == 2


# -- task IDs --


@settings(max_examples=30, deadline=None)
@given(step_dags(), st.data())
def test_every_task_address_carries_its_derived_id_and_its_result_maps_back_to_its_key(
    steps: List[Tuple[str, int, Tuple[str, ...]]], data: Any
) -> None:
    """A task's address names the farm, queue, job, step, and ``task-<step hex>-<index>``, and the farm's result for that ID reaches the same key.

    Payloads are stored by address, and results are matched back by ID, so the
    two must agree for every task.
    """
    root_steps: List[Tuple[str, int, Tuple[str, ...]]] = [(n, c, ()) for n, c, _ in steps]
    job, fake, clock, snapshot = started(root_steps)
    for step in snapshot.steps:
        index: int = data.draw(st.integers(min_value=0, max_value=step.task_count - 1))
        key: TaskKey = TaskKey(step.name, index)
        address: Any = job.address(key)
        assert (address.farm_id, address.queue_id, address.job_id) == (FARM, QUEUE, JOB)
        assert address.step_id == step_id_of(step.name)
        assert address.task_id == f"task-{_hex(step.name)}-{index}"

        # The result for that ID comes back as this key.
        job.release(key)
        job.poll()
        fake.finish(step.name, index)
        clock.advance(1)
        assert [o.key for o in job.poll()] == [key]


_NEAR_MISSES: List[Tuple[str, str]] = [
    (step_id_of("a"), task_id_of("a", 3)),
    (step_id_of("a"), f"task-{_hex('a')}-01"),
    (step_id_of("a"), f"task-{_hex('a')}--1"),
    (step_id_of("a"), f"task-{_hex('a')}-"),
    (step_id_of("a"), task_id_of("b", 0)),
    (step_id_of("b"), task_id_of("a", 0)),
    ("step-" + "0" * 32, "task-" + "0" * 32 + "-0"),
    ("", ""),
]


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    ids=st.lists(
        st.one_of(
            st.sampled_from(_NEAR_MISSES), st.tuples(st.text(max_size=50), st.text(max_size=50))
        ),
        min_size=1,
        max_size=5,
    )
)
def test_a_result_with_any_id_the_job_did_not_derive_is_ignored_and_logged(
    ids: List[Tuple[str, str]], caplog: pytest.LogCaptureFixture
) -> None:
    """A result whose IDs name no task of this job is logged once per result and reported to no one, and nothing raises.

    A leading zero, an index past the step's end, or another step's ID must not
    be read as one of this job's tasks.
    """
    # Every released task is in the job; the planted results are not.
    job, fake, _, _ = started([("a", 3, ()), ("b", 1, ())])
    for name, index in [("a", 0), ("a", 1), ("a", 2), ("b", 0)]:
        job.release(TaskKey(name, index))
    fake.extra_results = [
        {"stepId": step, "taskId": task, "runStatus": "SUCCEEDED", "endedAt": _T0}
        for step, task in ids
    ]
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="deadline.houdini_pdg"):
        assert job.poll() == []
    assert caplog.text.count("is not one of this job's tasks") == len(ids)


def test_an_address_outside_the_job_is_refused() -> None:
    """Asking for a task past a step's end, in an unknown step, or before start raises, rather than name a task that does not exist."""
    # Before start nothing has an address; after it, only the job's tasks do.
    job, _, _, _ = make(CHAIN)
    with pytest.raises(TaskMapIncompleteError):
        job.address(TaskKey("a", 0))
    job.start()
    with pytest.raises(TaskMapIncompleteError):
        job.address(TaskKey("a", 3))
    with pytest.raises(TaskMapIncompleteError):
        job.address(TaskKey("a", -1))
    with pytest.raises(TaskMapIncompleteError):
        job.address(TaskKey("nope", 0))
    assert job.address(TaskKey("a", 2)).task_id == task_id_of("a", 2)


# -- the release gate --


def test_a_dependent_step_is_held_until_its_dependency_succeeds_on_the_farm() -> None:
    """A task is activated only once every step it depends on reads ``SUCCEEDED``, and releasing it twice activates it once.

    Activating a task lets it skip its step's dependency wait, so an early
    activation would run it before its inputs exist.
    """
    job, fake, clock, _ = started()
    for key in [TaskKey("b", 0), TaskKey("a", 0), TaskKey("a", 1), TaskKey("a", 0)]:
        job.release(key)
    job.poll()
    assert requested_ids(fake) == [task_id_of("a", 0), task_id_of("a", 1)]
    assert all(
        t["targetRunStatus"] == "READY" for c in fake.ops("BatchUpdateTask") for t in c["tasks"]
    )

    # Two of three succeeded: the step is not SUCCEEDED, so b stays held.
    fake.finish("a", 0)
    fake.finish("a", 1)
    clock.advance(10)
    job.poll()
    assert task_id_of("b", 0) not in requested_ids(fake)

    # The last one succeeds, and the next step read releases b.
    job.release(TaskKey("a", 2))
    clock.advance(10)
    job.poll()
    fake.finish("a", 2)
    clock.advance(10)
    job.poll()
    assert requested_ids(fake)[-1] == task_id_of("b", 0)


def test_a_step_with_an_update_in_progress_is_held() -> None:
    """While an update is being applied to a step, activating its tasks would be overwritten, so they wait for it to finish."""
    # Held while the update runs, released on the first read after it ends.
    job, fake, clock, _ = started([("a", 2, ())])
    fake.steps["a"]["lifecycle"] = "UPDATE_IN_PROGRESS"
    job.release(TaskKey("a", 0))
    job.poll()
    assert not fake.ops("BatchUpdateTask")
    fake.steps["a"]["lifecycle"] = "UPDATE_SUCCEEDED"
    clock.advance(5)
    job.poll()
    assert requested_ids(fake) == [task_id_of("a", 0)]


def test_step_statuses_are_read_at_most_every_five_seconds_and_a_failed_read_keeps_the_last() -> (
    None
):
    """Steps are read again only after five seconds, and a failed read keeps the last good one.

    PDG ticks many times a second, and treating a failed read as "nothing
    finished" would take back a release.
    """
    job, fake, clock, _ = started(page_size=10)
    for i in range(3):
        fake.finish("a", i)
    job.poll()
    job.poll()
    clock.advance(4.9)
    job.poll()
    assert len(fake.ops("ListSteps")) == 1

    # The failed read does not take back the release of b.
    fake.faults["ListSteps"] = _client_error("ThrottlingException", "ListSteps")
    clock.advance(0.2)
    job.release(TaskKey("b", 0))
    job.poll()
    assert len(fake.ops("ListSteps")) == 2
    assert task_id_of("b", 0) in requested_ids(fake)


def test_held_tasks_are_logged_when_the_count_changes(caplog: pytest.LogCaptureFixture) -> None:
    """The held count is logged once per change, not once per tick, so a long wait does not flood the log."""
    # One held task, then a second in another step.
    job, _, _, _ = started()
    job.release(TaskKey("b", 0))
    with caplog.at_level(logging.INFO, logger="deadline.houdini_pdg"):
        job.poll()
        job.poll()
        job.release(TaskKey("c", 0))
        job.poll()
    assert [r.getMessage() for r in caplog.records if "Holding" in r.getMessage()] == [
        "Holding 1 task(s) until the steps they depend on succeed: 1 in b",
        "Holding 2 task(s) until the steps they depend on succeed: 1 in b, 1 in c",
    ]


class ReleaseGateMachine(RuleBasedStateMachine):
    """Every activation is sound against the step listing the job last read, and a released step drains in one tick.

    Steps succeed, updates start and end, and step reads fail, in any order. A
    failed read must never take back a release.
    """

    STEPS: StepsShape = [("a", 4, ()), ("b", 3, ("a",)), ("c", 2, ("a",)), ("d", 2, ("b", "c"))]

    def __init__(self) -> None:
        super().__init__()
        # A started job over the four steps, with one page holding every step.
        self.job: DeadlineFarmJob
        self.fake: FakeDeadline
        self.clock: Clock
        self.snapshot: GraphSnapshot
        self.job, self.fake, self.clock, self.snapshot = started(self.STEPS, page_size=10)
        self.released: Set[TaskKey] = set()

    @rule(step=st.sampled_from(STEPS), index=st.integers(0, 3))
    def release(self, step: Any, index: int) -> None:
        """PDG offers a work item."""
        if index < step[1]:
            self.job.release(TaskKey(step[0], index))
            self.released.add(TaskKey(step[0], index))

    @rule(step=st.sampled_from(STEPS), index=st.integers(0, 3))
    def task_finishes(self, step: Any, index: int) -> None:
        """A worker finishes an activated task."""
        if index < step[1] and self.fake.tasks[task_id_of(step[0], index)]["runStatus"] == "READY":
            self.fake.finish(step[0], index)

    @rule(step=st.sampled_from(STEPS), updating=st.booleans())
    def step_update(self, step: Any, updating: bool) -> None:
        """An update starts or finishes on a step."""
        self.fake.steps[step[0]]["lifecycle"] = (
            "UPDATE_IN_PROGRESS" if updating else "UPDATE_SUCCEEDED"
        )

    @rule(failing=st.booleans())
    def step_reads_fail(self, failing: bool) -> None:
        """Step reads start or stop failing."""
        if failing:
            self.fake.faults["ListSteps"] = _client_error("ThrottlingException", "ListSteps")
        else:
            self.fake.faults.pop("ListSteps", None)

    @rule(seconds=st.integers(0, 10))
    def tick(self, seconds: int) -> None:
        """PDG ticks, and every released task its step allows is activated in that tick."""
        # After the tick, a released task not yet asked for must be one its step
        # does not allow yet, by the listing the job last read.
        self.clock.advance(seconds)
        self.job.poll()
        requested: Set[str] = set(requested_ids(self.fake))
        for key in self.released:
            if task_id_of(key.step_name, key.task_index) not in requested:
                assert not self._allowed(key.step_name, self.fake.last_step_read), key

    @invariant()
    def every_activation_is_sound(self) -> None:
        """No task was asked to run before the listing the job had read allowed it."""
        names: Dict[str, str] = {step_id_of(s.name): s.name for s in self.snapshot.steps}
        for task_id, read in self.fake.ready_requests:
            step_id: str = "step-" + task_id.split("-")[1]
            assert self._allowed(names[step_id], read), task_id

    def _allowed(self, name: str, read: Dict[str, Tuple[str, str]]) -> bool:
        """The rule: every dependency read ``SUCCEEDED`` and the step has no update in progress."""
        step: Any = self.snapshot.step(name)
        if read.get(name, ("", ""))[1] == "UPDATE_IN_PROGRESS":
            return False
        return all(read.get(dep, ("", ""))[0] == "SUCCEEDED" for dep in step.dependencies)


ReleaseGateMachine.TestCase.settings = settings(
    max_examples=30, stateful_step_count=30, deadline=None
)
test_release_gate: Any = ReleaseGateMachine.TestCase


# -- activation --


def test_activation_batches_hold_at_most_a_hundred_tasks_and_keep_release_order() -> None:
    """Released tasks go out in calls of at most a hundred, the service's limit, oldest release first."""
    # Released newest first, so the order checked is release order, not index order.
    job, fake, _, _ = started([("a", 250, ())])
    for i in reversed(range(250)):
        job.release(TaskKey("a", i))
    job.poll()
    assert [len(c["tasks"]) for c in fake.ops("BatchUpdateTask")] == [100, 100, 50]
    assert requested_ids(fake) == [task_id_of("a", i) for i in reversed(range(250))]


def test_an_oversized_batch_raises_before_any_call() -> None:
    """A batch over the service's limit raises instead of being cut, which would lose tasks.

    The queue never hands out more than a hundred keys, so no public call can
    reach this guard; the test calls the private batch call directly.
    """
    job, fake, _, _ = started([("a", 1, ())])
    with pytest.raises(ValueError, match="at most 100"):
        job._activate_batch([TaskKey("a", 0)] * 101)
    assert not fake.ops("BatchUpdateTask")


def test_a_failed_activation_call_is_retried_on_the_next_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused call activates nothing, so its tasks are asked for again once the service accepts calls, and only then logged as activated."""
    # The first call is refused; the next poll's is accepted.
    job, fake, _, _ = started([("a", 3, ())])
    fake.faults["BatchUpdateTask"] = _client_error("ThrottlingException", "BatchUpdateTask")
    for i in range(3):
        job.release(TaskKey("a", i))
    with caplog.at_level(logging.INFO, logger="deadline.houdini_pdg"):
        # Nothing is logged as activated while the service refuses.
        job.poll()
        assert len(fake.ops("BatchUpdateTask")) == 1
        assert "Activated" not in caplog.text
        del fake.faults["BatchUpdateTask"]
        job.poll()
        job.poll()
    assert fake.accepted == [task_id_of("a", i) for i in range(3)]
    assert len(fake.ops("BatchUpdateTask")) == 2
    assert [r.getMessage() for r in caplog.records if "Activated" in r.getMessage()] == [
        "Activated 3 task(s) to run."
    ]


def test_a_rejected_task_is_retried_alone_behind_the_tasks_queued_before_it() -> None:
    """A task the service rejects is asked for again on the next poll, after the tasks still queued; its batch mates are not.

    Activation stops for the poll, so a task the service keeps rejecting neither
    loops nor holds up the tasks released after it.
    """
    # Tasks 0 to 99 go in the first call, and the service rejects task 0.
    job, fake, _, _ = started([("a", 150, ())])
    for i in range(150):
        job.release(TaskKey("a", i))
    fake.rejected = {task_id_of("a", 0)}
    job.poll()
    assert [len(c["tasks"]) for c in fake.ops("BatchUpdateTask")] == [100]

    fake.rejected = set()
    job.poll()
    second: List[str] = [t["taskId"] for t in fake.ops("BatchUpdateTask")[1]["tasks"]]
    assert second == [task_id_of("a", i) for i in range(100, 150)] + [task_id_of("a", 0)]
    assert sorted(fake.accepted) == sorted(task_id_of("a", i) for i in range(150))


def test_a_malformed_activation_response_retries_the_whole_batch() -> None:
    """When the answer cannot be read, every task in the call is asked for again: asking twice does nothing, and losing one stalls the cook."""
    # The service accepts the tasks but answers with an error entry that cannot be read.
    job, fake, _, _ = started([("a", 2, ())])
    fake.malformed["BatchUpdateTask"] = {"errors": [None]}
    job.release(TaskKey("a", 0))
    job.release(TaskKey("a", 1))
    job.poll()
    del fake.malformed["BatchUpdateTask"]
    job.poll()
    assert requested_ids(fake) == [task_id_of("a", 0), task_id_of("a", 1)] * 2


def test_a_task_released_again_is_activated_and_reported_once() -> None:
    """Releasing a task again while it is queued, after it is activated, or after its result is reported changes nothing.

    PDG can offer a work item more than once. Activating a task that already ran
    would run it again, and a second result would report the item twice.
    """
    job, fake, clock, _ = started([("a", 1, ())])
    key: TaskKey = TaskKey("a", 0)

    # Released twice while queued, then again once activated.
    job.release(key)
    job.release(key)
    job.poll()
    job.release(key)
    clock.advance(5)
    job.poll()

    # Released again after its result was reported.
    fake.finish("a", 0)
    clock.advance(5)
    reported: List[TaskOutcome] = job.poll()
    job.release(key)
    clock.advance(5)
    reported.extend(job.poll())
    assert fake.accepted == [task_id_of("a", 0)]
    assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED)]


def test_a_released_key_without_a_task_is_dropped_and_never_counted_as_activated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A released key the job has no task for is logged once and dropped; the tasks released with it still run.

    Keeping it would make every later poll and reconcile ask about a task that
    does not exist, or ask the service to read no task at all.
    """
    # A key past the step's end alone, then with a real task, then a reconcile
    # a minute later.
    job, fake, clock, _ = started([("a", 1, ())])
    job.release(TaskKey("a", 5))
    with caplog.at_level(logging.WARNING, logger="deadline.houdini_pdg"):
        # A poll with only the bad key must not fail either.
        job.poll()
        job.release(TaskKey("a", 6))
        job.release(TaskKey("a", 0))
        job.poll()
        clock.advance(61)
        job.poll()
    assert requested_ids(fake) == [task_id_of("a", 0)]
    # The read at release, the read before activation, and the reconcile.
    assert [[i["taskId"] for i in c["identifiers"]] for c in fake.ops("BatchGetTask")] == [
        [task_id_of("a", 0)]
    ] * 3
    assert caplog.text.count("Task a[5] is not in job") == 1
    assert caplog.text.count("Task a[6] is not in job") == 1
    assert "Polling job" not in caplog.text and "Could not read" not in caplog.text


class ReleaseQueueMachine(RuleBasedStateMachine):
    """The queue under any mix of releases, refused calls, rejected tasks, and farm results.

    No task is activated twice, nothing unreleased is activated, tasks that were
    never refused go out in release order, and nothing is lost once the faults
    stop.
    """

    STEPS: StepsShape = [("a", 6, ()), ("b", 6, ())]
    keys: Any = st.builds(TaskKey, st.sampled_from(["a", "b"]), st.integers(0, 5))

    def __init__(self) -> None:
        super().__init__()
        # A started job over two root steps, so the release gate never holds a task.
        self.job: DeadlineFarmJob
        self.fake: FakeDeadline
        self.clock: Clock
        self.job, self.fake, self.clock, _ = started(self.STEPS)
        self.release_order: List[str] = []
        self.ended_on_farm: Set[str] = set()

    @rule(key=keys)
    def release(self, key: TaskKey) -> None:
        """PDG offers a work item, possibly again."""
        self.job.release(key)
        task_id: str = task_id_of(key.step_name, key.task_index)
        if task_id not in self.release_order:
            self.release_order.append(task_id)

    @rule(failing=st.booleans())
    def calls_fail(self, failing: bool) -> None:
        """Activation calls start or stop failing."""
        if failing:
            self.fake.faults["BatchUpdateTask"] = _client_error(
                "ThrottlingException", "BatchUpdateTask"
            )
        else:
            self.fake.faults.pop("BatchUpdateTask", None)

    @rule(indices=st.sets(st.integers(0, 5)))
    def reject(self, indices: Set[int]) -> None:
        """The service starts rejecting these tasks of step a."""
        self.fake.rejected = {task_id_of("a", i) for i in indices}

    @rule(key=keys)
    def ends_on_farm(self, key: TaskKey) -> None:
        """A task ends on the farm, activated or not."""
        task_id: str = task_id_of(key.step_name, key.task_index)
        if task_id not in self.ended_on_farm:
            self.ended_on_farm.add(task_id)
            self.fake.finish(key.step_name, key.task_index)

    @rule(seconds=st.integers(0, 70))
    def tick(self, seconds: int) -> None:
        """PDG ticks."""
        self.clock.advance(seconds)
        self.job.poll()

    @invariant()
    def calls_are_well_formed(self) -> None:
        """Every call carries 1 to 100 released tasks, and no task is accepted twice."""
        assert all(1 <= len(c["tasks"]) <= 100 for c in self.fake.ops("BatchUpdateTask"))
        assert set(requested_ids(self.fake)) <= set(self.release_order)
        assert len(self.fake.accepted) == len(set(self.fake.accepted))

    @invariant()
    def never_refused_tasks_keep_release_order(self) -> None:
        """A task never refused is accepted only after every earlier released task that is still waiting."""
        accepted: List[str] = self.fake.accepted
        for later in accepted:
            if later in self.fake.refused:
                continue
            for earlier in self.release_order[: self.release_order.index(later)]:
                if earlier in self.fake.refused or earlier in self.ended_on_farm:
                    continue
                assert earlier in accepted and accepted.index(earlier) < accepted.index(later)

    def teardown(self) -> None:
        """Once the faults stop, one poll activates every released task that has not ended."""
        # Clear every fault, then give the job one poll to catch up.
        self.fake.faults.clear()
        self.fake.rejected = set()
        self.job.poll()
        for task_id in self.release_order:
            assert task_id in self.fake.accepted or task_id in self.ended_on_farm, task_id


ReleaseQueueMachine.TestCase.settings = settings(
    max_examples=40, stateful_step_count=30, deadline=None
)
test_release_queue: Any = ReleaseQueueMachine.TestCase


def test_a_task_the_farm_ran_unactivated_is_logged_once_and_never_reported_while_unreleased(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A result for a task never activated is logged once, however many polls see it, and is never reported while PDG has not released the task.

    A task the farm ran on its own may have run before its payload existed, so
    the artist needs to know. A task PDG never released has no work item waiting
    for it, so reporting its result on every poll would repeat it for as long as
    the search window covers it.
    """
    # a[0] is activated, a[1] is released and read but its activation is
    # refused, and a[2] is never released. Then all three end on the farm.
    job, fake, clock, _ = started([("a", 3, ())])
    job.release(TaskKey("a", 0))
    job.poll()
    fake.faults["BatchUpdateTask"] = _client_error("ThrottlingException", "BatchUpdateTask")
    job.release(TaskKey("a", 1))
    job.poll()
    for index in range(3):
        fake.finish("a", index)

    # A minute of polls, all inside the search window.
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        for _ in range(12):
            clock.advance(5)
            reported.extend(job.poll())
    assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED), ("a", 1, TaskStatus.SUCCEEDED)]
    assert caplog.text.count("before this job activated it") == 2
    # a[1]'s run, after its release check, goes to the cook; a[2]'s does not.
    assert caplog.text.count("Its result is reported.") == 1
    assert caplog.text.count("Its result is not used") == 1
    assert "Polling job" not in caplog.text

    # A queued task that already ended is not activated afterwards.
    del fake.faults["BatchUpdateTask"]
    attempts: int = requested_ids(fake).count(task_id_of("a", 1))
    job.poll()
    assert requested_ids(fake).count(task_id_of("a", 1)) == attempts


@pytest.mark.parametrize("release_delay", [0, 35])
def test_a_task_the_farm_ran_before_its_release_runs_again_and_only_its_new_result_is_reported(
    release_delay: int, caplog: pytest.LogCaptureFixture
) -> None:
    """When PDG releases a task the farm already finished, the task runs again, and only the new run's result is reported, once.

    The first run happened before the work item's payload was stored, so its
    result says nothing about the item: a failure from it would fail an item
    whose real run succeeds. The farm's index keeps showing the first result
    after the task is set to run again, so it may not be reported.
    """
    # The farm runs a[0] before PDG releases it, and the run fails for good.
    job, fake, clock, _ = started([("a", 1, ())])
    key: TaskKey = TaskKey("a", 0)
    fake.finish("a", 0, "FAILED", retries_used=1)
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        reported.extend(job.poll())

        # PDG releases it later: it is activated, so the farm runs it again,
        # and polls for over a minute, through a reconcile, report nothing
        # while the index still shows the first run's failure.
        clock.advance(release_delay)
        job.release(key)
        for _ in range(14):
            reported.extend(job.poll())
            clock.advance(5)
        assert fake.accepted == [task_id_of("a", 0)]
        assert reported == []

        # The second run succeeds, and its result is reported once.
        fake.finish("a", 0)
        for _ in range(14):
            clock.advance(5)
            reported.extend(job.poll())
    assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED)]
    assert fake.accepted == [task_id_of("a", 0)]
    assert caplog.text.count("before this job activated it") == 1
    # The early run's result is never used, and the log says so.
    assert "Its result is not used." in caplog.text
    assert "Polling job" not in caplog.text


def test_a_task_that_ran_early_and_is_still_queued_is_activated_and_not_reported() -> None:
    """A task the farm ran before its release, and that waits in the queue for its dependency, is never reported from that run and runs once its dependency succeeds.

    Treating it like a queued task that ended would report the run that had no
    payload and never run the task again.
    """
    # b[0] runs on the farm before its release, and then waits behind a[0].
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))])
    fake.finish("b", 0)
    reported: List[TaskOutcome] = job.poll()
    job.release(TaskKey("b", 0))
    clock.advance(5)
    reported.extend(job.poll())
    assert requested_ids(fake) == []

    # a[0] runs and succeeds, which releases b[0] to run again.
    job.release(TaskKey("a", 0))
    clock.advance(5)
    job.poll()
    fake.finish("a", 0)
    for _ in range(3):
        clock.advance(5)
        reported.extend(job.poll())
    assert fake.accepted == [task_id_of("a", 0), task_id_of("b", 0)]
    assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED)]

    # Its second run is reported.
    clock.advance(5)
    fake.finish("b", 0)
    clock.advance(5)
    reported.extend(job.poll())
    assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED), ("b", 0, TaskStatus.SUCCEEDED)]


@pytest.mark.parametrize(
    "lag, release_delay",
    [(30, 5), (0, 0)],
    ids=["index-shows-the-early-run-after-the-release", "early-run-and-release-between-polls"],
)
def test_a_task_that_ran_early_runs_again_even_when_the_search_has_not_shown_that_run(
    lag: int, release_delay: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A task the farm finished before its release runs again, and only the new run is reported, even when no search showed the early run before the release.

    The index can show a result minutes late, and a poll activates before it
    searches, so only reading the task itself tells the job it already ran.
    Taking the early run's result for the new run's would fail an item from a
    run that had no payload.
    """
    # The farm fails a[0] for good before PDG releases it, with no poll between.
    job, fake, clock, _ = started([("a", 1, ())])
    fake.finish("a", 0, "FAILED", lag=lag, retries_used=1)
    first_end: datetime = clock.now()
    clock.advance(release_delay)
    job.release(TaskKey("a", 0))

    # Polls through the index lag and a reconcile report nothing, and the task
    # was activated once.
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        for _ in range(14):
            reported.extend(job.poll())
            clock.advance(5)
        assert fake.accepted == [task_id_of("a", 0)] and reported == []

        # The second run succeeds, and only it is reported.
        fake.finish("a", 0)
        second_end: datetime = clock.now()
        for _ in range(14):
            clock.advance(5)
            reported.extend(job.poll())
    assert first_end < second_end
    assert [(o.status, o.ended_at) for o in reported] == [(TaskStatus.SUCCEEDED, second_end)]
    assert fake.accepted == [task_id_of("a", 0)]
    assert caplog.text.count("before this job activated it") == 1


def test_a_held_task_that_ran_before_its_release_is_never_reported_from_that_run() -> None:
    """A dependent task the farm ran before PDG released it is not reported from that run while the gate holds it, and runs again once its dependency succeeds.

    The release came after the run, so the run had no payload, even though the
    index only shows it while the task waits in the queue.
    """
    # b[0] ends on the farm, PDG releases it, and the index shows the run 30 s later.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))])
    fake.finish("b", 0, lag=30)
    job.release(TaskKey("b", 0))
    reported: List[TaskOutcome] = []
    for _ in range(14):
        reported.extend(job.poll())
        clock.advance(5)
    assert reported == [] and requested_ids(fake) == []

    # a[0] runs and succeeds, which releases b[0] to run again.
    job.release(TaskKey("a", 0))
    job.poll()
    fake.finish("a", 0)
    for _ in range(3):
        clock.advance(5)
        reported.extend(job.poll())
    assert fake.accepted == [task_id_of("a", 0), task_id_of("b", 0)]
    assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED)]

    # Its second run is reported.
    clock.advance(5)
    fake.finish("b", 0)
    clock.advance(5)
    reported.extend(job.poll())
    assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED), ("b", 0, TaskStatus.SUCCEEDED)]


@pytest.mark.parametrize(
    "skew, release_delay, run_length",
    [(-2, 1, 1), (2, 1, 30)],
    ids=["job-clock-ahead-short-run", "service-clock-ahead-release-just-after-the-early-run"],
)
def test_which_run_a_result_comes_from_does_not_depend_on_the_job_clock(
    skew: int, release_delay: int, run_length: int
) -> None:
    """With the service's clock 2 s off from the job's, the early run is never reported and the second run is, once.

    ``endedAt`` is stamped by the service. Comparing it with the job's own
    clock would drop a second run shorter than the difference, so the cook
    would wait forever, or would report the early run when the release comes
    right after it.
    """
    # The farm fails a[0] for good, and a search shows it before the release.
    job, fake, clock, _ = started([("a", 1, ())])
    fake.skew = timedelta(seconds=skew)
    fake.finish("a", 0, "FAILED", retries_used=1)
    reported: List[TaskOutcome] = job.poll()
    clock.advance(release_delay)
    job.release(TaskKey("a", 0))
    reported.extend(job.poll())
    assert fake.accepted == [task_id_of("a", 0)]

    # The second run ends ``run_length`` seconds after its activation.
    clock.advance(run_length)
    fake.finish("a", 0)
    for _ in range(14):
        clock.advance(5)
        reported.extend(job.poll())
    second_end: datetime = _T0 + timedelta(seconds=release_delay + run_length + skew)
    assert [(o.status, o.ended_at) for o in reported] == [(TaskStatus.SUCCEEDED, second_end)]


@settings(max_examples=40, deadline=None)
@given(
    early=st.sampled_from([("SUCCEEDED", None), ("FAILED", 1), ("NOT_COMPATIBLE", None)]),
    second=st.sampled_from(["SUCCEEDED", "FAILED", "NOT_COMPATIBLE"]),
    gap=st.integers(0, 300),
    lag=st.integers(0, 300),
    run_length=st.integers(1, 300),
    interval=st.integers(1, 60),
    skew=st.integers(-600, 600),
)
def test_for_any_timing_and_clock_difference_a_task_that_ran_early_reports_only_its_run_after_release(
    early: Tuple[str, Optional[int]],
    second: str,
    gap: int,
    lag: int,
    run_length: int,
    interval: int,
    skew: int,
) -> None:
    """For any early status, release delay, index lag, run length, tick interval, and clock difference, a task the farm ran before its release runs again once, and only that run's result is reported, once.

    The search may show the early run before or after the release, and the
    service's clock may be ahead of or behind the job's, so neither what the
    search has shown nor the job's clock can tell the two runs apart.
    """
    # The farm ends a[0] before PDG releases it, ``gap`` seconds later.
    job, fake, clock, _ = started([("a", 1, ())])
    fake.skew = timedelta(seconds=skew)
    fake.finish("a", 0, early[0], lag=lag, retries_used=early[1])
    release_at: datetime = clock.now() + timedelta(seconds=gap)

    # PDG ticks every ``interval`` seconds. The farm starts the second run when
    # the job activates the task, and ends it ``run_length`` seconds later.
    reported: List[TaskOutcome] = []
    rerun_end: List[datetime] = []
    task: Dict[str, Any] = fake.tasks[task_id_of("a", 0)]
    stop: datetime = release_at + timedelta(seconds=run_length + 3 * interval + 130)
    while clock.now() < stop:
        # PDG releases the task once its time has come, and ticks.
        if clock.now() >= release_at:
            job.release(TaskKey("a", 0))
        reported.extend(job.poll())

        # The second run starts at the activation, and ends once its time has come.
        if fake.accepted and not rerun_end:
            rerun_end.append(clock.now() + timedelta(seconds=run_length))
        if rerun_end and task["runStatus"] == "READY" and clock.now() >= rerun_end[0]:
            fake.finish("a", 0, second, ended=rerun_end[0], lag=lag, retries_used=1)
        clock.advance(interval)

    # The second run's status and service end time, once, from one activation.
    assert rerun_end and task["runStatus"] == second
    want: TaskStatus = TaskStatus.SUCCEEDED if second == "SUCCEEDED" else TaskStatus.FAILED
    assert [(o.status, o.ended_at) for o in reported] == [(want, rerun_end[0] + fake.skew)]
    assert fake.accepted == [task_id_of("a", 0)]


def test_a_released_task_is_activated_only_once_it_has_been_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A released task whose read fails, or comes back as an error, is not activated until a later read succeeds; the others read with it still are.

    Activating a task the job has not read could take an early run's result for
    the new run's.
    """
    # The whole read fails on the first poll, and the second answer leaves
    # both tasks out.
    job, fake, clock, _ = started([("a", 2, ())])
    fake.faults["BatchGetTask"] = _client_error("ThrottlingException", "BatchGetTask")
    job.release(TaskKey("a", 0))
    job.release(TaskKey("a", 1))
    with caplog.at_level(logging.WARNING, logger="deadline.houdini_pdg"):
        job.poll()
        del fake.faults["BatchGetTask"]
        fake.malformed["BatchGetTask"] = {"tasks": []}
        job.poll()
    assert not fake.ops("BatchUpdateTask")
    assert f"Could not read released task {task_id_of('a', 1)}: not in the answer" in caplog.text

    # The next read answers a[0] with an error, so only a[1] is activated.
    del fake.malformed["BatchGetTask"]
    fake.lost = {task_id_of("a", 0)}
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="deadline.houdini_pdg"):
        job.poll()
    assert fake.accepted == [task_id_of("a", 1)]
    assert caplog.messages == [
        f"Could not read released task {task_id_of('a', 0)}: ResourceNotFoundException"
    ]

    # Once a[0] can be read, it is activated too.
    fake.lost = set()
    job.poll()
    assert fake.accepted == [task_id_of("a", 1), task_id_of("a", 0)]


def test_a_result_for_a_released_task_not_yet_read_is_not_reported_and_the_task_runs_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A result the search shows for a released task whose read keeps failing is held back, and the task runs again once it is read.

    Until the job has read the task after its release, it cannot tell whether
    that run came before the payload was stored.
    """
    # a[0] ends on the farm right after its release, while reads fail.
    job, fake, clock, _ = started([("a", 1, ())])
    fake.faults["BatchGetTask"] = _client_error("ThrottlingException", "BatchGetTask")
    job.release(TaskKey("a", 0))
    fake.finish("a", 0)
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            clock.advance(5)
            reported.extend(job.poll())
        assert reported == [] and not fake.ops("BatchUpdateTask")

        # Reads work again: the task is activated, and its new run is reported.
        del fake.faults["BatchGetTask"]
        clock.advance(5)
        reported.extend(job.poll())
        assert fake.accepted == [task_id_of("a", 0)] and reported == []
        clock.advance(5)
        fake.finish("a", 0)
        clock.advance(5)
        reported.extend(job.poll())
    assert [(o.status, o.ended_at) for o in reported] == [
        (TaskStatus.SUCCEEDED, _T0 + timedelta(seconds=25))
    ]
    assert caplog.text.count("before this job activated it") == 1


def _release_b_after_a(job: DeadlineFarmJob, fake: FakeDeadline, clock: Clock) -> List[TaskOutcome]:
    """Release a[0] and poll, then let a[0] succeed on the farm, so the next poll releases step b.

    Returns that first poll's outcomes.
    """
    # The clock moves past the step-read interval, so the next poll sees a succeed.
    job.release(TaskKey("a", 0))
    outcomes: List[TaskOutcome] = job.poll()
    fake.finish("a", 0)
    clock.advance(5)
    return outcomes


@pytest.mark.parametrize("lag", [0, 30], ids=["index-shows-it-while-held", "index-shows-it-late"])
def test_a_held_task_the_farm_ran_after_its_release_read_is_reported_once_and_never_run_again(
    lag: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A dependent task read at its release, that the farm then runs while the gate holds it, is reported from that run, logged once, and never activated, however late the index shows the run.

    That run came after the release, so it had its payload. Activating the
    task would make the farm run it a second time after PDG has its result,
    and would hide the gate violation when the index is late.
    """
    # b[0] is released and read while suspended, then ends on the farm on its own.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))])
    job.release(TaskKey("b", 0))
    job.poll()
    clock.advance(1)
    fake.finish("b", 0, lag=lag)
    run_end: datetime = clock.now()

    # The gate opens 5 s later, before a late index shows the run.
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        reported.extend(_release_b_after_a(job, fake, clock))
        for _ in range(12):
            reported.extend(job.poll())
            clock.advance(5)
    assert fake.accepted == [task_id_of("a", 0)]
    assert [(o.key, o.ended_at) for o in reported if o.key.step_name == "b"] == [
        (TaskKey("b", 0), run_end)
    ]
    assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED), ("b", 0, TaskStatus.SUCCEEDED)]
    assert caplog.text.count("before this job activated it") == 1
    # The run came after the release check, so its result goes to the cook.
    assert "Its result is reported." in caplog.text


@pytest.mark.parametrize("in_progress", IN_PROGRESS_STATUSES)
def test_a_held_task_the_farm_is_running_when_its_gate_opens_is_activated_only_after_that_run_and_never_reported_from_it(
    in_progress: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A dependent task the farm starts on its own after its release read, and that is still running or about to run when its gate opens, is not activated while that run lasts, is never reported from that run, and runs again once it ends.

    Setting such a task ready would ask the farm to run it again while it
    runs, and reporting that run would give PDG the result of a run this job
    never started. Only the run after the activation is reported, and the gate
    violation is logged once. Each status the service uses for a run in
    progress is checked, because treating any one of them as idle would
    activate the task during that run.
    """
    # b[0] is read while suspended, and the farm starts it while the gate holds it.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))])
    job.release(TaskKey("b", 0))
    job.poll()
    fake.tasks[task_id_of("b", 0)]["runStatus"] = in_progress

    # The gate opens while b[0] runs: polls read it and leave it alone.
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        reported.extend(_release_b_after_a(job, fake, clock))
        for _ in range(3):
            reported.extend(job.poll())
            clock.advance(5)
        assert requested_ids(fake) == [task_id_of("a", 0)]

        # The run fails for good. That is not reported, and b[0] runs again.
        fake.finish("b", 0, "FAILED", retries_used=1)
        for _ in range(12):
            clock.advance(5)
            reported.extend(job.poll())
        assert fake.accepted == [task_id_of("a", 0), task_id_of("b", 0)]
        assert statuses_of(reported) == [("a", 0, TaskStatus.SUCCEEDED)]

        # The run after the activation succeeds, and only it is reported.
        fake.finish("b", 0)
        second_end: datetime = clock.now()
        for _ in range(12):
            clock.advance(5)
            reported.extend(job.poll())
    assert [(o.key, o.status, o.ended_at) for o in reported if o.key.step_name == "b"] == [
        (TaskKey("b", 0), TaskStatus.SUCCEEDED, second_end)
    ]
    assert fake.accepted == [task_id_of("a", 0), task_id_of("b", 0)]
    assert caplog.text.count("before this job activated it") == 1


@pytest.mark.parametrize("in_progress", IN_PROGRESS_STATUSES)
@pytest.mark.parametrize("lag", [0, 30], ids=["index-on-time", "index-late"])
@pytest.mark.parametrize("held", [False, True], ids=["root-task", "held-task"])
def test_a_task_the_farm_is_running_at_its_release_read_runs_again_and_only_the_new_run_is_reported(
    held: bool, lag: int, in_progress: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A task the farm started on its own before PDG released it, and that is still running or about to run when the job reads it, is not activated while that run lasts, is never reported from that run, and runs again once that run ends.

    That run may have started before the work item's payload was stored, so a
    failure from it would fail an item whose real run succeeds. Only the run
    after the activation is reported, once, and the gate violation is logged
    once, whether the task is a root task or one the gate holds. Each status
    the service uses for a run in progress is checked, because treating any one
    of them as idle would take that run for one with the payload.
    """
    # The farm starts the task before PDG releases it, and a poll reads it in progress.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))])
    key: TaskKey = TaskKey("b", 0) if held else TaskKey("a", 0)
    task_id: str = task_id_of(key.step_name, key.task_index)
    fake.tasks[task_id]["runStatus"] = in_progress
    clock.advance(1)
    job.release(key)
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        if held:
            reported.extend(_release_b_after_a(job, fake, clock))
        for _ in range(3):
            reported.extend(job.poll())
            clock.advance(5)
        assert task_id not in requested_ids(fake)

        # That run fails for good. It is never reported, and the task runs again once.
        fake.finish(key.step_name, key.task_index, "FAILED", retries_used=1, lag=lag)
        for _ in range(14):
            clock.advance(5)
            reported.extend(job.poll())
        assert requested_ids(fake).count(task_id) == 1
        assert [o for o in reported if o.key == key] == []

        # The run after the activation succeeds, and only it is reported.
        fake.finish(key.step_name, key.task_index, lag=lag)
        second_end: datetime = clock.now()
        for _ in range(14):
            clock.advance(5)
            reported.extend(job.poll())
    assert [(o.status, o.ended_at) for o in reported if o.key == key] == [
        (TaskStatus.SUCCEEDED, second_end)
    ]
    assert fake.accepted.count(task_id) == 1
    assert caplog.text.count("before this job activated it") == 1
    # That run's failure is never used, and the log says so.
    assert "Its result is not used." in caplog.text


@pytest.mark.parametrize("lag", [0, 30], ids=["index-on-time", "index-late"])
def test_a_held_task_whose_early_run_ends_while_it_is_still_held_runs_again_and_only_the_new_run_is_reported(
    lag: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A dependent task the farm is running at its release check, whose run then ends while the gate still holds it, is never reported from that run, and runs again once its gate opens.

    That run may have started before the work item's payload was stored. If
    the release check counted a task in progress as checked, the run's end
    would look like a run that came after the release, and its result would be
    reported while the task never ran with its payload.
    """
    # The farm starts b[0] before PDG releases it, and the release check finds
    # it running while a has not succeeded.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))])
    key: TaskKey = TaskKey("b", 0)
    task_id: str = task_id_of("b", 0)
    fake.tasks[task_id]["runStatus"] = "RUNNING"
    clock.advance(1)
    job.release(key)
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        reported.extend(job.poll())

        # That run succeeds while b[0] is still held, and polls run past the
        # index lag and a reconcile. Nothing is reported or activated for b[0].
        clock.advance(5)
        fake.finish("b", 0, lag=lag)
        for _ in range(14):
            clock.advance(5)
            reported.extend(job.poll())
        assert [o for o in reported if o.key == key] == []
        assert task_id not in requested_ids(fake)

        # The gate opens: b[0] is activated once, and still nothing is reported for it.
        reported.extend(_release_b_after_a(job, fake, clock))
        for _ in range(14):
            reported.extend(job.poll())
            clock.advance(5)
        assert fake.accepted.count(task_id) == 1
        assert [o for o in reported if o.key == key] == []

        # The run after the activation succeeds, and only it is reported.
        fake.finish("b", 0, lag=lag)
        second_end: datetime = clock.now()
        for _ in range(14):
            clock.advance(5)
            reported.extend(job.poll())
    assert [(o.status, o.ended_at) for o in reported if o.key == key] == [
        (TaskStatus.SUCCEEDED, second_end)
    ]
    assert fake.accepted.count(task_id) == 1
    assert caplog.text.count("before this job activated it") == 1
    assert "Its result is not used." in caplog.text


@pytest.mark.parametrize("lag", [0, 30], ids=["index-on-time", "index-late"])
@pytest.mark.parametrize(
    "held, canceled_before_release",
    [(False, True), (True, True), (True, False)],
    ids=["root-task", "held-task-canceled-before-release", "held-task-canceled-while-held"],
)
def test_a_released_task_canceled_on_the_farm_before_its_activation_runs_once_and_only_its_new_result_is_reported(
    held: bool, canceled_before_release: bool, lag: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A released task someone cancels on the farm before the job activates it is activated once, is not logged as a run the farm made on its own, returns its cancel at most once, and reports the result of the run after its activation once.

    A cancel of a task that never ran says nothing about the work item, and PDG
    still needs the item's result. Skipping the task would hang the cook until
    someone requeues it, and logging the cancel as a gate violation would blame
    the farm for a run that never happened.
    """
    # The task is canceled on the farm, before its release or while the gate
    # holds it after its release read.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))])
    key: TaskKey = TaskKey("b", 0) if held else TaskKey("a", 0)
    task_id: str = task_id_of(key.step_name, key.task_index)
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        if canceled_before_release:
            fake.finish(key.step_name, key.task_index, "CANCELED", lag=lag)
            clock.advance(1)
            job.release(key)
        else:
            job.release(key)
            reported.extend(job.poll())
            clock.advance(1)
            fake.finish(key.step_name, key.task_index, "CANCELED", lag=lag)

        # The gate opens, for a held task, and polls run past the index lag and
        # a reconcile. The task is activated once.
        if held:
            reported.extend(_release_b_after_a(job, fake, clock))
        for _ in range(14):
            reported.extend(job.poll())
            clock.advance(5)
        assert requested_ids(fake).count(task_id) == 1
        assert fake.accepted.count(task_id) == 1

        # The run after the activation succeeds, and is reported once.
        fake.finish(key.step_name, key.task_index, lag=lag)
        second_end: datetime = clock.now()
        for _ in range(14):
            clock.advance(5)
            reported.extend(job.poll())
    mine: List[TaskOutcome] = [o for o in reported if o.key == key]
    assert [o.status for o in mine].count(TaskStatus.CANCELED) <= 1
    assert [(o.status, o.ended_at) for o in mine if o.status != TaskStatus.CANCELED] == [
        (TaskStatus.SUCCEEDED, second_end)
    ]
    assert fake.accepted.count(task_id) == 1
    assert "before this job activated it" not in caplog.text


def test_a_task_that_ran_early_and_then_ran_again_while_held_reports_that_run_and_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dependent task that ran before its release, and that the farm runs again while the gate holds it, reports only the second run, is never activated, and its gate violation is logged once.

    The second run came after the release, so it had its payload; the first
    did not. One log line per task keeps a farm that ignores the gate from
    flooding the log.
    """
    # b[0] fails before its release, is read, and runs again and succeeds while held.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))])
    fake.finish("b", 0, "FAILED", retries_used=1, lag=30)
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        clock.advance(1)
        job.release(TaskKey("b", 0))
        reported.extend(job.poll())
        clock.advance(1)
        fake.finish("b", 0, lag=30)
        second_end: datetime = clock.now()

        # The gate opens before the index shows either run.
        reported.extend(_release_b_after_a(job, fake, clock))
        for _ in range(12):
            reported.extend(job.poll())
            clock.advance(5)
    assert fake.accepted == [task_id_of("a", 0)]
    assert [(o.key, o.status, o.ended_at) for o in reported if o.key.step_name == "b"] == [
        (TaskKey("b", 0), TaskStatus.SUCCEEDED, second_end)
    ]
    # The one record names the early run and promises no later run, because
    # the farm's second run, not a run after activation, is what the cook got.
    records: List[str] = [
        r.getMessage() for r in caplog.records if "before this job activated it" in r.getMessage()
    ]
    assert records == [
        "Task b[0] reached FAILED, but the farm ran it before this job activated it."
        " Its result is not used."
    ]


@pytest.mark.parametrize("lag", [0, 30], ids=["index-on-time", "index-late"])
@pytest.mark.parametrize(
    "failed_before_release", [False, True], ids=["failed-while-held", "failed-before-release"]
)
def test_a_held_task_whose_unactivated_run_fails_with_retries_left_is_logged_once_and_reports_its_next_run(
    failed_before_release: bool, lag: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A dependent task the farm runs on its own, whose run fails with retries left before its gate opens, is logged once as a run the farm made on its own, is activated once, and reports only the run after its activation.

    The log shows that the farm ran a task without the scheduler, whether or
    not the farm would retry the failure, so it may not depend on which read,
    or the search, sees the run first. A second log line for the same run
    would repeat it, and the failure itself is never a result.
    """
    # b[0] fails with one of its two retries left, before its release or after
    # its release read while the gate holds it.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))], max_retries=2)
    key: TaskKey = TaskKey("b", 0)
    task_id: str = task_id_of("b", 0)
    reported: List[TaskOutcome] = []
    with caplog.at_level(logging.ERROR):
        if failed_before_release:
            fake.finish("b", 0, "FAILED", retries_used=0, lag=lag)
            reported.extend(job.poll())
            clock.advance(1)
            job.release(key)
        else:
            job.release(key)
            reported.extend(job.poll())
            clock.advance(1)
            fake.finish("b", 0, "FAILED", retries_used=0, lag=lag)

        # Polls while the gate still holds b[0], then the gate opens, and polls
        # run past the index lag and a reconcile. b[0] is activated once.
        for _ in range(2):
            clock.advance(5)
            reported.extend(job.poll())
        reported.extend(_release_b_after_a(job, fake, clock))
        for _ in range(14):
            reported.extend(job.poll())
            clock.advance(5)
        assert requested_ids(fake).count(task_id) == 1
        assert [o for o in reported if o.key == key] == []

        # The run after the activation succeeds, and only it is reported.
        fake.finish("b", 0, lag=lag)
        second_end: datetime = clock.now()
        for _ in range(14):
            clock.advance(5)
            reported.extend(job.poll())
    assert [(o.status, o.ended_at) for o in reported if o.key == key] == [
        (TaskStatus.SUCCEEDED, second_end)
    ]
    assert fake.accepted == [task_id_of("a", 0), task_id]
    assert caplog.text.count("before this job activated it") == 1
    assert "b[0] reached FAILED, but the farm ran it before this job activated it." in caplog.text


def test_a_held_task_whose_failure_the_farm_retries_while_held_reports_that_retry_and_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dependent task read at its release, that the farm runs on its own, fails with retries left, and retries to success while the gate holds it, reports that success once, is never activated, and is logged once.

    Both runs came after the release read, so the success had its payload and
    is the item's result. The failure was already logged as a run the farm made
    on its own, so logging the success too would report one gate violation
    twice.
    """
    # b[0] is read while suspended, then fails on the farm, and a poll sees it.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 1, ("a",))], max_retries=2)
    job.release(TaskKey("b", 0))
    reported: List[TaskOutcome] = job.poll()
    with caplog.at_level(logging.ERROR):
        clock.advance(1)
        fake.finish("b", 0, "FAILED", retries_used=0)
        clock.advance(1)
        reported.extend(job.poll())
        assert caplog.text.count("before this job activated it") == 1

        # The farm retries it, still unactivated, and the retry succeeds before
        # the gate opens.
        clock.advance(1)
        fake.finish("b", 0, retries_used=1)
        retry_end: datetime = clock.now()
        reported.extend(_release_b_after_a(job, fake, clock))
        for _ in range(12):
            reported.extend(job.poll())
            clock.advance(5)
    assert fake.accepted == [task_id_of("a", 0)]
    assert [(o.key, o.status, o.ended_at) for o in reported if o.key.step_name == "b"] == [
        (TaskKey("b", 0), TaskStatus.SUCCEEDED, retry_end)
    ]
    # The one record names the failed run and promises no later run, because
    # the farm's own retry, not a run after activation, is what the cook got.
    records: List[str] = [
        r.getMessage() for r in caplog.records if "before this job activated it" in r.getMessage()
    ]
    assert records == [
        "Task b[0] reached FAILED, but the farm ran it before this job activated it."
        " Its result is not used."
    ]


def test_a_task_whose_read_before_activation_fails_is_activated_once_a_later_read_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the read just before activation fails, or leaves a task out, that task is not activated until a later read returns it.

    Activating it unread could make the farm run again a task that has
    already run with its payload.
    """
    # b[0] and b[1] are read at their release, then the gate opens while reads fail.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 2, ("a",))])
    job.release(TaskKey("b", 0))
    job.release(TaskKey("b", 1))
    job.poll()
    with caplog.at_level(logging.WARNING, logger="deadline.houdini_pdg"):
        _release_b_after_a(job, fake, clock)
        fake.faults["BatchGetTask"] = _client_error("ThrottlingException", "BatchGetTask")
        job.poll()
        assert requested_ids(fake) == [task_id_of("a", 0)]
        assert "Could not check 2 task(s) before activating them" in caplog.text

        # The next read loses b[0], so only b[1] is activated.
        del fake.faults["BatchGetTask"]
        fake.lost = {task_id_of("b", 0)}
        clock.advance(5)
        job.poll()
        assert fake.accepted == [task_id_of("a", 0), task_id_of("b", 1)]
        assert f"Could not check task {task_id_of('b', 0)} before activating it." in caplog.text

    # Once b[0] can be read, it is activated too.
    fake.lost = set()
    clock.advance(5)
    job.poll()
    assert fake.accepted == [task_id_of("a", 0), task_id_of("b", 1), task_id_of("b", 0)]


def test_tasks_the_farm_is_running_do_not_hold_up_the_tasks_released_after_them() -> None:
    """When a whole batch of a hundred released tasks is running on the farm on its own, the tasks after them are still activated in the same poll.

    Stopping at a batch with nothing to activate would stall every task
    released later for as long as those runs take.
    """
    # b's first hundred tasks run on the farm on their own while held; b[100] does not.
    job, fake, clock, _ = started([("a", 1, ()), ("b", 101, ("a",))])
    for i in range(101):
        job.release(TaskKey("b", i))
    job.poll()
    for i in range(100):
        fake.tasks[task_id_of("b", i)]["runStatus"] = "RUNNING"

    # One poll after the gate opens activates b[100] alone.
    _release_b_after_a(job, fake, clock)
    job.poll()
    assert fake.accepted == [task_id_of("a", 0), task_id_of("b", 100)]


# -- polling --


def test_the_first_search_has_no_time_filter_and_later_ones_trail_the_newest_result() -> None:
    """The search asks for the four final statuses, oldest first, and then only for tasks that ended after the newest result minus two minutes.

    The index publishes late and out of order, so the window must trail what it
    has seen. ``updatedAt`` does not change when a worker finishes a task, so it
    is never used.
    """
    job, fake, clock, _ = started([("a", 2, ())])
    job.release(TaskKey("a", 0))
    job.poll()
    first: Dict[str, Any] = fake.ops("SearchTasks")[0]
    assert first["filterExpressions"]["filters"] == [
        {
            "stringListFilter": {
                "name": "RUN_STATUS",
                "operator": "ANY_EQUALS",
                "values": FINAL_STATUSES,
            }
        }
    ]
    assert first["pageSize"] == 100 and first["itemOffset"] == 0
    assert "UPDATED_AT" not in repr(fake.calls)

    # A result that ended 30 s ago moves the window to 150 s ago.
    clock.advance(600)
    fake.finish("a", 0, ended=clock.now() - timedelta(seconds=30))
    outcomes: List[TaskOutcome] = job.poll()
    assert statuses_of(outcomes) == [("a", 0, TaskStatus.SUCCEEDED)]
    clock.advance(1)
    job.poll()
    assert ended_filter(fake) == _T0 + timedelta(seconds=600 - 150)


def test_searches_page_by_offset_and_stop_at_the_ten_thousand_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full pages are followed by offset up to the service's 10,000 cap; the search then stops with a warning, and the window moves past what it read."""
    job, fake, clock, _ = started([("a", 1, ())])
    fake.endless_search = True
    clock.advance(20_000)
    with caplog.at_level(logging.WARNING):
        job.poll()
    offsets: List[int] = [s["itemOffset"] for s in fake.ops("SearchTasks")]
    assert offsets == list(range(0, 10_000, 100))
    assert all(s["pageSize"] == 100 for s in fake.ops("SearchTasks"))
    assert "10000-result limit" in caplog.text

    # The newest result read ended at offset 9,999 seconds.
    fake.endless_search = False
    job.poll()
    assert ended_filter(fake) == _T0 + timedelta(seconds=9_999 - 120)


def test_a_short_page_or_an_offset_that_does_not_advance_ends_the_search() -> None:
    """A page under a hundred results, or a next offset that does not move forward, is the last page, so a confused answer cannot loop."""
    # A full page whose next offset does not move forward, then a short page.
    job, fake, _, _ = started([("a", 1, ())])
    fake.malformed["SearchTasks"] = {"tasks": [{}] * 100, "nextItemOffset": 0}
    job.poll()
    assert len(fake.ops("SearchTasks")) == 1
    fake.malformed["SearchTasks"] = {"tasks": [{}] * 99, "nextItemOffset": 100}
    job.poll()
    assert len(fake.ops("SearchTasks")) == 2


def test_a_search_that_fails_part_way_keeps_its_results_but_not_its_window() -> None:
    """A search that fails on a later page returns what it already read, and leaves the window where it was, so the next poll reads the rest.

    Moving the window past a page that was never read would lose those results
    to every later search.
    """
    job, fake, clock, _ = started([("a", 150, ())])
    fake.reconcile_blind = True
    for i in range(150):
        job.release(TaskKey("a", i))
    job.poll()
    clock.advance(600)
    for i in range(150):
        fake.finish("a", i, ended=_T0 + timedelta(seconds=i))

    # The first page is read, the second fails.
    fake.fail_search_at_offset = 100
    first: List[TaskOutcome] = job.poll()
    assert [o.key.task_index for o in first] == list(range(100))

    # The next search has no window yet, and finds the rest.
    fake.fail_search_at_offset = None
    second: List[TaskOutcome] = job.poll()
    assert ended_filter(fake) is None
    assert [o.key.task_index for o in second] == list(range(100, 150))
    job.poll()
    assert ended_filter(fake) == _T0 + timedelta(seconds=149 - 120)


@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    tasks=st.lists(
        st.tuples(st.sampled_from(FINAL_STATUSES), st.one_of(st.none(), st.integers(0, 4))),
        min_size=1,
        max_size=8,
    ),
    budget=st.integers(0, 3),
)
def test_each_final_status_is_reported_once_as_the_service_defines_it(
    tasks: List[Tuple[str, Optional[int]]], budget: int
) -> None:
    """For any mix of final statuses and retry counts, each task's outcome follows the service's meaning, once.

    ``SUCCEEDED`` succeeds. ``NOT_COMPATIBLE`` fails. ``FAILED`` fails once its
    retries are used up, or when its retry count is unreadable, and is not
    reported while the farm will retry it. ``CANCELED`` is returned as canceled,
    never as a failure.
    """
    job, fake, clock, _ = started([("a", len(tasks), ())], max_retries=budget)
    for i in range(len(tasks)):
        job.release(TaskKey("a", i))
    job.poll()
    for i, (status, used) in enumerate(tasks):
        fake.finish("a", i, status, retries_used=used)
    clock.advance(1)

    # The service's meaning of each status, as a table.
    expected: List[Tuple[str, int, TaskStatus]] = []
    for i, (status, used) in enumerate(tasks):
        if status == "SUCCEEDED":
            expected.append(("a", i, TaskStatus.SUCCEEDED))
        elif status == "NOT_COMPATIBLE":
            expected.append(("a", i, TaskStatus.FAILED))
        elif status == "CANCELED":
            expected.append(("a", i, TaskStatus.CANCELED))
        elif used is None or used >= budget:
            expected.append(("a", i, TaskStatus.FAILED))
    assert statuses_of(job.poll()) == expected
    clock.advance(61)
    assert job.poll() == []


def test_a_failure_the_farm_retries_is_reported_only_when_the_retries_run_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed task with retries left is not reported; the same task failing on its last retry is.

    The task was activated, so neither failure is logged as a run the farm made
    on its own: that log is for a task this job never activated.
    """
    # The first failure leaves a retry; the second uses the last one.
    job, fake, clock, _ = started([("a", 1, ())], max_retries=2)
    job.release(TaskKey("a", 0))
    job.poll()
    fake.finish("a", 0, "FAILED", retries_used=1)
    clock.advance(1)
    with caplog.at_level(logging.ERROR):
        assert job.poll() == []
        clock.advance(30)
        fake.finish("a", 0, "FAILED", retries_used=2)
        assert statuses_of(job.poll()) == [("a", 0, TaskStatus.FAILED)]
    assert "before this job activated it" not in caplog.text


@pytest.mark.parametrize("budget", [None, -1, True, "2"])
def test_an_unknown_retry_budget_holds_failures_until_the_job_status_gives_one(
    budget: Any,
) -> None:
    """Until the job's retry budget is read as a count, a failure is held back, since the farm may still retry it; the next status read supplies it."""
    # The budget read at start is not a count.
    job, fake, clock, _ = make([("a", 1, ())])
    fake.malformed["GetJob"] = {**fake.job, "maxRetriesPerTask": budget}
    job.start()
    job.release(TaskKey("a", 0))
    job.poll()
    fake.finish("a", 0, "FAILED", retries_used=1)
    clock.advance(1)
    assert job.poll() == []

    del fake.malformed["GetJob"]
    assert job.status().lifecycle_status == "CREATE_COMPLETE"
    clock.advance(1)
    assert statuses_of(job.poll()) == [("a", 0, TaskStatus.FAILED)]


def test_an_unreadable_retry_budget_does_not_fail_start() -> None:
    """A failed budget read is only a warning, and the steps are still suspended: the job's status is read again on every tick."""
    # Only the budget read fails.
    job, fake, _, _ = make(CHAIN)
    fake.faults["GetJob"] = _client_error("ThrottlingException", "GetJob")
    job.start()
    assert len(fake.ops("GetJob")) == 1 and len(fake.ops("UpdateStep")) == 2


def test_a_cancellation_is_returned_once_and_never_as_a_failure() -> None:
    """A canceled task is returned once as canceled, however often the search or the reconcile sees it again."""
    # One task activated and one never released, both canceled on the farm.
    job, fake, clock, _ = started([("a", 2, ())])
    job.release(TaskKey("a", 0))
    job.poll()
    fake.finish("a", 0, "CANCELED")
    fake.finish("a", 1, "CANCELED")
    clock.advance(1)
    first: List[TaskOutcome] = job.poll()
    clock.advance(61)
    second: List[TaskOutcome] = job.poll()
    assert statuses_of(first) == [("a", 0, TaskStatus.CANCELED), ("a", 1, TaskStatus.CANCELED)]
    assert second == []
    assert fake.ops("BatchGetTask")


def test_polling_never_raises_when_every_call_fails() -> None:
    """Every call failing gives empty polls, not an exception, so PDG's tick keeps running."""
    # Every call a poll makes fails.
    job, fake, clock, _ = started()
    for op in ("ListSteps", "BatchUpdateTask", "SearchTasks", "BatchGetTask"):
        fake.faults[op] = _client_error("InternalServerErrorException", op)
    job.release(TaskKey("a", 0))
    for _ in range(3):
        clock.advance(61)
        assert job.poll() == []


def test_a_malformed_response_in_one_poll_part_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A search answer that cannot be read is logged, and the reconcile after it still reports what it finds."""
    # The search and step answers cannot be read; the reconcile is due a minute later.
    job, fake, clock, _ = started([("a", 1, ())])
    job.release(TaskKey("a", 0))
    job.poll()
    fake.finish("a", 0)
    fake.malformed["SearchTasks"] = {"tasks": [None]}
    fake.malformed["ListSteps"] = {"steps": [None]}
    clock.advance(61)
    with caplog.at_level(logging.ERROR):
        outcomes: List[TaskOutcome] = job.poll()
    assert statuses_of(outcomes) == [("a", 0, TaskStatus.SUCCEEDED)]
    assert "Polling job" in caplog.text


def test_reconcile_finds_a_result_the_index_never_published() -> None:
    """Every minute, the activated tasks with no result are read directly, so a result the search misses is still reported, once."""
    job, fake, clock, _ = started([("a", 1, ())])
    job.release(TaskKey("a", 0))
    job.poll()
    clock.advance(400)
    job.poll()

    # Ended long before the window, and never published: no search finds it.
    fake.finish("a", 0, ended=_T0 + timedelta(seconds=1))
    fake.tasks[task_id_of("a", 0)]["published"] = None
    clock.advance(30)
    assert job.poll() == []
    clock.advance(31)
    assert statuses_of(job.poll()) == [("a", 0, TaskStatus.SUCCEEDED)]
    assert fake.ops("BatchGetTask")[-1]["identifiers"][0]["taskId"] == task_id_of("a", 0)

    # The task is final, so no reconcile asks about it again.
    reconciles: int = len(fake.ops("BatchGetTask"))
    clock.advance(61)
    assert job.poll() == []
    assert len(fake.ops("BatchGetTask")) == reconciles


def test_a_failed_reconcile_is_retried_a_minute_later() -> None:
    """A failed reconcile reports nothing, and the next one a minute later asks again."""
    # The first reconcile fails; the next is due a minute after it.
    job, fake, clock, _ = started([("a", 1, ())])
    job.release(TaskKey("a", 0))
    job.poll()
    fake.faults["BatchGetTask"] = _client_error("ThrottlingException", "BatchGetTask")
    clock.advance(61)
    assert job.poll() == []
    del fake.faults["BatchGetTask"]
    fake.finish("a", 0, ended=_T0)
    fake.tasks[task_id_of("a", 0)]["published"] = None
    clock.advance(59)
    assert job.poll() == []
    clock.advance(2)
    assert statuses_of(job.poll()) == [("a", 0, TaskStatus.SUCCEEDED)]


def test_reconcile_runs_once_a_minute_counted_from_start() -> None:
    """The first reconcile comes a minute after start and the next a minute after that, not sooner, so it does not flood the service."""
    # The poll at start reads the released task twice, at its release and just
    # before its activation.
    job, fake, clock, _ = started([("a", 1, ())])
    job.release(TaskKey("a", 0))
    job.poll()
    reads: int = len(fake.ops("BatchGetTask"))

    # Polls just before each minute read nothing directly; the poll on the minute does.
    reconciles: List[int] = []
    for seconds in (59.9, 0.1, 59.9, 0.1):
        clock.advance(seconds)
        job.poll()
        reconciles.append(len(fake.ops("BatchGetTask")) - reads)
    assert reads == 2 and reconciles == [0, 1, 1, 2]


def test_a_task_the_reconcile_cannot_read_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task ``BatchGetTask`` answers with an error is logged, and the other results in the same answer are still reported."""
    # Two activated tasks end unpublished; the service has lost one of them.
    job, fake, clock, _ = started([("a", 2, ())])
    job.release(TaskKey("a", 0))
    job.release(TaskKey("a", 1))
    job.poll()
    for i in range(2):
        fake.finish("a", i)
        fake.tasks[task_id_of("a", i)]["published"] = None
    fake.lost = {task_id_of("a", 0)}
    clock.advance(61)
    with caplog.at_level(logging.DEBUG, logger="deadline.houdini_pdg"):
        outcomes: List[TaskOutcome] = job.poll()
    assert statuses_of(outcomes) == [("a", 1, TaskStatus.SUCCEEDED)]
    assert (
        f"Could not reconcile task {task_id_of('a', 0)}: ResourceNotFoundException" in caplog.text
    )


def test_reconcile_reads_at_most_a_hundred_tasks_per_call() -> None:
    """``BatchGetTask`` takes at most a hundred tasks, so the read at release, the read before activation, and the reconcile all split their reads."""
    # 250 tasks read and activated, and reconciled a minute later.
    job, fake, clock, _ = started([("a", 250, ())])
    for i in range(250):
        job.release(TaskKey("a", i))
    job.poll()
    clock.advance(61)
    job.poll()
    calls: List[Dict[str, Any]] = fake.ops("BatchGetTask")
    assert [len(c["identifiers"]) for c in calls] == [100, 100, 50] * 3
    for part in (calls[:3], calls[3:6], calls[6:]):
        assert sorted(i["taskId"] for c in part for i in c["identifiers"]) == sorted(
            task_id_of("a", i) for i in range(250)
        )


@settings(max_examples=50, deadline=None)
@given(
    st.lists(st.tuples(st.integers(-10_000, 10_000), st.integers(0, 600)), min_size=1, max_size=20)
)
def test_the_window_trails_the_newest_result_and_now_and_never_moves_back(
    steps: List[Tuple[int, int]],
) -> None:
    """For any sequence of newest results and clock times, the window is ``max(current, min(newest - 120 s, now - 120 s))``.

    So it never moves back, and one result with a skewed clock cannot jump it
    past the tasks still running.
    """
    job, fake, clock, _ = started([("a", 1, ())])
    windows: List[datetime] = []
    for seen_offset, wait in steps:
        clock.advance(wait)
        seen: datetime = clock.now() + timedelta(seconds=seen_offset)
        fake.extra_results = [
            {"stepId": "step-x", "taskId": "task-x", "runStatus": "SUCCEEDED", "endedAt": seen}
        ]
        job.poll()

        # The formula above, against the next search's filter.
        candidate: datetime = min(seen, clock.now()) - timedelta(seconds=120)
        windows.append(max(windows[-1], candidate) if windows else candidate)
        fake.extra_results = []
        job.poll()
        assert ended_filter(fake) == windows[-1]


@pytest.mark.parametrize("reconcile", [False, True])
@settings(max_examples=20, deadline=None)
@given(data=st.data())
def test_every_finished_task_is_reported_exactly_once_despite_index_lag(
    reconcile: bool, data: Any
) -> None:
    """For any end times, publication lags, and tick interval, each task is reported exactly once.

    With a lag of up to two minutes the trailing window alone finds every
    result. Beyond that, the reconcile catches what the search misses.
    """
    max_lag: int = 3_600 if reconcile else 120
    count: int = data.draw(st.integers(1, 12), label="tasks")
    ends: List[int] = data.draw(
        st.lists(st.integers(0, 900), min_size=count, max_size=count), label="ends"
    )
    lags: List[int] = data.draw(
        st.lists(st.integers(0, max_lag), min_size=count, max_size=count), label="lags"
    )
    interval: int = data.draw(st.integers(1, 90), label="poll interval")
    job, fake, clock, _ = started([("a", count, ())])
    fake.reconcile_blind = not reconcile
    for i in range(count):
        job.release(TaskKey("a", i))
    # Every task is activated before any ends, so each runs exactly once.
    seen: List[int] = [o.key.task_index for o in job.poll()]

    # Tasks end on schedule while PDG ticks, until every lag has passed.
    finished: Set[int] = set()
    while clock.now() < _T0 + timedelta(seconds=900 + max_lag + 300):
        for i in range(count):
            if i not in finished and _T0 + timedelta(seconds=ends[i]) <= clock.now():
                finished.add(i)
                fake.finish("a", i, ended=_T0 + timedelta(seconds=ends[i]), lag=lags[i])
        seen.extend(o.key.task_index for o in job.poll())
        clock.advance(interval)
    assert sorted(seen) == list(range(count))


# -- the job --


def test_status_maps_get_job_and_is_empty_when_the_read_fails() -> None:
    """The job's status is ``GetJob``'s, and an empty one when the read fails or cannot be read, so the cook keeps going."""
    # A real status, then an answer that cannot be read, then a failed call.
    job, fake, _, _ = started()
    fake.job = {
        "lifecycleStatus": "UPDATE_FAILED",
        "taskRunStatus": "FAILED",
        "taskRunStatusCounts": {"FAILED": 1},
    }
    assert job.status() == JobStatus("UPDATE_FAILED", "FAILED", {"FAILED": 1})
    fake.malformed["GetJob"] = None
    assert job.status() == JobStatus()
    fake.faults["GetJob"] = _client_error("ThrottlingException", "GetJob")
    assert job.status() == JobStatus()


def test_cancel_cancels_the_job_and_logs_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Cancel sends one job-wide cancellation, and a refused one is logged, not raised, so stopping a cook always finishes."""
    # One cancellation, then a refused one.
    job, fake, _, _ = started()
    job.cancel()
    assert fake.ops("UpdateJob") == [
        {"farmId": FARM, "queueId": QUEUE, "jobId": JOB, "targetTaskRunStatus": "CANCELED"}
    ]
    fake.faults["UpdateJob"] = _client_error("AccessDeniedException", "UpdateJob")
    with caplog.at_level(logging.ERROR):
        job.cancel()
    assert "Could not cancel job" in caplog.text


def test_fail_marks_the_job_failed() -> None:
    """Fail sends one job-wide FAILED, so no suspended task is left waiting, and the job does not read as stopped by the artist."""
    job, fake, _, _ = started()
    job.fail()
    assert fake.ops("UpdateJob") == [
        {"farmId": FARM, "queueId": QUEUE, "jobId": JOB, "targetTaskRunStatus": "FAILED"}
    ]


def test_fail_cancels_the_job_when_the_service_refuses_failed() -> None:
    """When the service will not mark the job failed, the job is canceled instead, so its tasks still end."""
    # The service refuses FAILED but accepts CANCELED.
    job, fake, _, _ = started()
    fake.refused_job_statuses = {"FAILED"}
    job.fail()
    assert [u["targetTaskRunStatus"] for u in fake.ops("UpdateJob")] == ["FAILED", "CANCELED"]


def test_fail_never_raises_when_the_service_refuses_both(caplog: pytest.LogCaptureFixture) -> None:
    """With both updates refused, fail logs the cancel failure and returns, so ending a cook never raises."""
    # Both targets refused.
    job, fake, _, _ = started()
    fake.refused_job_statuses = {"FAILED", "CANCELED"}
    with caplog.at_level(logging.WARNING):
        job.fail()
    assert "Could not mark job" in caplog.text and "Could not cancel job" in caplog.text


# -- log tails --


class FakeLogReader:
    """A session log reader that serves fixed events and records what it was asked."""

    def __init__(self, messages: List[str], error: Optional[Exception] = None) -> None:
        self.messages: List[str] = messages
        self.error: Optional[Exception] = error
        self.requests: List[Dict[str, Any]] = []

    def __call__(self, farm_id: str, queue_id: str, *, session_id: str, limit: int) -> Any:
        """Every event, as ``get_session_logs`` returns them, or the planted error."""
        self.requests.append(
            {"farm_id": farm_id, "queue_id": queue_id, "session_id": session_id, "limit": limit}
        )
        if self.error is not None:
            raise self.error
        return SimpleNamespace(events=[SimpleNamespace(message=m) for m in self.messages])


@settings(max_examples=40, deadline=None)
@given(
    hexes=st.lists(
        st.text("0123456789abcdef", min_size=32, max_size=32), min_size=1, max_size=4, unique=True
    ),
    order=st.randoms(use_true_random=False),
    messages=st.lists(st.text("abc \n", max_size=12), max_size=30),
    lines=st.integers(1, 20),
)
def test_the_log_tail_reads_the_latest_session_named_by_its_actions(
    hexes: List[str], order: Any, messages: List[str], lines: int
) -> None:
    """For any session actions, the tail comes from the session of the latest-started action, named by its action ID, and is the log's last lines.

    The action summaries carry no session ID, and an action ID of another shape
    names no session, even when it started last.
    """
    # Distinct start times, shuffled, and a malformed ID that starts last.
    actions: List[Dict[str, Any]] = [
        {
            "sessionActionId": f"sessionaction-{h}-{n}",
            "status": "FAILED",
            "startedAt": _T0 + timedelta(seconds=n),
            "definition": {"taskRun": {"stepId": step_id_of("a"), "taskId": task_id_of("a", 0)}},
        }
        for n, h in enumerate(hexes)
    ]
    order.shuffle(actions)
    actions.append({"sessionActionId": "sessionaction-xyz-0", "startedAt": _T0 + timedelta(days=1)})

    reader: FakeLogReader = FakeLogReader(messages)
    job, fake, _, _ = started([("a", 1, ())], log_reader=reader)
    fake.session_actions[task_id_of("a", 0)] = actions
    tail: List[str] = job.log_tail(TaskKey("a", 0), lines)

    all_lines: List[str] = [line for m in messages for line in m.splitlines()]
    assert reader.requests == [
        {"farm_id": FARM, "queue_id": QUEUE, "session_id": f"session-{hexes[-1]}", "limit": lines}
    ]
    assert tail == all_lines[-lines:]
    assert fake.ops("ListSessionActions")[0]["taskId"] == task_id_of("a", 0)


def test_the_log_tail_is_empty_on_any_failure() -> None:
    """No actions, a failing reader, a failing listing, an unknown task, or zero lines give an empty tail, never an error."""
    # No actions yet, so the reader is never asked.
    reader: FakeLogReader = FakeLogReader(["x"], error=RuntimeError("no logs"))
    job, fake, _, _ = started([("a", 1, ())], log_reader=reader)
    assert job.log_tail(TaskKey("a", 0), 10) == []
    assert reader.requests == []

    fake.session_actions[task_id_of("a", 0)] = [
        {"sessionActionId": "sessionaction-" + "a" * 32 + "-0", "status": "FAILED"}
    ]
    assert job.log_tail(TaskKey("a", 0), 10) == []
    assert len(reader.requests) == 1
    assert job.log_tail(TaskKey("a", 0), 0) == []
    assert job.log_tail(TaskKey("a", 7), 10) == []
    fake.faults["ListSessionActions"] = _client_error("ThrottlingException", "ListSessionActions")
    assert job.log_tail(TaskKey("a", 0), 10) == []
    assert len(reader.requests) == 1


def test_the_log_reader_defaults_to_the_client_library_function() -> None:
    """A job built without a log reader reads worker logs with ``deadline.client.api.get_session_logs``.

    The scheduler builds the job without passing a reader, so a wrong default
    would make every log tail silently empty: ``log_tail`` swallows the error.
    """
    parameter: inspect.Parameter = inspect.signature(DeadlineFarmJob.__init__).parameters[
        "get_session_logs"
    ]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is api.get_session_logs


# -- threads --


def test_release_and_poll_from_two_threads_activate_each_task_once() -> None:
    """PDG releases on its schedule thread while it polls on its tick thread; every task is still activated exactly once."""
    job, fake, _, _ = started([("a", 400, ())])
    keys: List[TaskKey] = [TaskKey("a", i) for i in range(400)]

    def releaser() -> None:
        """Release every key twice, as a repeated offer would."""
        for key in keys:
            job.release(key)
            job.release(key)

    # Poll on this thread while another releases, until it finishes.
    thread: threading.Thread = threading.Thread(target=releaser)
    thread.start()
    while thread.is_alive():
        job.poll()
    thread.join()
    job.poll()
    assert sorted(fake.accepted) == sorted(task_id_of("a", i) for i in range(400))
    assert len(fake.accepted) == len(set(fake.accepted))


def test_a_slow_activation_call_does_not_block_a_release_from_another_thread() -> None:
    """A release returns while an activation call is still waiting on the network.

    A lock held over the call would freeze PDG's schedule thread for as long as
    the farm takes to answer.
    """
    job, fake, _, _ = started([("a", 2, ())])
    fake.hold_activation = threading.Event()
    job.release(TaskKey("a", 0))
    poller: threading.Thread = threading.Thread(target=job.poll)
    poller.start()
    assert fake.activation_started.wait(5)

    # Release from another thread while the call waits.
    releaser: threading.Thread = threading.Thread(target=job.release, args=(TaskKey("a", 1),))
    releaser.start()
    releaser.join(5)
    blocked: bool = releaser.is_alive()
    fake.hold_activation.set()
    poller.join(5)
    assert not blocked

    fake.hold_activation = None
    job.poll()
    assert fake.accepted == [task_id_of("a", 0), task_id_of("a", 1)]


def test_the_job_exposes_no_public_attribute_another_thread_could_change() -> None:
    """A started job that has released and polled has no public attribute.

    PDG calls the job from several threads, and the release queue guards itself.
    A public attribute would let a caller read or change state without that guard.
    """
    # Release and poll past the reconcile, so every attribute the job sets lazily exists.
    job, _, clock, _ = started([("a", 2, ())])
    job.release(TaskKey("a", 0))
    clock.advance(61)
    job.poll()
    assert [name for name in vars(job) if not name.startswith("_")] == []


# -- the port --


def test_the_farm_job_offers_every_method_of_its_port_with_the_same_signature() -> None:
    """The cook holds a ``FarmJob``, so a method missing or shaped differently here would fail only in the middle of a cook."""
    port: Dict[str, inspect.Signature] = {
        name: inspect.signature(member)
        for name, member in vars(FarmJob).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }
    assert "fail" in port and "poll" in port
    for name, signature in port.items():
        assert str(inspect.signature(getattr(DeadlineFarmJob, name))) == str(signature), name
