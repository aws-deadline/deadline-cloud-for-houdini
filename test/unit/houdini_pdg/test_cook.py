# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the cook: the PDG callbacks, the phases, submission, dispatch, and reporting.

PDG, the submitter, storage, the farm job, the journal, and the clock are small fakes
built from the ports, and every fake call goes into one ordered record. Validation is
the real one, so a refused graph here is a graph validation really refuses.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
)

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from typing_extensions import TypeAlias

from deadline.houdini_pdg.cook import (
    ALLOWED_TRANSITIONS,
    Cook,
    CookServices,
    PdgScheduler,
    ScheduleResult,
    SystemClock,
    _CookStateMachine,
    _OfferedItem,
    _WorkItemTracker,
)
from deadline.houdini_pdg.diagnostics import CookJournal
from deadline.houdini_pdg.errors import (
    GraphRefusedError,
    JobSubmissionError,
    PayloadTransferError,
    QuotaExceededError,
    TaskMapIncompleteError,
    artist_message,
)
from deadline.houdini_pdg.graph import (
    DeclaredFile,
    GraphSnapshot,
    NodeInfo,
    RawItem,
    TaskKey,
    WorkItemSpec,
)
from deadline.houdini_pdg.houdini.settings import SchedulerSettings
from deadline.houdini_pdg.ports import (
    CookPhase,
    CookSummary,
    ItemOutcome,
    ItemState,
    JobStatus,
    OutputFile,
    SceneFiles,
    TaskAddress,
    TaskOutcome,
    TaskStatus,
    WorkItemHandle,
)
from deadline.houdini_pdg.scene_reads import ReadReport, SceneRead, SceneReads
from deadline.houdini_pdg.validation import RefusalScenario

_T0: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
WORKING_DIR: str = "/proj/cook"
JOB_ID: str = "job-" + "a" * 32
PACKAGE_LOGGER: str = "deadline.houdini_pdg"
#: How long apart ticks must be for each to poll the farm.
POLL_SECONDS: float = 5

#: A step graph as (name, item count, upstream names).
Steps: TypeAlias = Sequence[Tuple[str, int, Sequence[str]]]

CHAIN: Steps = (("a", 2, ()), ("b", 1, ("a",)))
#: Two steps that do not depend on each other, so either can report last.
PAIR: Steps = (("a", 1, ()), ("b", 1, ()))


@pytest.fixture(autouse=True)
def _no_hip_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The cook reads HIP from the environment before the scene. A HIP left over
    # from the shell would change what validation sees.
    monkeypatch.delenv("HIP", raising=False)
    yield


class FakeClock:
    """A clock that moves when a test moves it, and by ``step`` seconds after each read."""

    def __init__(self) -> None:
        self.at: datetime = _T0
        self.step: float = 0
        #: Run once, inside the next read.
        self.hook: Optional[Callable[[], Any]] = None

    def now(self) -> datetime:
        # The hook is taken before it runs, so a read inside it does not run it again.
        hook: Optional[Callable[[], Any]] = self.hook
        self.hook = None
        if hook is not None:
            hook()
        read: datetime = self.at
        self.at += timedelta(seconds=self.step)
        return read

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def nodes_for(steps: Steps, outputs: Optional[Dict[str, str]] = None) -> Tuple[NodeInfo, ...]:
    """Static TOP nodes that each run a job script, with ``outputs[name]`` as each item's declared file."""
    found: Dict[str, str] = outputs or {}
    return tuple(
        NodeInfo(
            name=name,
            is_dynamic=False,
            top_type="Processor",
            upstream=tuple(upstream),
            items=tuple(
                RawItem(
                    name=f"{name}_{i}",
                    index=i,
                    item_id=100 * n + i,
                    command="hython job.py",
                    declared_outputs=(
                        (DeclaredFile(found[name].format(i=i), "file/geo"),)
                        if name in found
                        else ()
                    ),
                )
                for i in range(count)
            ),
            schedulable=True,
        )
        for n, (name, count, upstream) in enumerate(steps)
    )


#: A command server node, which validation refuses because a worker cannot reach it.
_COMMAND_SERVER: NodeInfo = NodeInfo(
    name="server",
    is_dynamic=False,
    top_type="Processor",
    upstream=(),
    items=(RawItem(name="server_0", index=0, item_id=900, command="hython job.py"),),
    schedulable=True,
    node_type_name="houdiniserver",
)


def _replace(nodes: Tuple[NodeInfo, ...], name: str, **changes: Any) -> Tuple[NodeInfo, ...]:
    return tuple(dataclasses.replace(n, **changes) if n.name == name else n for n in nodes)


#: Changes to CHAIN's nodes that validation refuses, and the scenario each one raises.
REFUSED_GRAPHS: Dict[
    str, Tuple[Callable[[Tuple[NodeInfo, ...]], Tuple[NodeInfo, ...]], RefusalScenario]
] = {
    "dynamic": (lambda nodes: _replace(nodes, "a", is_dynamic=True), RefusalScenario.DYNAMIC_NODE),
    "other scheduler": (
        lambda nodes: _replace(nodes, "b", scheduler_override="/obj/topnet/localscheduler"),
        RefusalScenario.MULTIPLE_SCHEDULERS,
    ),
    "command server": (
        lambda nodes: nodes + (dataclasses.replace(_COMMAND_SERVER, upstream=()),),
        RefusalScenario.SERVICE_NODE,
    ),
}


def refused(*names: str) -> Tuple[NodeInfo, ...]:
    """CHAIN's nodes with each named refusal applied."""
    return functools.reduce(
        lambda nodes, name: REFUSED_GRAPHS[name][0](nodes), names, nodes_for(CHAIN)
    )


def handle(name: str, declared: Tuple[DeclaredFile, ...] = ()) -> WorkItemHandle:
    """The work item PDG offers for ``<node>_<index>``."""
    # The name carries the node and the index, as PDG names items.
    node: str
    index: str
    node, index = name.rsplit("_", 1)
    return WorkItemHandle(
        item_id=hash(name) % 10_000,
        index=int(index),
        name=name,
        node_name=node,
        declared_outputs=declared,
    )


def name_of(key: TaskKey) -> str:
    return f"{key.step_name}_{key.task_index}"


class Events:
    """One ordered record of every call the fakes see, with hooks and faults by call name."""

    def __init__(self) -> None:
        self.log: List[Tuple[str, Any]] = []
        #: Run once, the next time the named call is made.
        self.hooks: Dict[str, Callable[[], Any]] = {}
        #: Raised by every named call until removed.
        self.faults: Dict[str, BaseException] = {}
        #: Raised once, by the next named call.
        self.fail_once: Dict[str, BaseException] = {}

    def __call__(self, name: str, detail: Any = None) -> None:
        # Record the call, then run its hook, then raise its fault.
        self.log.append((name, detail))
        hook: Optional[Callable[[], Any]] = self.hooks.pop(name, None)
        if hook is not None:
            hook()
        fault: Optional[BaseException] = self.fail_once.pop(name, None) or self.faults.get(name)
        if fault is not None:
            raise fault

    def names(self) -> List[str]:
        return [name for name, _ in self.log]

    def count(self, name: str) -> int:
        return self.names().count(name)

    def details(self, name: str) -> List[Any]:
        return [detail for n, detail in self.log if n == name]


class FakeHost:
    """PDG and the scene: a fixed graph, and a record of what PDG is told."""

    def __init__(self, events: Events, nodes: Tuple[NodeInfo, ...], scene: SceneReads) -> None:
        self.events: Events = events
        self.nodes: Tuple[NodeInfo, ...] = nodes
        self.scene: SceneReads = scene
        self.hip_file: str = f"{WORKING_DIR}/scene.hip"
        #: What ``pending_generation`` returns, one entry per call; then nothing pending.
        self.pending: List[Any] = []
        #: Each work item's PDG log as (ERROR lines, last line), by item ID.
        self.reasons: Dict[int, Tuple[str, str]] = {}
        #: Whether generation is being watched.
        self.watching: bool = False

    def read_graph(self) -> Sequence[NodeInfo]:
        self.events("read_graph")
        return self.nodes

    def read_scene(self) -> SceneReads:
        self.events("read_scene")
        return self.scene

    def scene_files(self) -> SceneFiles:
        self.events("scene_files")
        return SceneFiles(hip_file=self.hip_file)

    def serialize(self, item: WorkItemHandle) -> str:
        self.events("serialize", item.name)
        return '{"name": "%s"}' % item.name

    def notify(self, item: WorkItemHandle, outcome: ItemOutcome) -> None:
        self.events("notify", (item.name, outcome))

    def set_working_dir(self, path: str) -> None:
        self.events("set_working_dir", path)

    def watch_generation(self, cook_set: Optional[AbstractSet[str]]) -> None:
        # A watch takes effect after the call's hook, as a real one would once registered.
        self.events("watch_generation", cook_set)
        self.watching = True

    def pending_generation(self) -> Sequence[str]:
        # Each queued answer is used once: names, or an error to raise.
        self.events("pending_generation")
        if not self.pending:
            return ()
        answer: Any = self.pending.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return tuple(answer)

    def stop_watching_generation(self) -> None:
        self.events("stop_watching_generation")
        self.watching = False

    def failure_reason(self, item_id: int) -> Tuple[str, str]:
        self.events("failure_reason", item_id)
        return self.reasons.get(item_id, ("", ""))


class FakeSubmitter:
    def __init__(self, events: Events) -> None:
        self.events: Events = events
        self.snapshots: List[GraphSnapshot] = []

    def submit(self, snapshot: GraphSnapshot, bundle_dir: str) -> str:
        self.snapshots.append(snapshot)
        self.events("submit", bundle_dir)
        return JOB_ID


class FakeStorage:
    def __init__(self, events: Events) -> None:
        self.events: Events = events
        #: What each task recorded, by task ID: a list, None, or an error to raise.
        self.outputs: Dict[str, Any] = {}

    def put_payload(self, address: TaskAddress, payload_json: str) -> None:
        self.events("put_payload", (address.task_id, payload_json))

    def get_payload(self, address: TaskAddress) -> Dict[str, Any]:
        raise AssertionError("the scheduler never reads a payload")

    def put_outputs(self, address: TaskAddress, outputs: Sequence[OutputFile]) -> None:
        raise AssertionError("the scheduler never writes an output record")

    def get_outputs(self, address: TaskAddress) -> Optional[List[OutputFile]]:
        self.events("get_outputs", address.task_id)
        found: Any = self.outputs.get(address.task_id)
        if isinstance(found, BaseException):
            raise found
        return found


class FakeFarmJob:
    """A job whose polls and statuses the test queues, one per call."""

    def __init__(self, events: Events, job_id: str, snapshot: GraphSnapshot) -> None:
        # Queued answers, and counts of what the cook asked for.
        self.events: Events = events
        self.job_id: str = job_id
        self.snapshot: GraphSnapshot = snapshot
        self.polls: List[List[TaskOutcome]] = []
        self.statuses: List[JobStatus] = []
        self.status_now: JobStatus = JobStatus("CREATE_COMPLETE", "READY")
        self.released: List[TaskKey] = []
        self.cancels: int = 0
        self.fails: int = 0

    def start(self) -> None:
        self.events("start")

    def address(self, key: TaskKey) -> TaskAddress:
        if self.snapshot.item(key) is None:
            raise TaskMapIncompleteError(f"{key} is not in the job")
        return TaskAddress(
            "farm-1",
            "queue-1",
            self.job_id,
            f"step-{key.step_name}",
            f"task-{key.step_name}-{key.task_index}",
        )

    def release(self, key: TaskKey) -> None:
        self.events("release", key)
        self.released.append(key)

    def poll(self) -> List[TaskOutcome]:
        self.events("poll")
        return self.polls.pop(0) if self.polls else []

    def status(self) -> JobStatus:
        self.events("status")
        if self.statuses:
            self.status_now = self.statuses.pop(0)
        return self.status_now

    def cancel(self) -> None:
        self.cancels += 1
        self.events("cancel", self.job_id)

    def fail(self) -> None:
        self.fails += 1
        self.events("fail", self.job_id)

    def log_tail(self, key: TaskKey, lines: int) -> List[str]:
        self.events("log_tail", (key, lines))
        return ["last words"]


class _Records(logging.Handler):
    """Keeps every record it is handed, as the journal's log handler would write them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class FakeJournal:
    """The journal's calls, recorded in the shared event log."""

    def __init__(self, events: Events) -> None:
        self.events: Events = events
        self.handler: _Records = _Records()
        self.summaries: List[CookSummary] = []

    def log_handler(self) -> logging.Handler:
        return self.handler

    def bundle_dir(self) -> str:
        self.events("bundle_dir")
        return "/tmp/bundle"

    def snapshot_taken(
        self, nodes: Sequence[NodeInfo], snapshot: GraphSnapshot, reads: Optional[ReadReport]
    ) -> None:
        self.events("snapshot_taken", reads)

    def job_submitted(self, job_id: str) -> None:
        self.events("job_submitted", job_id)

    def item_scheduled(self, item: WorkItemHandle) -> None:
        self.events("item_scheduled", item.name)

    def item_failed(self, key: TaskKey, log_tail: Callable[[], List[str]]) -> None:
        self.events("item_failed", (key, log_tail()))

    def closed(self, summary: CookSummary) -> None:
        self.events("closed", summary)
        self.summaries.append(summary)


class Harness:
    """A scheduler wired to the fakes, with the shortcuts most tests need."""

    def __init__(
        self,
        steps: Steps = CHAIN,
        *,
        outputs: Optional[Dict[str, str]] = None,
        nodes: Optional[Tuple[NodeInfo, ...]] = None,
        scene: SceneReads = SceneReads(),
        validate_graph: bool = True,
        journal: Optional[CookJournal] = None,
    ) -> None:
        # The fakes share one event log, so a test can check the order of calls.
        self.validate_graph: bool = validate_graph
        self.events: Events = Events()
        self.clock: FakeClock = FakeClock()
        self.host: FakeHost = FakeHost(
            self.events, nodes if nodes is not None else nodes_for(steps, outputs), scene
        )
        self.submitter: FakeSubmitter = FakeSubmitter(self.events)
        self.storage: FakeStorage = FakeStorage(self.events)
        self.journal: FakeJournal = FakeJournal(self.events)
        #: A real journal, used instead of the fake when given.
        self.real_journal: Optional[CookJournal] = journal
        self.jobs: List[FakeFarmJob] = []
        self.cooks: List[Cook] = []
        self.connect_fault: Optional[BaseException] = None
        self.scheduler: PdgScheduler = PdgScheduler(
            host=self.host,
            clock=self.clock,
            read_settings=self._read_settings,
            connect=self._connect,
        )

    def _read_settings(self) -> SchedulerSettings:
        self.events("read_settings")
        # The default settings, so a test that keeps validation on relies on the default.
        settings: SchedulerSettings = SchedulerSettings(working_dir=WORKING_DIR)
        return (
            settings if self.validate_graph else dataclasses.replace(settings, validate_graph=False)
        )

    def _connect(self, settings: SchedulerSettings, cook_id: str) -> CookServices:
        self.events("connect", cook_id)
        if self.connect_fault is not None:
            raise self.connect_fault
        journal: Any = self.real_journal if self.real_journal is not None else self.journal
        return CookServices(
            submitter=self.submitter,
            storage=self.storage,
            open_job=self._open_job,
            journal=journal,
        )

    def _open_job(self, job_id: str, snapshot: GraphSnapshot) -> FakeFarmJob:
        self.events("open_job", job_id)
        job: FakeFarmJob = FakeFarmJob(self.events, job_id, snapshot)
        self.jobs.append(job)
        return job

    @property
    def cook(self) -> Optional[Cook]:
        """The open cook, which is also kept in ``cooks`` for later checks."""
        current: Optional[Cook] = self.scheduler._cook
        if current is not None and current not in self.cooks:
            self.cooks.append(current)
        return current

    @property
    def job(self) -> FakeFarmJob:
        return self.jobs[-1]

    def start(self, generate_only: bool = False) -> None:
        assert self.scheduler.on_start_cook(generate_only)
        self.cook

    def tick(self) -> None:
        """One tick, then time enough that the next tick polls the farm again."""
        self.scheduler.on_tick()
        self.clock.advance(POLL_SECONDS)

    def cooking(self) -> "Harness":
        """Start, and offer ``a_0`` so the job is submitted."""
        # a_0 is the first offer, so it submits the job.
        self.start()
        assert self.scheduler.on_schedule(handle("a_0")) is ScheduleResult.SUCCEEDED
        assert self.phase() is CookPhase.COOKING
        return self

    def phase(self) -> Optional[CookPhase]:
        cook: Optional[Cook] = self.cook
        return cook.phase() if cook is not None else None

    def history(self, cook: Optional[Cook] = None) -> Tuple[Tuple[CookPhase, CookPhase], ...]:
        """Every phase change of ``cook``, or of the open or last cook."""
        found: Optional[Cook] = cook or self.cook or (self.cooks[-1] if self.cooks else None)
        assert found is not None
        return found._state.history()

    def started_fresh(self) -> bool:
        """Whether a new start now connects a new cook, with a new ID, in CONNECTED."""
        # Count the connects before the start, then check the one it made.
        ids: int = len(self.events.details("connect"))
        self.start()
        connected: List[str] = self.events.details("connect")
        return (
            len(connected) == ids + 1
            and connected[-1] not in connected[:-1]
            and (self.phase() is CookPhase.CONNECTED)
        )

    def notified(self, state: Optional[ItemState] = None) -> List[Tuple[str, ItemOutcome]]:
        return [
            (name, outcome)
            for name, outcome in self.events.details("notify")
            if state is None or outcome.state is state
        ]

    def results(self) -> Dict[str, List[ItemState]]:
        """Every succeeded or failed result PDG was told, by item name."""
        found: Dict[str, List[ItemState]] = {}
        for name, outcome in self.notified():
            if outcome.state is not ItemState.STARTED:
                found.setdefault(name, []).append(outcome.state)
        return found

    def summary(self) -> CookSummary:
        (only,) = self.journal.summaries
        return only


def _traceback_logged(caplog: pytest.LogCaptureFixture, message: str) -> bool:
    """Whether ``message`` was logged with the exception that caused it, for the console."""
    return any(r.getMessage() == message and r.exc_info for r in caplog.records)


# -- starting --


def test_a_generate_touches_no_farm_and_builds_no_journal() -> None:
    """A generate only generates items, so it must not connect, submit, or count as a cook.

    It still sets the working directory, where PDG copies its job scripts.
    """
    h: Harness = Harness()
    h.start(generate_only=True)
    assert h.cook is None
    assert h.events.details("set_working_dir") == [WORKING_DIR]
    # Ticks and offers during it do nothing, and a second start is ignored.
    for _ in range(3):
        h.scheduler.on_tick()
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.FAILED
    assert h.scheduler.on_start_cook(False)
    assert h.cook is None
    assert h.scheduler.on_stop_cook(False)
    assert h.events.names() == ["read_settings", "set_working_dir", "stop_watching_generation"]
    assert h.journal.summaries == []
    # After the stop, a farm cook starts normally.
    h.start()
    assert h.phase() is CookPhase.CONNECTED


def test_the_journal_job_and_summary_share_the_cook_id() -> None:
    """The debug directory, the job, and the outcome event must name the same cook."""
    # The ID passed to connect is the one the summary carries.
    h: Harness = Harness().cooking()
    cook: Optional[Cook] = h.cook
    assert cook is not None
    (cook_id,) = h.events.details("connect")
    assert len(cook_id) == 32
    h.scheduler.on_stop_cook(False)
    assert h.summary().cook_id == cook_id


@settings(max_examples=20, deadline=None)
@given(ticks=st.integers(0, 5), schedules=st.integers(0, 3), generate_only=st.booleans())
def test_only_the_first_schedule_submits_and_ticks_never_do(
    ticks: int, schedules: int, generate_only: bool
) -> None:
    """For any ticks then k offers, the job is submitted min(k, 1) times, and never for a generate.

    A second submission would put a second job on the farm for one cook.
    """
    # Tick first, then offer, counting submissions after each.
    h: Harness = Harness()
    h.start(generate_only=generate_only)
    assert h.phase() is (None if generate_only else CookPhase.CONNECTED)
    for _ in range(ticks):
        h.tick()
    assert h.events.count("submit") == 0
    for i in range(schedules):
        h.scheduler.on_schedule(handle(f"a_{i % 2}"))
    assert h.events.count("submit") == (0 if generate_only else min(schedules, 1))
    assert h.events.count("start") == (0 if generate_only else min(schedules, 1))
    assert h.events.count("read_settings") == 1
    assert h.events.count("connect") == (0 if generate_only else 1)


def test_each_start_builds_a_fresh_cook_with_a_fresh_id() -> None:
    """Nothing carries over between cooks: a new ID, nothing registered, and CONNECTED again."""
    h: Harness = Harness().cooking()
    first: Optional[Cook] = h.cook
    h.scheduler.on_stop_cook(False)
    h.start()
    second: Optional[Cook] = h.cook
    assert first is not None and second is not None and first is not second
    first_id: str
    second_id: str
    first_id, second_id = h.events.details("connect")
    assert first_id != second_id
    assert second.phase() is CookPhase.CONNECTED
    # The new cook submits its own job, and a_0 is new to it.
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.SUCCEEDED
    assert len(h.jobs) == 2 and h.job.released == [TaskKey("a", 0)]


def test_a_start_while_a_cook_is_open_is_ignored() -> None:
    """A start is accepted only with no cook open, so the open cook keeps its job and its state."""
    # Start twice with no stop between.
    h: Harness = Harness()
    h.start()
    cook: Optional[Cook] = h.cook
    assert h.scheduler.on_start_cook(False)
    assert h.cook is cook and h.events.count("connect") == 1


def test_a_failed_start_raises_and_leaves_no_cook_open() -> None:
    """PDG drops a scheduler whose start failed, so the cook is closed and recorded as FAILED."""
    h: Harness = Harness()
    h.events.faults["set_working_dir"] = RuntimeError("no such directory")
    with pytest.raises(RuntimeError):
        h.scheduler.on_start_cook(False)
    assert h.cook is None
    summary: CookSummary = h.summary()
    assert summary.final_phase is CookPhase.FAILED and not summary.stopped_by_user
    # A farm that cannot be reached fails the start too, and later callbacks do nothing.
    h.connect_fault = RuntimeError("no farm")
    with pytest.raises(RuntimeError, match="no farm"):
        h.scheduler.on_start_cook(False)
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.FAILED
    h.scheduler.on_tick()
    assert h.scheduler.on_stop_cook(True)
    assert h.events.count("submit") == 0
    # Neither failed start left a cook open, so the next start connects a fresh one.
    h.connect_fault = None
    del h.events.faults["set_working_dir"]
    assert h.started_fresh()


#: The calls a farm start makes, in order: a stop can arrive during any of them.
START_CALLS: List[str] = ["connect", "set_working_dir", "watch_generation"]


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("stage", START_CALLS)
def test_a_stop_while_a_cook_starts_ends_that_cook(
    stage: str, cancel: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """PDG can stop a cook while its start is still connecting, so the stop must still end it.

    Otherwise a cook stays open after the stop: its journal never records the end,
    the package logger keeps the journal's handler, generation is still watched,
    and the next start is ignored.
    """
    # One whole cook first, so the stop is not the scheduler's first.
    h: Harness = Harness()
    h.start()
    h.scheduler.on_stop_cook(False)
    package: logging.Logger = logging.getLogger(PACKAGE_LOGGER)
    level: int = package.level
    # The stop runs inside the start's call, after recording any cook already open.
    h.events.hooks[stage] = lambda: (h.cook, h.scheduler.on_stop_cook(cancel))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER):
        assert h.scheduler.on_start_cook(False)
    assert h.cook is None
    # Only a cook that began says it started: this one does once it is CONNECTED.
    started: int = sum("started in" in r.getMessage() for r in caplog.records)
    assert started == (1 if stage == "watch_generation" else 0)
    summary: CookSummary = h.journal.summaries[-1]
    assert len(h.journal.summaries) == 2
    # The cook ends in the phase it had reached: CONNECTED only once it began.
    reached: CookPhase = CookPhase.CONNECTED if stage == "watch_generation" else CookPhase.IDLE
    assert summary.stopped_by_user is cancel and summary.final_phase is reached
    assert h.journal.handler not in package.handlers and package.level == level
    # The phase was never written after the stop.
    if stage != "connect":
        last: Cook = h.cooks[-1]
        assert last.phase() is CookPhase.IDLE
        assert h.history(last)[-1][1] is CookPhase.IDLE
    # Generation is not watched once the cook is over.
    assert not h.host.watching
    # An offer finds no cook, and the next start builds a fresh one.
    stopped: List[Cook] = list(h.cooks)
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.FAILED
    assert h.started_fresh() and h.cook not in stopped


def test_a_stop_while_connecting_records_one_end_and_writes_no_phase() -> None:
    """A cook stopped before it was open never began, so nothing about it can run afterwards."""
    # The stop runs while the start connects, then the cook is closed once and never begins.
    h: Harness = Harness()
    h.events.hooks["connect"] = lambda: h.scheduler.on_stop_cook(False)
    assert h.scheduler.on_start_cook(False)
    assert h.events.count("closed") == 1
    assert h.events.count("set_working_dir") == 0 and h.events.count("watch_generation") == 0


def test_a_stop_while_a_generate_starts_lets_the_next_cook_start() -> None:
    """A generate stopped while it starts must not stay open, or every later start is ignored."""
    # The stop runs while the generate sets the working directory, then a farm start follows.
    h: Harness = Harness()
    h.events.hooks["set_working_dir"] = lambda: h.scheduler.on_stop_cook(False)
    h.start(generate_only=True)
    h.start()
    assert h.phase() is CookPhase.CONNECTED


def test_a_start_while_another_start_connects_is_ignored() -> None:
    """Two starts at once must not connect two cooks, or one job would have no cook to drive it."""
    # The second start runs while the first connects.
    h: Harness = Harness()
    h.events.hooks["connect"] = lambda: h.scheduler.on_start_cook(False)
    h.start()
    assert h.events.count("connect") == 1 and h.phase() is CookPhase.CONNECTED


def test_a_failed_start_after_a_stop_closes_the_cook_once() -> None:
    """A start that fails after a stop closed its cook must not close it again."""
    # The stop runs inside set_working_dir, which then raises.
    h: Harness = Harness()
    h.events.hooks["set_working_dir"] = lambda: h.scheduler.on_stop_cook(True)
    h.events.faults["set_working_dir"] = RuntimeError("no such directory")
    with pytest.raises(RuntimeError):
        h.scheduler.on_start_cook(False)
    assert h.events.count("closed") == 1 and h.summary().stopped_by_user


def test_a_start_that_fails_while_a_stop_is_closing_leaves_the_close_to_the_stop() -> None:
    """A failing start must not close a cook that a stop on another thread has taken.

    If it did, the cook would close without the artist's cancel first, and the
    journal would record the artist's stop as not stopped by the user.
    """
    # The stop takes the cook, then waits in stop_watching_generation until released.
    h: Harness = Harness()
    stopping: threading.Event = threading.Event()
    release: threading.Event = threading.Event()

    def hold_the_stop() -> None:
        stopping.set()
        assert release.wait(5)

    h.events.hooks["stop_watching_generation"] = hold_the_stop
    stopper: threading.Thread = threading.Thread(target=lambda: h.scheduler.on_stop_cook(True))

    # set_working_dir starts the stop, waits until it holds the cook, then raises.
    def stop_on_another_thread() -> None:
        stopper.start()
        assert stopping.wait(5)

    h.events.hooks["set_working_dir"] = stop_on_another_thread
    h.events.faults["set_working_dir"] = RuntimeError("no such directory")
    with pytest.raises(RuntimeError):
        h.scheduler.on_start_cook(False)
    # Only now does the stop close the cook, with the artist's cancel.
    release.set()
    stopper.join(5)
    assert not stopper.is_alive()
    assert h.events.count("closed") == 1 and h.summary().stopped_by_user


# -- the package logger --


@pytest.mark.parametrize(
    "before, during",
    [
        (logging.NOTSET, logging.INFO),
        (logging.WARNING, logging.INFO),
        (logging.DEBUG, logging.DEBUG),
    ],
)
def test_the_journal_hears_info_lines_for_the_life_of_the_cook(before: int, during: int) -> None:
    """The journal's log holds the cook's progress lines, and the logger is handed back as found.

    A handler sees only what its logger passes, so at Python's default level the
    journal would get no progress lines. A level the artist set lower is kept.
    """
    # Set the level the artist had, and put it back afterwards.
    package: logging.Logger = logging.getLogger(PACKAGE_LOGGER)
    saved: int = package.level
    package.setLevel(before)
    try:
        # While the cook is open, the journal's handler is on the logger at INFO or lower.
        h: Harness = Harness()
        h.start()
        assert h.journal.handler in package.handlers
        assert package.level == during
        h.scheduler.on_schedule(handle("a_0"))
        h.scheduler.on_stop_cook(True)
        # Closed, the logger is as the cook found it, and the journal heard the INFO lines.
        assert h.journal.handler not in package.handlers
        assert package.level == before
        lines: List[str] = [r.getMessage() for r in h.journal.handler.records]
        assert any(line.startswith("Cooking job ") for line in lines)
        assert any(line.startswith("The cook ended Cooking") for line in lines)
    finally:
        package.setLevel(saved)


def test_the_journal_records_the_cook_for_its_life(tmp_path: Any) -> None:
    """With the real journal, the cook's progress lines reach its log file and the outcome event is sent.

    The fake journal cannot show that the cook's logger setup really feeds the
    journal's file, or that the summary the cook hands over makes a valid event.
    """
    # A real journal under a temporary working directory, sending events to a list.
    sent: List[Tuple[str, Dict[str, Any]]] = []
    journal: CookJournal = CookJournal(
        working_dir=str(tmp_path),
        cook_id="c" * 32,
        started_at=_T0,
        debug_level=0,
        record_event=lambda name, details: sent.append((name, details)),
    )
    package: logging.Logger = logging.getLogger(PACKAGE_LOGGER)
    level: int = package.level
    h: Harness = Harness(journal=journal)
    h.start()
    assert journal.log_handler() in package.handlers
    assert package.getEffectiveLevel() <= logging.INFO
    # One offer submits the job, and the artist's stop ends the cook.
    h.scheduler.on_schedule(handle("a_0"))
    h.scheduler.on_stop_cook(True)
    assert journal.log_handler() not in package.handlers
    assert package.level == level
    (debug_dir,) = (tmp_path / "pdg_debug").iterdir()
    log: str = (debug_dir / "orchestrator.log").read_text()
    assert "Cooking job" in log and "The cook ended Cooking" in log
    ((_, details),) = sent
    assert details["job_submitted"] is True


# -- submission --


def test_the_first_schedule_submits_then_dispatches_in_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first offer snapshots, submits, and starts the job, then registers, uploads, releases, and notifies.

    The payload must be stored before the release, or the worker could read
    nothing, and PDG hears the item started only once it is on its way.
    """
    h: Harness = Harness()
    h.start()
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER):
        assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.SUCCEEDED
    assert "Snapshot: 2 step(s) and 3 task(s). Nodes that are not steps: none." in caplog.text
    ignored: Set[str] = {
        "read_settings",
        "connect",
        "set_working_dir",
        "scene_files",
        "watch_generation",
        "pending_generation",
    }
    assert [n for n in h.events.names() if n not in ignored] == [
        "read_graph",
        "read_scene",
        "snapshot_taken",
        "bundle_dir",
        "submit",
        "open_job",
        "job_submitted",
        "start",
        "item_scheduled",
        "serialize",
        "put_payload",
        "release",
        "notify",
    ]
    assert h.events.details("submit") == ["/tmp/bundle"]
    assert h.events.details("put_payload") == [("task-a-0", '{"name": "a_0"}')]
    assert h.notified() == [("a_0", ItemOutcome(ItemState.STARTED))]
    snapshot: GraphSnapshot = h.submitter.snapshots[0]
    assert snapshot.step_names == ("a", "b") and snapshot.total_tasks == 3
    assert h.history() == (
        (CookPhase.IDLE, CookPhase.CONNECTED),
        (CookPhase.CONNECTED, CookPhase.SNAPSHOTTING),
        (CookPhase.SNAPSHOTTING, CookPhase.BUILDING),
        (CookPhase.BUILDING, CookPhase.SUBMITTING),
        (CookPhase.SUBMITTING, CookPhase.RESOLVING),
        (CookPhase.RESOLVING, CookPhase.COOKING),
    )
    # Later items dispatch without resubmitting.
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.SUCCEEDED
    assert h.events.count("submit") == 1
    assert h.job.released == [TaskKey("a", 0), TaskKey("a", 1)]


def test_the_snapshot_waits_until_pdg_has_generated_every_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An offer while PDG still generates a step is deferred, so the job is not missing its items.

    The wait is logged at most every 10 seconds, because PDG offers items many
    times a second.
    """
    # Three deferrals at 0, 9, and 10 s; the wait is logged at 0 and 10.
    h: Harness = Harness()
    h.start()
    h.host.pending = [("b",)] * 3
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER):
        for seconds in (0, 9, 1):
            h.clock.advance(seconds)
            assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.DEFERRED
        assert h.events.count("read_graph") == 0 and h.phase() is CookPhase.CONNECTED
        h.clock.advance(0.5)
        assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.SUCCEEDED
    waits: List[str] = [
        r.getMessage() for r in caplog.records if "Waiting for PDG" in r.getMessage()
    ]
    assert waits == ["Waiting for PDG to generate b before reading the graph."] * 2
    assert "PDG generated every step after 10.5 s." in caplog.text
    assert h.events.count("read_graph") == 1 and h.events.count("submit") == 1
    assert h.phase() is CookPhase.COOKING


def test_the_snapshot_stops_waiting_five_minutes_after_the_first_deferral(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A step that never finishes generating must not hold the cook for ever.

    After five minutes from the first deferral, not from the start, the snapshot
    goes ahead, and validation names any step still without items. A deferral
    never counts as a failed offer.
    """
    # b never finishes generating, and the first offer comes long after the start.
    h: Harness = Harness()
    h.start()
    h.host.pending = [("b",)] * 100
    h.clock.advance(1000)
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.DEFERRED
    h.clock.advance(299)
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.DEFERRED
    h.clock.advance(1)
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.SUCCEEDED
    assert "PDG has not generated b after 300 s." in caplog.text
    assert h.events.count("submit") == 1


def test_an_unreadable_generation_state_does_not_hold_the_cook(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A host that cannot say whether PDG is generating must not stall the cook, and the console says why."""
    # The first read of the generation state raises.
    h: Harness = Harness()
    h.start()
    h.host.pending = [RuntimeError("no context")]
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.SUCCEEDED
    assert _traceback_logged(caplog, "Could not tell whether PDG is still generating.")


def test_once_generation_has_ended_the_cook_never_waits_again() -> None:
    """After the snapshot, a step PDG reports as generating again does not defer later items."""
    # The host would say b is generating, if asked.
    h: Harness = Harness().cooking()
    h.host.pending = [("b",)]
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.SUCCEEDED
    assert h.events.count("pending_generation") == 1


def test_a_cook_follows_generation_of_its_cook_set_until_it_stops() -> None:
    """The host is told which steps to watch, and stops watching when the cook ends."""
    # PDG asks for a and b.
    h: Harness = Harness()
    assert h.scheduler.on_start_cook(False, frozenset({"a", "b"}))
    assert h.events.details("watch_generation") == [frozenset({"a", "b"})]
    h.scheduler.on_stop_cook(False)
    assert h.events.count("stop_watching_generation") == 1


def test_a_failure_to_watch_generation_is_not_a_failure_of_the_cook(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Watching is a refinement: without it the first item reads the graph at once."""
    # Both watching calls raise.
    h: Harness = Harness()
    h.events.faults["watch_generation"] = RuntimeError("no context")
    h.events.faults["stop_watching_generation"] = RuntimeError("no context")
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        h.start()
    assert _traceback_logged(caplog, "Could not follow PDG's generation of the graph.")
    assert h.phase() is CookPhase.CONNECTED
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.SUCCEEDED
    assert h.scheduler.on_stop_cook(True)
    assert h.summary().final_phase is CookPhase.COOKING


def test_a_generate_follows_no_generation() -> None:
    """A generate submits nothing, so there is no snapshot to wait for."""
    h: Harness = Harness()
    h.start(generate_only=True)
    assert h.events.count("watch_generation") == 0


def test_scene_reads_add_data_edges_to_the_submitted_graph() -> None:
    """A step that reads another step's output must wait for it on the farm, even with no wire between them."""
    # c reads a file a writes, and is wired only to b.
    steps: Steps = (("a", 1, ()), ("b", 1, ()), ("c", 1, ("b",)))
    read: SceneRead = SceneRead(
        step="c",
        node_path="/obj/geo/file1",
        parm="file",
        raw="$PDG_DIR/geo/a/a_0.bgeo",
        path=f"{WORKING_DIR}/geo/a/a_0.bgeo",
    )
    h: Harness = Harness(
        steps, outputs={"a": "__PDG_DIR__/geo/a/a_{i}.bgeo"}, scene=SceneReads(reads=(read,))
    )
    h.cooking()
    snapshot: GraphSnapshot = h.submitter.snapshots[0]
    step: Any = snapshot.step("c")
    assert step.dependencies == ("a", "b")
    (report,) = h.events.details("snapshot_taken")
    assert report is not None and report.data_edges["c"] == frozenset({"a"})


def test_an_unfinished_scene_walk_submits_without_data_edges_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed scene walk must not block the cook, and validation must say its path checks did not run."""
    # The scene walk reports that it did not finish.
    h: Harness = Harness(scene=SceneReads(available=False))
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        h.cooking()
    assert h.events.details("snapshot_taken") == [None]
    assert "The scene walk did not finish" in caplog.text
    assert "the scene's file references (output_outside_cook)" in caplog.text


def test_validation_reads_hip_from_the_environment_before_the_scene(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Validation compares $HIP with the working directory, so it must see the HIP the scene will use.

    The environment's HIP wins, and only without it does the cook ask the host
    for the saved scene.
    """
    monkeypatch.setenv("HIP", "/from/env")
    h: Harness = Harness()
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        h.cooking()
    # Validation normalizes the directory, so on Windows it reports backslashes.
    env_dir: str = os.path.normpath("/from/env")
    assert f"{env_dir} (output_outside_cook): The scene is in '{env_dir}'" in caplog.text
    assert h.events.count("scene_files") == 0
    # Without HIP, the saved scene's directory stands in for it.
    monkeypatch.delenv("HIP")
    caplog.clear()
    saved: Harness = Harness()
    saved.host.hip_file = "/proj/scene.hip"
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        saved.cooking()
    scene_dir: str = os.path.normpath("/proj")
    assert f"{scene_dir} (output_outside_cook): The scene is in '{scene_dir}'" in caplog.text
    # An unsaved scene gives no HIP, and the check has nothing to compare.
    caplog.clear()
    unsaved: Harness = Harness()
    unsaved.host.hip_file = ""
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        unsaved.cooking()
    assert "output_outside_cook" not in caplog.text


@settings(max_examples=20, deadline=None)
@given(found=st.sets(st.sampled_from(sorted(REFUSED_GRAPHS)), min_size=1), later=st.integers(0, 3))
def test_a_refused_graph_uploads_nothing_and_creates_no_job(found: Set[str], later: int) -> None:
    """For any refused graph, nothing reaches the farm, and the error carries validation's verdict.

    The summary records each scenario, and later offers and ticks change nothing.
    """
    # Apply each chosen refusal to the graph, then offer the first item.
    h: Harness = Harness(nodes=refused(*sorted(found)))
    h.start()
    with pytest.raises(GraphRefusedError) as raised:
        h.scheduler.on_schedule(handle("a_0"))
    expected: Set[RefusalScenario] = {REFUSED_GRAPHS[name][1] for name in found}
    assert {r.scenario for r in raised.value.verdict.refusals} == expected
    for i in range(later):
        assert h.scheduler.on_schedule(handle(f"a_{i % 2}")) is ScheduleResult.FAILED
        h.scheduler.on_tick()
    assert h.phase() is CookPhase.REFUSED
    for call in ("bundle_dir", "submit", "open_job", "put_payload", "release", "notify"):
        assert h.events.count(call) == 0, call
    h.scheduler.on_stop_cook(True)
    summary: CookSummary = h.summary()
    assert summary.final_phase is CookPhase.REFUSED
    assert set(summary.refusals) == expected and len(summary.refusals) == len(expected)
    # Nothing was submitted, so the summary counts no steps or tasks.
    assert (summary.step_count, summary.task_count, summary.job_id) == (0, 0, "")
    assert h.events.count("cancel") == 0


def test_with_validation_off_a_refused_graph_is_submitted_and_each_refusal_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An artist who turns validation off still sees what each check found, as a warning."""
    # Two refusals, with validation turned off.
    h: Harness = Harness(nodes=refused("dynamic", "other scheduler"), validate_graph=False)
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        h.cooking()
    assert h.events.count("submit") == 1
    assert "a (dynamic_node): Not refused, because graph validation is off." in caplog.text
    assert "b (multiple_schedulers): Not refused" in caplog.text
    assert "Refused," not in caplog.text
    h.scheduler.on_stop_cook(False)
    assert h.summary().refusals == ()


def test_a_refusal_is_logged_with_its_remedy(caplog: pytest.LogCaptureFixture) -> None:
    """The console must say what to change, not only that the cook was refused."""
    # A graph with one dynamic node.
    h: Harness = Harness(nodes=refused("dynamic"))
    h.start()
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        with pytest.raises(GraphRefusedError) as raised:
            h.scheduler.on_schedule(handle("a_0"))
    (found,) = raised.value.verdict.refusals
    assert f"Refused, a (dynamic_node): {found.detail} {found.remedy}" in caplog.text


def test_a_failed_submission_fails_the_cook_and_shows_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The artist sees the service's reason, and nothing is uploaded or canceled, since no job exists."""
    # CreateJob is denied.
    h: Harness = Harness()
    h.events.faults["submit"] = JobSubmissionError("Access to the queue was denied.")
    h.start()
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        with pytest.raises(JobSubmissionError) as raised:
            h.scheduler.on_schedule(handle("a_0"))
    assert "denied" in artist_message(raised.value)
    assert _traceback_logged(caplog, "The cook could not start its job.")
    assert h.phase() is CookPhase.FAILED
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.FAILED
    h.scheduler.on_stop_cook(True)
    assert h.events.count("cancel") == 0 and h.events.count("put_payload") == 0


def test_a_quota_refusal_from_the_service_is_a_refusal(caplog: pytest.LogCaptureFixture) -> None:
    """CreateJob refusing the job for a quota creates nothing, so the cook is REFUSED, not FAILED."""
    # CreateJob is refused for a quota.
    h: Harness = Harness()
    h.events.faults["submit"] = QuotaExceededError("tasks per job")
    h.start()
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        with pytest.raises(QuotaExceededError):
            h.scheduler.on_schedule(handle("a_0"))
    assert _traceback_logged(caplog, "The job was refused by a service quota.")
    assert h.phase() is CookPhase.REFUSED
    h.scheduler.on_stop_cook(False)
    assert h.summary().refusals == (RefusalScenario.QUOTA_EXCEEDED,)


def test_a_failed_start_of_the_job_fails_it_exactly_once() -> None:
    """A created job whose tasks cannot be resolved would wait suspended for ever, so it is failed once.

    Failed, not canceled: a canceled job reads on the farm as one the artist stopped.
    """
    # The job is created, then its tasks cannot be resolved.
    h: Harness = Harness()
    h.events.faults["start"] = TaskMapIncompleteError("no step b")
    h.start()
    with pytest.raises(TaskMapIncompleteError):
        h.scheduler.on_schedule(handle("a_0"))
    assert h.phase() is CookPhase.FAILED
    assert h.events.details("fail") == [JOB_ID] and h.events.count("cancel") == 0
    h.scheduler.on_stop_cook(True)
    assert (h.job.fails, h.job.cancels) == (1, 0)
    assert h.summary().job_id == JOB_ID


def test_a_job_that_cannot_be_opened_is_named_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """Without the job's handle the cook cannot end the job, so the artist must learn which job to cancel."""
    # CreateJob returns, then opening the job raises.
    h: Harness = Harness()
    h.events.faults["open_job"] = RuntimeError("bad handle")
    h.start()
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        with pytest.raises(RuntimeError, match="bad handle"):
            h.scheduler.on_schedule(handle("a_0"))
    assert f"Job {JOB_ID} was created, but the cook could not open it." in caplog.text
    assert h.phase() is CookPhase.FAILED


#: The calls the submission makes, grouped by the phase they run in. Each group
#: ends at a compare-and-set, so a stop during a group lets only the rest of it run.
STAGE_GROUPS: List[List[str]] = [
    ["read_graph", "read_scene", "snapshot_taken"],
    ["bundle_dir"],
    ["submit", "open_job"],
    ["job_submitted", "start"],
]
STAGES: List[str] = [name for group in STAGE_GROUPS for name in group]


def stages_run_after_a_stop_in(stage: str) -> List[str]:
    """Every submission call made when the cook stops during ``stage``."""
    (group,) = [g for g in STAGE_GROUPS if stage in g]
    return STAGES[: STAGES.index(group[-1]) + 1]


@settings(max_examples=40, deadline=None)
@given(stage=st.sampled_from(STAGES), cancel=st.booleans())
def test_a_stop_during_submission_leaves_no_job_running_and_writes_no_phase(
    stage: str, cancel: bool
) -> None:
    """For a stop at any stage of submission, no phase follows IDLE and a created job is not left waiting.

    A job returned after the stop is canceled once and not recorded. One recorded
    before the stop is canceled when the stop asked for it, and otherwise marked
    failed, because a cook that never reached COOKING released none of its tasks.
    """
    h: Harness = Harness()
    h.start()
    cook: Optional[Cook] = h.cook
    assert cook is not None
    h.events.hooks[stage] = lambda: h.scheduler.on_stop_cook(cancel)
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.FAILED
    history: Tuple[Tuple[CookPhase, CookPhase], ...] = h.history(cook)
    assert history[-1][1] is CookPhase.IDLE and cook.phase() is CookPhase.IDLE
    # Nothing past the next compare-and-set runs once the cook stopped.
    assert [n for n in h.events.names() if n in STAGES] == stages_run_after_a_stop_in(stage)
    # A created job gets exactly one call that ends it.
    recorded: bool = stage in ("job_submitted", "start")
    ended_by: Tuple[int, int] = (
        ((0, 1) if (recorded and not cancel) else (1, 0)) if h.events.count("open_job") else (0, 0)
    )
    assert (h.events.count("cancel"), h.events.count("fail")) == ended_by
    assert h.events.count("put_payload") == 0 and h.events.count("release") == 0
    assert h.summary().job_id == (JOB_ID if recorded else "")
    # The next cook starts clean.
    h.start()
    assert h.cook is not cook and h.phase() is CookPhase.CONNECTED


@pytest.mark.parametrize("stage", ["read_graph", "read_scene", "submit", "open_job", "start"])
def test_a_stage_that_fails_after_a_stop_writes_no_phase_and_raises_nothing(
    stage: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure after the cook stopped belongs to no open cook, so it records nothing.

    PDG has already moved on, so raising would report an error for a cook that
    is over, and recording FAILED would write a phase after IDLE. The failure is
    still logged with its traceback, so it is not lost.
    """
    # The stop runs inside the call, and the same call then raises.
    h: Harness = Harness()
    h.start()
    h.events.hooks[stage] = lambda: h.scheduler.on_stop_cook(True)
    h.events.faults[stage] = RuntimeError(stage)
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.FAILED
    assert h.history()[-1] == (h.history()[-2][1], CookPhase.IDLE)
    assert CookPhase.FAILED not in {after for _, after in h.history()}
    assert h.events.count("put_payload") == 0 and h.events.count("release") == 0
    assert _traceback_logged(
        caplog, "The cook stopped while it was submitting, and the submission then failed."
    )


@pytest.mark.parametrize("start_raises", [False, True])
@pytest.mark.parametrize("stage", ["job_submitted", "start"])
def test_pdg_ending_a_cook_while_its_job_starts_fails_the_job_once(
    stage: str, start_raises: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A job whose cook ended before COOKING has no one to release its tasks, so it is marked failed.

    Left alone, its dependent steps stay suspended and the job stays open on the
    farm with no error. A start that then raises must not end the job a second time.
    """
    # PDG ends the cook while the job is recorded and being started.
    h: Harness = Harness()
    h.start()
    h.events.hooks[stage] = lambda: h.scheduler.on_stop_cook(False)
    if start_raises:
        h.events.faults["start"] = TaskMapIncompleteError("no step b")
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.FAILED
    assert (h.job.fails, h.job.cancels) == (1, 0)
    summary: CookSummary = h.summary()
    assert summary.final_phase is CookPhase.FAILED and not summary.stopped_by_user
    assert summary.job_id == JOB_ID
    assert f"The cook ended while job {JOB_ID} was being started" in caplog.text


def test_a_stop_before_a_refusal_raises_nothing() -> None:
    """A refusal found after the cook stopped is not shown: the cook is over, and nothing was submitted."""
    # The stop comes while the refused graph is recorded, before validation.
    h: Harness = Harness(nodes=refused("dynamic"))
    h.start()
    h.events.hooks["snapshot_taken"] = lambda: h.scheduler.on_stop_cook(False)
    assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.FAILED
    summary: CookSummary = h.summary()
    assert summary.final_phase is CookPhase.SNAPSHOTTING and summary.refusals == ()


def test_a_quota_error_after_the_job_exists_fails_the_cook_and_the_job() -> None:
    """Only CreateJob's quota error means nothing was created. Later, the job exists and must not wait suspended."""
    # Resolving the created job raises a quota error.
    h: Harness = Harness()
    h.events.faults["start"] = QuotaExceededError("requests per second")
    h.start()
    with pytest.raises(QuotaExceededError):
        h.scheduler.on_schedule(handle("a_0"))
    assert h.phase() is CookPhase.FAILED
    assert (h.job.fails, h.job.cancels) == (1, 0)
    h.scheduler.on_stop_cook(False)
    assert h.summary().refusals == () and (h.job.fails, h.job.cancels) == (1, 0)


def test_a_stop_while_creating_the_job_does_not_wait_for_it() -> None:
    """No lock is held over CreateJob, so the artist's stop returns while the call is still out.

    The job the call returns afterwards is canceled.
    """
    # CreateJob blocks until the test lets it return.
    h: Harness = Harness()
    h.start()
    cook: Optional[Cook] = h.cook
    assert cook is not None
    entered: threading.Event = threading.Event()
    release: threading.Event = threading.Event()

    def blocking_submit(snapshot: GraphSnapshot, bundle_dir: str) -> str:
        entered.set()
        assert release.wait(5)
        return JOB_ID

    h.submitter.submit = blocking_submit  # type: ignore[method-assign]
    # Offer on one thread, stop on another while the offer waits on the network.
    results: List[ScheduleResult] = []
    worker: threading.Thread = threading.Thread(
        target=lambda: results.append(h.scheduler.on_schedule(handle("a_0")))
    )
    worker.start()
    assert entered.wait(5)
    stopper: threading.Thread = threading.Thread(target=lambda: h.scheduler.on_stop_cook(True))
    stopper.start()
    stopper.join(2)
    assert not stopper.is_alive(), "the stop waited on the network call"
    h.scheduler.on_tick()
    release.set()
    worker.join(5)
    assert results == [ScheduleResult.FAILED]
    assert h.events.details("cancel") == [JOB_ID]
    assert h.history(cook)[-1][1] is CookPhase.IDLE


# -- dispatch --


def test_an_item_the_snapshot_lacks_fails_and_is_never_released() -> None:
    """An item PDG generated after the snapshot has no task, so it fails instead of reaching the farm."""
    h: Harness = Harness().cooking()
    assert h.scheduler.on_schedule(handle("zzz_0")) is ScheduleResult.FAILED
    assert h.job.released == [TaskKey("a", 0)]


def test_a_failed_upload_fails_the_item_and_its_later_result_is_ignored() -> None:
    """A task without its payload must not run, and a result the farm reports for it anyway is dropped."""
    # The upload for a_1 fails, then the farm reports a_1 anyway.
    h: Harness = Harness().cooking()
    h.events.faults["put_payload"] = PayloadTransferError("s3 down")
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.FAILED
    assert h.job.released == [TaskKey("a", 0)]
    h.job.polls.append([TaskOutcome(TaskKey("a", 1), TaskStatus.SUCCEEDED)])
    h.scheduler.on_tick()
    assert h.results() == {}
    h.scheduler.on_stop_cook(True)
    assert h.summary().failed == 1


def test_a_failed_serialization_fails_the_item() -> None:
    """A payload PDG cannot serialize is a failed upload: the task is never released."""
    # Serializing fails before anything is stored.
    h: Harness = Harness().cooking()
    h.events.faults["serialize"] = ValueError("bad")
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.FAILED
    assert TaskKey("a", 1) not in h.job.released


def test_an_item_offered_twice_is_dispatched_once(caplog: pytest.LogCaptureFixture) -> None:
    """A second offer of the same item must not release its task again, and the console says why."""
    # a_0 was offered when the cook started; PDG offers it again.
    h: Harness = Harness().cooking()
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        assert h.scheduler.on_schedule(handle("a_0")) is ScheduleResult.FAILED
    assert h.job.released == [TaskKey("a", 0)]
    assert "Work item 'a_0' was offered twice." in caplog.text


def test_a_failed_start_notification_still_dispatches(caplog: pytest.LogCaptureFixture) -> None:
    """The task is already released, so a PDG error while hearing it started must not fail the item."""
    # PDG raises while hearing that a_1 started.
    h: Harness = Harness().cooking()
    h.events.faults["notify"] = RuntimeError("pdg busy")
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.SUCCEEDED
    assert "Could not tell PDG that work item 'a_1' started" in caplog.text


def _fail_the_job(h: Harness) -> None:
    """Tick once with the job reading FAILED, from inside whatever call runs this."""
    h.job.statuses.append(JobStatus("UPDATE_SUCCEEDED", "FAILED"))
    h.scheduler.on_tick()


def test_an_item_offered_after_the_job_failed_fails_without_starting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An offer that races the job's failure must still get a result.

    Here the job fails after the offer found the cook COOKING but before the
    offer was recorded. Recorded then, the item would hear STARTED and never a
    result, since the job's failure has already failed every item it knew.
    """
    # The tick that fails the job runs inside the offer's clock read.
    h: Harness = Harness().cooking()
    h.clock.hook = lambda: _fail_the_job(h)
    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.FAILED
    # It is not PDG's mistake, so it is not logged as one.
    assert "offered twice" not in caplog.text
    assert h.notified() == [
        ("a_0", ItemOutcome(ItemState.STARTED)),
        ("a_0", ItemOutcome(ItemState.FAILED)),
    ]
    assert TaskKey("a", 1) not in h.job.released and h.events.details("serialize") == ["a_0"]


def test_a_job_failure_during_the_upload_reaches_pdg_after_started() -> None:
    """PDG must hear that an item started before it hears that it failed.

    The job fails while the item's payload is being stored. PDG hears STARTED
    once the task is released, and then the failure, as onSchedule's answer.
    """
    # The tick that fails the job runs inside the upload.
    h: Harness = Harness().cooking()
    h.events.hooks["put_payload"] = lambda: _fail_the_job(h)
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.FAILED
    assert [o for name, o in h.notified() if name == "a_1"] == [ItemOutcome(ItemState.STARTED)]
    h.scheduler.on_stop_cook(False)
    assert (h.summary().succeeded, h.summary().failed) == (0, 2)


def test_a_result_that_comes_before_started_is_reported_after_it() -> None:
    """A task can finish on the farm before PDG is told its item started, and PDG must still hear STARTED first."""
    # The farm's result for a_1 is polled while a_1 is being released.
    h: Harness = Harness().cooking()
    h.job.polls.append([TaskOutcome(TaskKey("a", 1), TaskStatus.SUCCEEDED)])
    h.events.hooks["release"] = lambda: h.scheduler.on_tick()
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.SUCCEEDED
    h.clock.advance(POLL_SECONDS)
    h.tick()
    assert [o.state for name, o in h.notified() if name == "a_1"] == [
        ItemState.STARTED,
        ItemState.SUCCEEDED,
    ]
    h.scheduler.on_stop_cook(True)
    assert h.summary().succeeded == 1


def test_a_held_result_survives_a_poll_that_raises() -> None:
    """A result that waited for its item's STARTED is still reported when the next poll raises.

    Taking the held result off the tracker before a poll that raised would lose
    it, and PDG would wait on the item for ever.
    """
    # The farm's result for a_1 is polled while a_1 is being released, so it waits.
    h: Harness = Harness().cooking()
    h.job.polls.append([TaskOutcome(TaskKey("a", 1), TaskStatus.SUCCEEDED)])
    h.events.hooks["release"] = lambda: h.scheduler.on_tick()
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.SUCCEEDED
    # The next tick that polls has its poll raise once. The tick after it polls normally.
    h.clock.advance(POLL_SECONDS)
    h.events.fail_once["poll"] = RuntimeError("search failed")
    h.tick()
    h.tick()
    assert [o.state for name, o in h.notified() if name == "a_1"] == [
        ItemState.STARTED,
        ItemState.SUCCEEDED,
    ]
    h.scheduler.on_stop_cook(True)
    assert h.summary().succeeded == 1


def test_no_cook_lock_is_held_over_the_upload() -> None:
    """A slow upload must not stall PDG's other threads: a tick, a phase read, and a stop all return."""
    h: Harness = Harness().cooking()
    returned: List[str] = []

    def call_from_another_thread() -> None:
        # Each call runs on its own thread and must return within a second.
        cook: Optional[Cook] = h.cook
        assert cook is not None
        calls: List[Tuple[str, Callable[[], Any]]] = [
            ("tick", h.scheduler.on_tick),
            ("phase", cook.phase),
            ("stop", lambda: h.scheduler.on_stop_cook(True)),
        ]
        for name, call in calls:
            thread: threading.Thread = threading.Thread(target=call)
            thread.start()
            thread.join(1)
            if not thread.is_alive():
                returned.append(name)

    h.events.hooks["put_payload"] = call_from_another_thread
    h.scheduler.on_schedule(handle("a_1"))
    assert returned == ["tick", "phase", "stop"]


def test_scheduling_outside_cooking_fails_without_touching_state() -> None:
    """Once the cook failed, an offer fails without calling anything or changing a count."""
    # Fail the cook through a dead job, then offer an item.
    h: Harness = Harness().cooking()
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.job.statuses.append(JobStatus("UPDATE_SUCCEEDED", "FAILED"))
    h.scheduler.on_tick()
    assert h.phase() is CookPhase.FAILED
    before: List[Tuple[str, Any]] = list(h.events.log)
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.FAILED
    assert h.events.log == before
    h.scheduler.on_stop_cook(False)
    summary: CookSummary = h.summary()
    assert (summary.succeeded, summary.failed) == (1, 0)


# -- reporting --


def test_declared_outputs_are_reported_with_no_storage_call() -> None:
    """Outputs PDG already knows are reported as local paths, with the time since the offer."""
    # a_0 succeeds 12 seconds after its offer.
    h: Harness = Harness(outputs={"a": "__PDG_DIR__/geo/a_{i}.bgeo"}).cooking()
    h.clock.advance(12)
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.scheduler.on_tick()
    ((name, outcome),) = h.notified(ItemState.SUCCEEDED)
    assert name == "a_0"
    assert outcome.outputs == (OutputFile(f"{WORKING_DIR}/geo/a_0.bgeo", "file/geo"),)
    assert outcome.duration_seconds == 12
    assert h.events.count("get_outputs") == 0


def test_a_clock_that_moves_back_reports_no_negative_duration() -> None:
    """PDG shows the duration, so a clock step backwards must not show a negative one."""
    # The clock steps back 5 seconds before a_0 succeeds.
    h: Harness = Harness().cooking()
    h.clock.advance(-5)
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.scheduler.on_tick()
    ((_, outcome),) = h.notified(ItemState.SUCCEEDED)
    assert outcome.duration_seconds == 0.0


def test_a_naive_clock_reads_as_utc() -> None:
    """A clock without a time zone is read as UTC, so a duration against an aware time still works."""
    # The offer used an aware time; the result arrives on a naive one.
    h: Harness = Harness().cooking()
    h.clock.at = datetime(2026, 1, 1, 0, 0, 7)
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.scheduler.on_tick()
    ((_, outcome),) = h.notified(ItemState.SUCCEEDED)
    assert outcome.duration_seconds == 7


def test_outputs_declared_at_schedule_time_win_over_the_snapshot() -> None:
    """Some nodes declare their outputs only when PDG offers the item, so those are the ones reported."""
    # The offer declares a file the snapshot did not have.
    h: Harness = Harness(outputs={"a": "__PDG_DIR__/geo/a_{i}.bgeo"})
    h.start()
    late: Tuple[DeclaredFile, ...] = (DeclaredFile("__PDG_DIR__/late/a_0.exr", "file/image"),)
    h.scheduler.on_schedule(handle("a_0", late))
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.scheduler.on_tick()
    ((_, outcome),) = h.notified(ItemState.SUCCEEDED)
    assert outcome.outputs == (OutputFile(f"{WORKING_DIR}/late/a_0.exr", "file/image"),)


@pytest.mark.parametrize(
    "record, expected",
    [
        (
            [OutputFile("__PDG_DIR__/out/x.bgeo", "")],
            (OutputFile(f"{WORKING_DIR}/out/x.bgeo", ""),),
        ),
        (None, ()),
        (OSError("unreadable"), ()),
    ],
)
def test_an_item_that_declares_nothing_reports_what_its_task_recorded(
    record: Any, expected: Tuple[OutputFile, ...]
) -> None:
    """Without declared outputs the task's own record is read, and an unreadable one reports none.

    The item still succeeds, because its task did.
    """
    # a_0 declares nothing, so its task's record is read.
    h: Harness = Harness().cooking()
    h.storage.outputs["task-a-0"] = record
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.scheduler.on_tick()
    assert h.events.details("get_outputs") == ["task-a-0"]
    ((_, outcome),) = h.notified(ItemState.SUCCEEDED)
    assert outcome.outputs == expected


def test_a_failure_logs_the_worker_tail_and_fails_the_item() -> None:
    """A failed task fails its item, and the journal gets the worker's last 50 lines."""
    # The farm reports a_0 failed.
    h: Harness = Harness().cooking()
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.FAILED)])
    h.scheduler.on_tick()
    assert h.events.details("log_tail") == [(TaskKey("a", 0), 50)]
    assert h.events.details("item_failed") == [(TaskKey("a", 0), ["last words"])]
    assert h.results() == {"a_0": [ItemState.FAILED]}


def test_a_success_pdg_cannot_be_told_is_reported_failed_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a result PDG would wait on the item for ever, so a success it rejects becomes one failure.

    The success is never retried, because a retry could add the item's outputs
    twice. The other results of the same poll still reach PDG.
    """
    # PDG rejects a_0's success once; a_1 fails on the farm in the same poll.
    h: Harness = Harness().cooking()
    h.scheduler.on_schedule(handle("a_1"))
    h.events.fail_once["notify"] = RuntimeError("pdg busy")
    h.job.polls.append(
        [
            TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED),
            TaskOutcome(TaskKey("a", 1), TaskStatus.FAILED),
        ]
    )
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        h.tick()
    assert [o.state for name, o in h.notified() if name == "a_0"] == [
        ItemState.STARTED,
        ItemState.SUCCEEDED,
        ItemState.FAILED,
    ]
    assert h.results()["a_1"] == [ItemState.FAILED]
    assert _traceback_logged(
        caplog, "Could not tell PDG that work item 'a_0' succeeded. Reporting it failed"
    )
    # The same success polled again is not reported again, and the item counts as failed.
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.tick()
    assert h.results()["a_0"] == [ItemState.SUCCEEDED, ItemState.FAILED]
    h.scheduler.on_stop_cook(True)
    assert (h.summary().succeeded, h.summary().failed) == (0, 2)


def test_a_result_pdg_rejects_does_not_drop_the_rest_of_the_poll() -> None:
    """One result PDG cannot be told must not stop the poll's other results from reaching it."""
    # Every notification after the first raises, until a_1's log tail is read.
    h: Harness = Harness().cooking()
    h.scheduler.on_schedule(handle("a_1"))
    h.events.faults["notify"] = RuntimeError("pdg busy")
    h.events.hooks["log_tail"] = lambda: h.events.faults.pop("notify")
    h.job.polls.append(
        [
            TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED),
            TaskOutcome(TaskKey("a", 1), TaskStatus.FAILED),
        ]
    )
    h.tick()
    assert h.results() == {
        "a_0": [ItemState.SUCCEEDED, ItemState.FAILED],
        "a_1": [ItemState.FAILED],
    }


def test_a_cancellation_is_counted_once_and_never_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A canceled task is no verdict on its item, so PDG hears nothing, and the summary counts it once."""
    # a_0's cancel comes back three times, b_0's once.
    h: Harness = Harness().cooking()
    canceled: TaskOutcome = TaskOutcome(TaskKey("a", 0), TaskStatus.CANCELED)
    h.job.polls += [
        [canceled, canceled],
        [canceled, TaskOutcome(TaskKey("b", 0), TaskStatus.CANCELED)],
    ]
    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        h.tick()
        h.tick()
    assert h.results() == {}
    canceled_lines: List[str] = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Task ")
    ]
    assert canceled_lines == ["Task a[0] was canceled.", "Task b[0] was canceled."]
    assert all(
        r.levelno == logging.DEBUG for r in caplog.records if r.getMessage().startswith("Task ")
    )
    h.scheduler.on_stop_cook(False)
    assert h.summary().canceled == 2


def test_a_canceled_task_that_then_fails_with_the_job_counts_only_as_failed() -> None:
    """Each item has one result in the summary: a canceled task whose item failed with the job is a failure."""
    # a_0's task is canceled, then the job fails with a_0 and b_0 outstanding.
    h: Harness = Harness(PAIR).cooking()
    h.scheduler.on_schedule(handle("b_0"))
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.CANCELED)])
    h.job.statuses.append(JobStatus("UPDATE_SUCCEEDED", "FAILED"))
    h.tick()
    h.scheduler.on_stop_cook(False)
    summary: CookSummary = h.summary()
    assert (summary.failed, summary.canceled) == (2, 0)


def test_ticks_poll_the_farm_at_most_every_five_seconds() -> None:
    """Each poll searches the job's tasks and each status read calls GetJob, so a fast tick must not repeat them."""
    # The first tick polls; ticks within the next 5 s do not.
    h: Harness = Harness().cooking()
    h.scheduler.on_tick()
    for _ in range(4):
        h.clock.advance(1.2)
        h.scheduler.on_tick()
    assert (h.events.count("poll"), h.events.count("status")) == (1, 1)
    h.clock.advance(0.2)
    h.scheduler.on_tick()
    assert (h.events.count("poll"), h.events.count("status")) == (2, 2)


def test_a_stop_during_a_poll_reports_nothing_after_it() -> None:
    """PDG must hear no result after the cook closed, and the summary must count exactly what PDG heard.

    The stop comes while the tick waits on the farm. The results that poll
    returns arrive after the close, so none of them is recorded or reported.
    """
    # The artist stops while the farm returns a_0's and a_1's results.
    h: Harness = Harness().cooking()
    h.scheduler.on_schedule(handle("a_1"))
    h.job.polls.append(
        [
            TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED),
            TaskOutcome(TaskKey("a", 1), TaskStatus.FAILED),
        ]
    )
    h.events.hooks["poll"] = lambda: h.scheduler.on_stop_cook(True)
    h.tick()
    assert h.results() == {}
    assert h.events.count("get_outputs") == 0 and h.events.count("item_failed") == 0
    summary: CookSummary = h.summary()
    assert (summary.succeeded, summary.failed) == (0, 0)


def test_a_result_for_an_item_not_yet_offered_waits_for_it() -> None:
    """A result for an item PDG has not offered claims nothing, so the item can still report once offered."""
    # a_1's result arrives before PDG offers a_1, then again after.
    h: Harness = Harness().cooking()
    h.job.polls.append([TaskOutcome(TaskKey("a", 1), TaskStatus.SUCCEEDED)])
    h.tick()
    assert h.results() == {}
    h.scheduler.on_schedule(handle("a_1"))
    h.job.polls.append([TaskOutcome(TaskKey("a", 1), TaskStatus.SUCCEEDED)])
    h.tick()
    assert h.results() == {"a_1": [ItemState.SUCCEEDED]}


@pytest.mark.parametrize(
    "status",
    [
        JobStatus("UPDATE_SUCCEEDED", "FAILED"),
        JobStatus("UPDATE_SUCCEEDED", "CANCELED"),
        JobStatus("CREATE_FAILED", "READY"),
        JobStatus("UPLOAD_FAILED", "READY"),
        JobStatus("UPDATE_FAILED", "READY"),
        JobStatus("ARCHIVED", ""),
    ],
)
def test_a_dead_job_fails_every_outstanding_item_once(status: JobStatus) -> None:
    """A job that can make no more progress fails each item still waiting, once, and the cook fails."""
    # a_0 succeeds in the tick that finds the job dead; a_1 is still out.
    h: Harness = Harness().cooking()
    h.scheduler.on_schedule(handle("a_1"))
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.job.statuses.append(status)
    h.tick()
    h.tick()
    assert h.results() == {"a_0": [ItemState.SUCCEEDED], "a_1": [ItemState.FAILED]}
    assert h.phase() is CookPhase.FAILED


@pytest.mark.parametrize(
    "status, shown",
    [
        (
            JobStatus("UPDATE_SUCCEEDED", "FAILED", {"FAILED": 1, "SUCCEEDED": 2, "READY": 0}),
            "(lifecycle UPDATE_SUCCEEDED, tasks FAILED: FAILED=1, SUCCEEDED=2). Failing 1 ",
        ),
        (JobStatus("ARCHIVED", ""), "(lifecycle ARCHIVED, tasks ?). Failing 1 "),
        (JobStatus("", "CANCELED"), "(lifecycle ?, tasks CANCELED). Failing 1 "),
    ],
)
def test_a_dead_job_is_logged_with_its_status_and_counts(
    status: JobStatus, shown: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The console must show why the cook gave up: the job's statuses, its non-zero counts, and what failed."""
    # a_0 is the one item still waiting when the job dies.
    h: Harness = Harness().cooking()
    h.job.statuses.append(status)
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        h.scheduler.on_tick()
    assert f"The job can make no more progress {shown}outstanding work item(s)." in caplog.text


@pytest.mark.parametrize(
    "status",
    [JobStatus(), JobStatus("UPDATE_SUCCEEDED", "READY"), JobStatus("CREATE_COMPLETE", "RUNNING")],
)
def test_a_live_or_unreadable_job_status_does_not_end_the_cook(status: JobStatus) -> None:
    """A failed status read, or a job still running, must not abandon the cook."""
    # The only status the job gives is this one.
    h: Harness = Harness().cooking()
    h.job.statuses.append(status)
    h.scheduler.on_tick()
    assert h.phase() is CookPhase.COOKING
    assert h.results() == {}


def test_a_dead_job_still_fails_the_items_pdg_cannot_be_told_about() -> None:
    """One item PDG rejects must not stop the others from failing."""
    # Once the job reads dead, every notification to PDG raises.
    h: Harness = Harness().cooking()
    h.scheduler.on_schedule(handle("a_1"))
    h.events.hooks["status"] = lambda: h.events.faults.update(notify=RuntimeError("pdg busy"))
    h.job.statuses.append(JobStatus("UPDATE_SUCCEEDED", "FAILED"))
    h.scheduler.on_tick()
    failed: List[str] = [name for name, _ in h.notified(ItemState.FAILED)]
    assert sorted(failed) == ["a_0", "a_1"]


def test_a_cook_drains_when_every_item_reports_and_is_done_when_the_job_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DRAINING follows the last report, and DONE follows the job's success, with no more polling between."""
    h: Harness = Harness().cooking()
    h.scheduler.on_schedule(handle("a_1"))
    h.job.polls.append(
        [
            TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED),
            TaskOutcome(TaskKey("a", 1), TaskStatus.SUCCEEDED),
        ]
    )
    h.scheduler.on_tick()
    assert h.phase() is CookPhase.COOKING
    h.scheduler.on_schedule(handle("b_0"))
    h.job.polls.append([TaskOutcome(TaskKey("b", 0), TaskStatus.SUCCEEDED)])
    h.clock.advance(POLL_SECONDS)
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER):
        h.tick()
    assert h.phase() is CookPhase.DRAINING
    # The tick that drains says so, and only that tick.
    drained: List[str] = [r.getMessage() for r in caplog.records]
    assert drained.count("All 3 work item(s) reported: 3 succeeded, 0 failed.") == 1
    assert "The job succeeded." not in drained
    caplog.clear()
    # In DRAINING only the job's status is read, and its success ends the cook.
    polls: int = h.events.count("poll")
    h.job.statuses.append(JobStatus("UPDATE_SUCCEEDED", "SUCCEEDED"))
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER):
        h.tick()
        h.tick()
    assert h.phase() is CookPhase.DONE
    assert h.events.count("poll") == polls
    done: List[str] = [r.getMessage() for r in caplog.records]
    assert done.count("The job succeeded.") == 1
    assert not any(m.startswith("All ") for m in done)
    h.scheduler.on_stop_cook(False)
    summary: CookSummary = h.summary()
    assert (summary.final_phase, summary.succeeded, summary.task_count) == (CookPhase.DONE, 3, 3)
    assert (summary.step_count, summary.job_id) == (2, JOB_ID)


def test_a_job_that_succeeds_before_every_item_reports_is_not_done() -> None:
    """DONE needs both every report and the job's success, so PDG hears every result first."""
    # The job reads SUCCEEDED while a_1 and b_0 were never reported.
    h: Harness = Harness().cooking()
    h.job.statuses.append(JobStatus("UPDATE_SUCCEEDED", "SUCCEEDED"))
    h.scheduler.on_tick()
    assert h.phase() is CookPhase.COOKING


@settings(max_examples=30, deadline=None)
@given(broken=st.sets(st.sampled_from(["poll", "status", "notify", "get_outputs"]), min_size=1))
def test_a_failing_tick_part_does_not_stop_the_others(broken: Set[str]) -> None:
    """For any parts of a tick made to raise, the tick returns, the other parts run, and all run next tick."""
    h: Harness = Harness().cooking()
    h.scheduler.on_schedule(handle("a_1"))
    for name in broken:
        h.events.faults[name] = RuntimeError(name)
    h.job.polls.append(
        [
            TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED),
            TaskOutcome(TaskKey("a", 1), TaskStatus.FAILED),
        ]
    )
    h.tick()
    assert h.events.count("status") == 1
    if "poll" not in broken:
        # Both outcomes were handled, whichever part of reporting one of them broke.
        assert h.events.count("notify") >= 4 - (1 if "notify" in broken else 0)
    for name in broken:
        del h.events.faults[name]
    h.tick()
    assert h.events.count("status") == 2 and h.events.count("poll") == 2


def test_the_heartbeat_is_logged_at_most_every_thirty_seconds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The heartbeat shows a long cook is alive, without a line for every tick.

    It counts the offered items still waiting for a result apart from those
    already reported.
    """
    # a_0 and a_1 report in the first tick; b_0 stays out. Ticks every 5 s: beats at 5 and 35 s.
    h: Harness = Harness().cooking()
    h.scheduler.on_schedule(handle("a_1"))
    h.scheduler.on_schedule(handle("b_0"))
    h.job.polls.append(
        [
            TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED),
            TaskOutcome(TaskKey("a", 1), TaskStatus.FAILED),
        ]
    )
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER):
        for _ in range(7):
            h.clock.advance(5)
            h.scheduler.on_tick()
    beats: List[str] = [
        r.getMessage() for r in caplog.records if "waiting for a result" in r.getMessage()
    ]
    assert (
        beats
        == [
            "Cooking: 2 of 3 work item(s) reported (1 succeeded, 1 failed), 1 offered and "
            "waiting for a result, 0 canceled."
        ]
        * 2
    )


# -- closing --


def test_closing_with_cancel_cancels_the_job_once() -> None:
    """The artist's stop cancels exactly the cook's job, once, however often the cook is closed."""
    # The cook is closed by the stop, then again directly, then stopped again.
    h: Harness = Harness().cooking()
    cook: Optional[Cook] = h.cook
    assert h.scheduler.on_stop_cook(True)
    assert h.events.count("cancel") == 1
    assert cook is not None
    closed_again: CookSummary = cook.close(cancel=True)
    assert closed_again is h.summary()
    assert h.events.count("cancel") == 1
    summary: CookSummary = h.summary()
    assert summary.stopped_by_user and summary.final_phase is CookPhase.COOKING
    assert h.scheduler.on_stop_cook(True)
    assert h.events.count("cancel") == 1


def test_closing_a_cook_returns_the_summary_the_journal_records() -> None:
    """The cook's own close is what a caller reads the outcome from, so it must be the recorded summary."""
    # Close the cook directly, then again through the scheduler.
    h: Harness = Harness().cooking()
    cook: Optional[Cook] = h.cook
    assert cook is not None
    summary: CookSummary = cook.close(cancel=True)
    assert summary is h.summary() and summary.job_id == JOB_ID
    h.scheduler.on_stop_cook(True)
    assert h.journal.summaries == [summary] and h.job.cancels == 1


def test_a_journal_that_raises_at_the_end_still_hands_the_logger_back() -> None:
    """A bug in a port must not leave the journal's handler on the logger, or the cook open."""
    # The journal raises while recording the summary.
    h: Harness = Harness().cooking()
    h.events.faults["closed"] = RuntimeError("disk full")
    assert h.scheduler.on_stop_cook(True)
    assert h.journal.handler not in logging.getLogger(PACKAGE_LOGGER).handlers
    assert h.job.cancels == 1
    del h.events.faults["closed"]
    assert h.started_fresh()


def test_closing_without_cancel_leaves_a_job_whose_tasks_were_all_offered() -> None:
    """A cook PDG ends normally, with every task released, keeps its job.

    The job may still be finishing on the farm, and failing or cancelling it would
    throw away work the artist is waiting for.
    """
    # Offer every item, then let PDG end the cook.
    h: Harness = Harness().cooking()
    for name in ("a_1", "b_0"):
        assert h.scheduler.on_schedule(handle(name)) is ScheduleResult.SUCCEEDED
    h.scheduler.on_stop_cook(False)
    assert (h.job.cancels, h.job.fails) == (0, 0)
    assert h.summary().final_phase is CookPhase.COOKING


def test_an_end_with_tasks_pdg_never_offered_fails_the_job_and_names_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PDG can end a cook without offering every work item.

    A node's own preparation can fail first, as FFmpeg Encode Video does for a
    codec its ffmpeg lacks, and PDG then never calls onSchedule for the item. Its
    task was created SUSPENDED and nothing will ever release it, so the job would
    sit unfinished on the farm for ever. The cook must name the item and fail the
    job, not cancel it, since a cancel reads as the artist stopping the cook.
    """
    # PDG offers a_0 and a_1, never b_0, then ends the cook.
    h: Harness = Harness().cooking()
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.SUCCEEDED
    job: FakeFarmJob = h.job
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        h.scheduler.on_stop_cook(False)
    assert (job.fails, job.cancels) == (1, 0)
    assert "'b_0' of node 'b' was never offered" in caplog.text
    assert "PDG logged nothing for it." in caplog.text
    summary: CookSummary = h.summary()
    assert summary.final_phase is CookPhase.FAILED and not summary.stopped_by_user


def _offer_a_and_reason_for_b(reason: Tuple[str, str]) -> Tuple[Harness, int]:
    """A cook that offered 'a_0' and 'a_1' only, with ``reason`` as the PDG log of the never-offered 'b_0'."""
    # b_0's item ID comes from the submitted snapshot, as the host sees it.
    h: Harness = Harness().cooking()
    assert h.scheduler.on_schedule(handle("a_1")) is ScheduleResult.SUCCEEDED
    spec: Optional[WorkItemSpec] = h.submitter.snapshots[0].item(TaskKey("b", 0))
    assert spec is not None
    h.host.reasons = {spec.item_id: reason}
    return h, spec.item_id


def test_the_error_for_an_item_never_offered_carries_pdgs_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The artist needs PDG's own words, which name what to change in the scene."""
    # b_0's log has an ERROR line and an unrelated last line.
    h: Harness
    item_id: int
    h, item_id = _offer_a_and_reason_for_b(
        ('Video codec "libx264" is not available on this platform.', "ignored")
    )
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        h.scheduler.on_stop_cook(False)
    assert 'PDG\'s reason: Video codec "libx264" is not available on this platform.' in caplog.text
    assert "ignored" not in caplog.text
    assert h.events.details("failure_reason") == [item_id]


def test_a_last_log_line_without_an_error_is_not_called_pdgs_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no ERROR line, the last line may be unrelated, so it is shown as just that."""
    # b_0's log has only an ordinary last line.
    h: Harness
    h, _ = _offer_a_and_reason_for_b(("", "Generated 1 work item"))
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        h.scheduler.on_stop_cook(False)
    assert "PDG logged no error for it. Its last log line: Generated 1 work item" in caplog.text
    assert "PDG's reason" not in caplog.text


def test_many_items_never_offered_are_named_up_to_ten_then_counted_by_node(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A large graph whose items were never offered must not log, and read PDG's log, once per item.

    An item whose dependency failed is never offered either, so this is common.
    The first ten are named with PDG's reason, and the rest are counted per node.
    """
    # a_0 is offered; a_1 to a_12 and b_0 to b_12, 25 items, never are.
    h: Harness = Harness((("a", 13, ()), ("b", 13, ()))).cooking()
    with caplog.at_level(logging.ERROR, logger=PACKAGE_LOGGER):
        h.scheduler.on_stop_cook(False)
    named: List[str] = [r.getMessage() for r in caplog.records if "PDG logged" in r.getMessage()]
    assert [line.split("'")[1] for line in named] == [f"a_{i}" for i in range(1, 11)]
    assert h.events.count("failure_reason") == 10
    counted: List[str] = [
        r.getMessage() for r in caplog.records if " more work item" in r.getMessage()
    ]
    assert counted == [
        "2 more work item(s) of node 'a' were never offered to the Deadline Cloud Scheduler node.",
        "13 more work item(s) of node 'b' were never offered to the Deadline Cloud Scheduler node.",
    ]
    assert h.job.fails == 1


def test_a_user_stop_with_tasks_never_offered_cancels_once_and_is_not_a_failure() -> None:
    """The artist's own stop is a cancel, made exactly once, and never also a failure."""
    # a_1 and b_0 were never offered when the artist stops.
    h: Harness = Harness().cooking()
    job: FakeFarmJob = h.job
    h.scheduler.on_stop_cook(True)
    assert (job.cancels, job.fails) == (1, 0)
    summary: CookSummary = h.summary()
    assert summary.stopped_by_user and summary.final_phase is CookPhase.COOKING
    assert h.events.count("failure_reason") == 0


def test_a_cook_that_already_failed_does_not_fail_its_job_again_at_the_end() -> None:
    """A job the farm already ended needs no second call, and the cook keeps the verdict it reached."""
    # The job reads FAILED before PDG ends the cook.
    h: Harness = Harness().cooking()
    h.job.statuses.append(JobStatus("UPDATE_SUCCEEDED", "FAILED"))
    h.scheduler.on_tick()
    h.scheduler.on_stop_cook(False)
    assert (h.job.fails, h.job.cancels) == (0, 0)
    assert h.events.count("failure_reason") == 0
    assert h.summary().final_phase is CookPhase.FAILED


def _pair_with_unstored_b() -> Harness:
    """A two-step cook whose 'b_0' payload could not be stored, so its task is never released."""
    # Only b_0's upload fails.
    h: Harness = Harness(PAIR).cooking()
    h.events.faults["put_payload"] = PayloadTransferError("s3 down")
    assert h.scheduler.on_schedule(handle("b_0")) is ScheduleResult.FAILED
    del h.events.faults["put_payload"]
    return h


@pytest.mark.parametrize("drained", [False, True])
def test_an_end_with_a_task_never_released_fails_the_job_and_names_it(
    drained: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A task whose payload could not be stored stays SUSPENDED, so the job could never finish.

    When it is a leaf, every other item can still report and the cook drains,
    so the job is failed whether the cook ended COOKING or DRAINING.
    """
    # Optionally let a_0 report, so the cook drains before PDG ends it.
    h: Harness = _pair_with_unstored_b()
    with caplog.at_level(logging.INFO, logger=PACKAGE_LOGGER):
        if drained:
            h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
            h.scheduler.on_tick()
            assert h.phase() is CookPhase.DRAINING
        h.scheduler.on_stop_cook(False)
    assert (h.job.fails, h.job.cancels) == (1, 0)
    assert "Work item 'b_0' of node 'b' could not be stored" in caplog.text
    summary: CookSummary = h.summary()
    assert summary.final_phase is CookPhase.FAILED and not summary.stopped_by_user
    assert (summary.succeeded, summary.failed) == ((1, 1) if drained else (0, 1))
    # The drain and the end are logged with the counts, failures included.
    reported: str = "2 of 2 work item(s) reported, 1 succeeded" if drained else "1 of 2 work"
    assert f"The cook ended Failed with job {JOB_ID}: {reported}" in caplog.text
    assert ("All 2 work item(s) reported: 1 succeeded, 1 failed." in caplog.text) is drained


def test_a_user_stop_with_a_task_never_released_cancels_once() -> None:
    """The artist's stop is still a cancel, and the cook keeps the phase it reached."""
    # a_0 reports, so the cook drains, then the artist stops.
    h: Harness = _pair_with_unstored_b()
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.scheduler.on_tick()
    h.scheduler.on_stop_cook(True)
    assert (h.job.cancels, h.job.fails) == (1, 0)
    assert h.summary().final_phase is CookPhase.DRAINING


def test_a_done_cook_keeps_its_job_at_the_end() -> None:
    """A job the farm reports succeeded is never failed afterwards."""
    # a_0 reports, then the job reads SUCCEEDED.
    h: Harness = _pair_with_unstored_b()
    h.job.polls.append([TaskOutcome(TaskKey("a", 0), TaskStatus.SUCCEEDED)])
    h.job.statuses += [
        JobStatus("UPDATE_SUCCEEDED", "READY"),
        JobStatus("UPDATE_SUCCEEDED", "SUCCEEDED"),
    ]
    h.tick()
    h.tick()
    assert h.phase() is CookPhase.DONE
    h.scheduler.on_stop_cook(False)
    assert (h.job.fails, h.job.cancels) == (0, 0)
    assert h.summary().final_phase is CookPhase.DONE


# -- threads and state --


def test_no_cook_state_is_a_public_attribute() -> None:
    """PDG's three threads share this state, so all of it stays behind methods that guard it."""
    # Check the scheduler, the cook, and both of the cook's monitors.
    h: Harness = Harness().cooking()
    cook: Optional[Cook] = h.cook
    assert cook is not None
    for owner in (h.scheduler, cook, cook._state, cook._work_items):
        public: List[str] = [name for name in vars(owner) if not name.startswith("_")]
        assert public == [], type(owner).__name__


def test_a_poll_that_blocks_does_not_hold_up_scheduling_or_stopping() -> None:
    """No lock is held over a network call, so a slow poll cannot stall PDG's other threads."""
    h: Harness = Harness().cooking()
    entered: threading.Event = threading.Event()
    release: threading.Event = threading.Event()

    def blocking_poll() -> List[TaskOutcome]:
        entered.set()
        assert release.wait(5)
        return [TaskOutcome(TaskKey("a", 1), TaskStatus.SUCCEEDED)]

    h.job.poll = blocking_poll  # type: ignore[method-assign]
    # Tick on one thread while it waits on the network, offer on another.
    ticker: threading.Thread = threading.Thread(target=h.scheduler.on_tick)
    ticker.start()
    assert entered.wait(5)
    results: List[ScheduleResult] = []
    offer: threading.Thread = threading.Thread(
        target=lambda: results.append(h.scheduler.on_schedule(handle("a_1")))
    )
    offer.start()
    offer.join(2)
    assert results == [ScheduleResult.SUCCEEDED], "the offer waited on the poll"
    stopper: threading.Thread = threading.Thread(target=lambda: h.scheduler.on_stop_cook(True))
    stopper.start()
    stopper.join(2)
    assert not stopper.is_alive(), "the stop waited on the poll"
    release.set()
    ticker.join(5)
    assert h.job.cancels == 1


@settings(max_examples=15, deadline=None)
@given(copies=st.integers(1, 3), seed=st.randoms(use_true_random=False))
def test_offers_and_polls_on_three_threads_report_each_item_once(copies: int, seed: Any) -> None:
    """For offers on one thread and duplicated results on two ticking threads, each item reports once.

    The ledger is shared by PDG's threads, so a result claimed on two threads at
    once must still reach PDG once, and only for an item that was offered.
    """
    # Every result comes back 1 to 3 times, shuffled, one per poll. The clock moves
    # on with every read, so each tick polls.
    count: int = 30
    h: Harness = Harness((("a", count, ()),)).cooking()
    h.clock.step = POLL_SECONDS
    cook: Optional[Cook] = h.cook
    assert cook is not None
    keys: List[TaskKey] = [TaskKey("a", i) for i in range(count)]
    outcomes: List[TaskOutcome] = [TaskOutcome(k, TaskStatus.SUCCEEDED) for k in keys] * copies
    seed.shuffle(outcomes)
    h.job.polls = [[o] for o in outcomes]

    def offer_all() -> None:
        for key in keys[1:]:
            h.scheduler.on_schedule(handle(name_of(key)))

    def tick_until_drained() -> None:
        # Once every item has reported the cook stops polling, so stop ticking too.
        while h.job.polls and cook.phase() is CookPhase.COOKING:
            h.scheduler.on_tick()

    # One offering thread and two ticking threads, as PDG runs them.
    threads: List[threading.Thread] = [
        threading.Thread(target=offer_all),
        threading.Thread(target=tick_until_drained),
        threading.Thread(target=tick_until_drained),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    results: Dict[str, List[ItemState]] = h.results()
    assert all(states == [ItemState.SUCCEEDED] for states in results.values())
    # A result claimed before its offer is reported once the farm returns it again.
    h.job.polls = [[TaskOutcome(k, TaskStatus.SUCCEEDED) for k in keys]]
    h.scheduler.on_tick()
    assert h.results() == {name_of(k): [ItemState.SUCCEEDED] for k in keys}
    assert h.phase() is CookPhase.DRAINING


def test_system_clock_is_aware_utc() -> None:
    """Durations subtract the clock's times, so they must all carry the UTC zone."""
    now: datetime = SystemClock().now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_the_allowed_transitions_are_the_specified_table() -> None:
    """The phase table is the cook's contract, so it is checked against the table written out here.

    The other phase tests take the table as their oracle, so they cannot see a
    wrong entry, such as COOKING to DONE, which would skip waiting for results.
    """
    p: Type[CookPhase] = CookPhase
    # Each phase's moves from the table, and IDLE from every phase, because a stop closes any cook.
    table: Dict[CookPhase, FrozenSet[CookPhase]] = {
        p.IDLE: frozenset({p.CONNECTED, p.FAILED}),
        p.CONNECTED: frozenset({p.SNAPSHOTTING}),
        p.SNAPSHOTTING: frozenset({p.BUILDING, p.REFUSED, p.FAILED}),
        p.BUILDING: frozenset({p.SUBMITTING, p.REFUSED, p.FAILED}),
        p.SUBMITTING: frozenset({p.RESOLVING, p.REFUSED, p.FAILED}),
        p.RESOLVING: frozenset({p.COOKING, p.FAILED}),
        p.COOKING: frozenset({p.DRAINING, p.FAILED}),
        p.DRAINING: frozenset({p.DONE, p.FAILED}),
        p.DONE: frozenset(),
        p.REFUSED: frozenset(),
        p.FAILED: frozenset(),
    }
    expected: Dict[CookPhase, FrozenSet[CookPhase]] = {
        phase: moves | {p.IDLE} for phase, moves in table.items()
    }
    assert dict(ALLOWED_TRANSITIONS) == expected
    assert set(expected) == set(CookPhase)


# -- the cook's state machine, as a state machine --


class PhaseMachine(RuleBasedStateMachine):
    """Every phase change, under any sequence of moves and closes, has the properties the table implies.

    A closed machine never moves, a move from a phase the machine is not in never
    moves, IDLE is reached only by closing, a move that succeeds follows the
    table, and one that fails changes nothing. What a submission records (the
    snapshot, the refusals, the job) is kept only when its move succeeds, and the
    job is handed out for one cancel or fail.
    """

    phases: Any = st.sampled_from(list(CookPhase))

    def __init__(self) -> None:
        super().__init__()
        # The model: only the phase, whether the machine is closed, and what was recorded.
        self.state: _CookStateMachine = _CookStateMachine()
        self.phase: CookPhase = CookPhase.IDLE
        self.closed: bool = False
        self.snapshot: Optional[GraphSnapshot] = None
        self.refusals: Tuple[RefusalScenario, ...] = ()
        self.job: Optional[FakeFarmJob] = None
        self.job_taken: bool = False

    def _check(self, from_phase: CookPhase, to_phase: CookPhase, moved: bool, changes: int) -> None:
        """The properties of one attempted move, given the history's length before it."""
        history: Tuple[Tuple[CookPhase, CookPhase], ...] = self.state.history()
        if moved:
            # A move that succeeded came from the current phase of an open machine,
            # followed the table, and was recorded once.
            assert not self.closed and from_phase is self.phase
            assert to_phase is not CookPhase.IDLE and to_phase in ALLOWED_TRANSITIONS[from_phase]
            assert self.state.phase() is to_phase
            assert len(history) == changes + 1 and history[-1] == (from_phase, to_phase)
            self.phase = to_phase
        else:
            # A move that failed changed nothing.
            assert self.state.phase() is self.phase and len(history) == changes
        # On an open machine, a move the table allows from the current phase succeeds.
        if (
            not self.closed
            and from_phase is self.phase
            and to_phase in ALLOWED_TRANSITIONS[from_phase] - {CookPhase.IDLE}
        ):
            assert moved

    @rule(from_phase=phases, to_phase=phases)
    def move(self, from_phase: CookPhase, to_phase: CookPhase) -> None:
        changes: int = len(self.state.history())
        self._check(from_phase, to_phase, self.state.move(from_phase, to_phase), changes)

    @rule(to_phase=phases)
    def move_from_the_current_phase(self, to_phase: CookPhase) -> None:
        # Most random pairs name the wrong phase, so also try the right one.
        changes: int = len(self.state.history())
        from_phase: CookPhase = self.phase
        self._check(from_phase, to_phase, self.state.move(from_phase, to_phase), changes)

    @rule()
    def fail(self) -> None:
        changes: int = len(self.state.history())
        before: CookPhase = self.phase
        moved: bool = self.state.fail()
        # fail() succeeds exactly when FAILED is allowed from the current phase of an open machine.
        assert moved == (not self.closed and CookPhase.FAILED in ALLOWED_TRANSITIONS[before])
        self._check(before, CookPhase.FAILED, moved, changes)

    @rule()
    def record_snapshot(self) -> None:
        # The snapshot is kept only when SNAPSHOTTING moves to BUILDING.
        snapshot: GraphSnapshot = GraphSnapshot(steps=())
        changes: int = len(self.state.history())
        moved: bool = self.state.record_snapshot(snapshot)
        self._check(CookPhase.SNAPSHOTTING, CookPhase.BUILDING, moved, changes)
        if moved:
            self.snapshot = snapshot

    @rule(from_phase=phases)
    def refuse(self, from_phase: CookPhase) -> None:
        # The refusals are kept only when the move to REFUSED succeeds.
        refusals: Tuple[RefusalScenario, ...] = (RefusalScenario.DYNAMIC_NODE,)
        changes: int = len(self.state.history())
        moved: bool = self.state.refuse(refusals, from_phase=from_phase)
        self._check(from_phase, CookPhase.REFUSED, moved, changes)
        if moved:
            self.refusals = refusals

    @rule()
    def record_job(self) -> None:
        # The job is kept only when SUBMITTING moves to RESOLVING.
        job: FakeFarmJob = FakeFarmJob(Events(), JOB_ID, GraphSnapshot(steps=()))
        changes: int = len(self.state.history())
        moved: bool = self.state.record_job(JOB_ID, job)
        self._check(CookPhase.SUBMITTING, CookPhase.RESOLVING, moved, changes)
        if moved:
            self.job = job

    @rule()
    def take_job_to_end(self) -> None:
        # The recorded job is handed out once, and nothing before a job is recorded.
        expected: Optional[FakeFarmJob] = None if self.job_taken else self.job
        assert self.state.take_job_to_end() is expected
        self.job_taken = self.job_taken or self.job is not None

    @rule()
    def close(self) -> None:
        # Closing returns the phase it closed from, and the history ends in IDLE once.
        changes: int = len(self.state.history())
        final: CookPhase = self.state.close()
        assert final is self.phase
        history: Tuple[Tuple[CookPhase, CookPhase], ...] = self.state.history()
        if self.closed:
            assert len(history) == changes
        else:
            assert len(history) == changes + 1 and history[-1] == (final, CookPhase.IDLE)
        self.closed = True
        self.phase = CookPhase.IDLE

    @invariant()
    def records_only_what_a_successful_move_kept(self) -> None:
        # The phase, what was recorded, and the job ID agree with the model.
        assert self.state.phase() is self.phase
        assert self.state.snapshot() is self.snapshot
        assert self.state.refusals() == self.refusals
        assert self.state.job_id() == (JOB_ID if self.job else "")
        # The running view exists exactly in COOKING and DRAINING once a job and a
        # snapshot were recorded, and holds them. Bare moves can reach COOKING
        # without either, and then there is nothing to run.
        running: Any = self.state.running()
        if (
            self.phase in (CookPhase.COOKING, CookPhase.DRAINING)
            and self.job is not None
            and self.snapshot is not None
        ):
            assert running is not None and running.phase is self.phase
            assert running.job is self.job and running.snapshot is self.snapshot
        else:
            assert running is None


PhaseMachine.TestCase.settings = settings(max_examples=60, stateful_step_count=30, deadline=None)
test_phase_machine: Any = PhaseMachine.TestCase


# -- the work item tracker, as a state machine --


def _spec(key: TaskKey) -> WorkItemSpec:
    return WorkItemSpec(
        name=name_of(key),
        task_index=key.task_index,
        pdg_index=key.task_index,
        item_id=key.task_index,
        node_name=key.step_name,
    )


def _key_of(offered: _OfferedItem) -> TaskKey:
    return TaskKey(offered.spec.node_name, offered.spec.task_index)


class TrackerMachine(RuleBasedStateMachine):
    """Any sequence of offers, starts, results, job failures, and closes, as the cook makes them.

    Each item's result is handed out at most once, only once offered, never before
    the item's STARTED, and never after the close. After a job failure no offer is
    recorded, and every item still waiting is failed or left to the thread
    releasing it. A result that came before STARTED comes back once it was sent.
    The counts agree with what was handed out.
    """

    keys: Any = st.builds(TaskKey, st.sampled_from(["a", "b"]), st.integers(0, 3))

    def __init__(self) -> None:
        super().__init__()
        # The facts so far, from the calls made and what they returned.
        self.tracker: _WorkItemTracker = _WorkItemTracker()
        self.expected: int = 0
        self.offered: Set[TaskKey] = set()
        self.started: Set[TaskKey] = set()
        self.unreleased: List[TaskKey] = []
        #: Each key whose result was handed out to report, and whether it counts as a success.
        self.results: Dict[TaskKey, bool] = {}
        #: The first result for each item that came before its STARTED.
        self.waiting: Dict[TaskKey, bool] = {}
        #: Items the job failed before their STARTED.
        self.failed_unstarted: Set[TaskKey] = set()
        self.canceled: Set[TaskKey] = set()
        self.job_failed: bool = False
        self.closed: bool = False

    def _hand_out(self, key: TaskKey, succeeded: bool) -> None:
        # No item's result is handed out twice, or for an item never offered.
        assert key in self.offered and key not in self.results, key
        self.results[key] = succeeded

    def _settled(self, key: TaskKey) -> bool:
        return key in self.results or key in self.failed_unstarted

    @rule(count=st.integers(0, 10))
    def set_expected_count(self, count: int) -> None:
        self.tracker.set_expected_count(count)
        self.expected = count

    @rule(key=keys)
    def offer(self, key: TaskKey) -> None:
        recorded: bool = self.tracker.record_offer(
            key, _OfferedItem(handle(name_of(key)), _spec(key), _T0)
        )
        # Nothing is recorded once the job failed or the cook closed, and no item twice.
        assert recorded == (not self.closed and not self.job_failed and key not in self.offered)
        if recorded:
            self.offered.add(key)
        assert self.tracker.was_offered(key) == (key in self.offered)

    @rule(data=st.data())
    def start(self, data: Any) -> None:
        # The cook sends STARTED once per offered item whose payload was stored.
        startable: List[TaskKey] = sorted(
            self.offered - self.started - set(self.unreleased), key=name_of
        )
        if not startable:
            return
        key: TaskKey = data.draw(st.sampled_from(startable))
        fail_now: bool = self.tracker.mark_started(key)
        self.started.add(key)
        # Only an item the job failed before its STARTED is failed now, and none once closed.
        assert fail_now == (key in self.failed_unstarted and not self.closed)
        if fail_now:
            self.failed_unstarted.discard(key)
            self._hand_out(key, False)

    @rule(key=keys, succeeded=st.booleans())
    def result(self, key: TaskKey, succeeded: bool) -> None:
        self._result(key, succeeded)

    def _result(self, key: TaskKey, succeeded: bool) -> None:
        offered: Optional[_OfferedItem] = self.tracker.record_result(key, succeeded=succeeded)
        open_and_unsettled: bool = (
            not self.closed and key in self.offered and not self._settled(key)
        )
        if offered is not None:
            # Handed out only after STARTED, and only while open.
            assert key in self.started and open_and_unsettled and _key_of(offered) == key
            self._hand_out(key, succeeded)
            return
        # A result for a started item that has none yet is never dropped.
        assert not (open_and_unsettled and key in self.started)
        if open_and_unsettled:
            self.waiting.setdefault(key, succeeded)

    @rule()
    def take_waiting(self) -> None:
        returned: List[TaskOutcome] = self.tracker.take_waiting()
        # Exactly the waiting results whose item has since started come back, each with its first status.
        ready: Dict[TaskKey, bool] = {k: v for k, v in self.waiting.items() if k in self.started}
        assert {o.key: o.status is TaskStatus.SUCCEEDED for o in returned} == ready
        assert len(returned) == len(ready)
        for outcome in returned:
            del self.waiting[outcome.key]
            # The cook records each one again, now that STARTED was sent.
            self._result(outcome.key, outcome.status is TaskStatus.SUCCEEDED)

    @precondition(lambda self: bool(self.results))
    @rule(data=st.data())
    def delivery_failed(self, data: Any) -> None:
        key: TaskKey = data.draw(st.sampled_from(sorted(self.results, key=name_of)))
        self.tracker.record_delivery_failed(key)
        if not self.closed:
            self.results[key] = False

    @rule(data=st.data())
    def payload_not_stored(self, data: Any) -> None:
        # The cook records this once, for an offered item it has not started.
        candidates: List[TaskKey] = sorted(
            self.offered - self.started - set(self.unreleased) - set(self.results), key=name_of
        )
        if not candidates:
            return
        key: TaskKey = data.draw(st.sampled_from(candidates))
        self.tracker.record_unreleased(key)
        if self.closed:
            return
        self.unreleased.append(key)
        self.waiting.pop(key, None)
        self.failed_unstarted.discard(key)
        self._hand_out(key, False)

    @rule()
    def fail_unreported(self) -> None:
        failed: List[TaskKey] = [_key_of(o) for o in self.tracker.fail_unreported()]
        if self.closed:
            assert failed == []
            return
        # Every started item without a result is failed now, once; unstarted ones are left
        # to the thread releasing them.
        unsettled: Set[TaskKey] = {k for k in self.offered if not self._settled(k)}
        assert len(failed) == len(set(failed)) and set(failed) == unsettled & self.started
        self.job_failed = True
        self.failed_unstarted |= unsettled - self.started
        for key in unsettled:
            self.waiting.pop(key, None)
        for key in failed:
            self._hand_out(key, False)

    @rule(key=keys)
    def cancel(self, key: TaskKey) -> None:
        new: bool = not self.closed and key not in self.canceled
        assert self.tracker.record_cancellation(key) == new
        if new:
            self.canceled.add(key)

    @rule()
    def close(self) -> None:
        self.closed = True
        self._check_counts(self.tracker.close())

    def _check_counts(self, counts: Any) -> None:
        # The counts are what was handed out, and the cancels with no other result.
        assert counts.succeeded == sum(self.results.values())
        assert counts.failed == len(self.results) - counts.succeeded
        assert counts.offered == len(self.offered)
        assert counts.canceled == len(self.canceled - set(self.results))

    @invariant()
    def counts_and_reads_agree_with_the_facts(self) -> None:
        self._check_counts(self.tracker.counts())
        assert self.tracker.offered_keys() == self.offered
        assert [_key_of(o) for o in self.tracker.never_released()] == self.unreleased
        assert self.tracker.all_reported() == (
            self.expected > 0 and len(self.results) >= self.expected
        )


TrackerMachine.TestCase.settings = settings(max_examples=200, stateful_step_count=40, deadline=None)
test_tracker: Any = TrackerMachine.TestCase


def test_a_closed_tracker_changes_no_count() -> None:
    """The summary is built from the counts at the close, so nothing later may change them.

    That includes a success PDG rejects after the close, which would otherwise be
    counted failed after the summary said it succeeded.
    """
    # One success handed out, an offered item never started, then the close.
    tracker: _WorkItemTracker = _WorkItemTracker()
    done: TaskKey = TaskKey("a", 0)
    pending: TaskKey = TaskKey("a", 1)
    for key in (done, pending):
        assert tracker.record_offer(key, _OfferedItem(handle(name_of(key)), _spec(key), _T0))
    tracker.mark_started(done)
    assert tracker.record_result(done, succeeded=True) is not None
    closed: Any = tracker.close()
    # Every later call that could record something records nothing.
    tracker.record_delivery_failed(done)
    tracker.record_unreleased(pending)
    assert not tracker.record_cancellation(pending)
    assert tracker.fail_unreported() == []
    assert tracker.counts() == closed and (closed.succeeded, closed.failed) == (1, 0)


# -- the four callbacks, as a state machine --


class CallbackMachine(RuleBasedStateMachine):
    """Any interleaving of PDG's callbacks and injected faults moves only along allowed transitions.

    Terminal phases leave only for IDLE, a closed cook stays IDLE, and no job is
    canceled or failed twice.
    """

    def __init__(self) -> None:
        super().__init__()
        self.h: Harness = Harness()

    @rule(generate_only=st.booleans())
    def start(self, generate_only: bool) -> None:
        # Faults are injected on purpose, so a start may raise. The invariants
        # check the state it leaves behind.
        try:
            self.h.scheduler.on_start_cook(generate_only)
        except Exception:
            pass
        # Reading the open cook keeps it, so the invariants check it after it closes.
        self.h.cook

    @rule(
        name=st.sampled_from(["a_0", "a_1", "b_0", "x_0"]),
        fault=st.sampled_from([None, "refuse", "submit", "start", "put_payload"]),
    )
    def schedule(self, name: str, fault: Optional[str]) -> None:
        # A refusal comes from the graph, any other fault from the named call.
        self.h.events.faults.clear()
        self.h.host.nodes = refused("dynamic") if fault == "refuse" else nodes_for(CHAIN)
        if fault in ("submit", "start", "put_payload"):
            self.h.events.faults[fault] = RuntimeError(fault)
        # Faults are injected on purpose, so a schedule may raise. The invariants
        # check the state it leaves behind.
        try:
            self.h.scheduler.on_schedule(handle(name))
        except Exception:
            pass
        self.h.events.faults.clear()
        self.h.host.nodes = nodes_for(CHAIN)

    @precondition(lambda self: bool(self.h.jobs))
    @rule(
        outcomes=st.lists(
            st.tuples(
                st.sampled_from([("a", 0), ("a", 1), ("b", 0)]),
                st.sampled_from(["SUCCEEDED", "FAILED", "CANCELED"]),
            ),
            max_size=4,
        ),
        job_status=st.sampled_from(["READY", "SUCCEEDED", "FAILED", ""]),
    )
    def tick(self, outcomes: List[Any], job_status: str) -> None:
        job: FakeFarmJob = self.h.job
        job.polls.append(
            [TaskOutcome(TaskKey(*key), TaskStatus(status)) for key, status in outcomes]
        )
        job.statuses.append(JobStatus("UPDATE_SUCCEEDED" if job_status else "", job_status))
        self.h.tick()

    @rule()
    def tick_without_a_job(self) -> None:
        self.h.scheduler.on_tick()

    @rule(cancel=st.booleans())
    def stop(self, cancel: bool) -> None:
        assert self.h.scheduler.on_stop_cook(cancel)

    @invariant()
    def every_change_is_allowed_and_terminal_phases_hold(self) -> None:
        self.h.cook
        terminal: Set[CookPhase] = {CookPhase.DONE, CookPhase.REFUSED, CookPhase.FAILED}
        for cook in self.h.cooks:
            history: Tuple[Tuple[CookPhase, CookPhase], ...] = self.h.history(cook)
            for i, (before, after) in enumerate(history):
                assert after in ALLOWED_TRANSITIONS[before], (before, after)
                if i:
                    assert history[i - 1][1] is before
                if before in terminal:
                    assert after is CookPhase.IDLE
            if history and history[-1][1] is CookPhase.IDLE:
                assert cook.phase() is CookPhase.IDLE
        for job in self.h.jobs:
            assert job.cancels + job.fails <= 1


CallbackMachine.TestCase.settings = settings(
    max_examples=40,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)
test_callbacks: Any = CallbackMachine.TestCase


# -- a whole cook, as a state machine --


class FullCookMachine(RuleBasedStateMachine):
    """Duplicated, reordered polls, retries, cancellations, and a job failure at any tick.

    Every item gets at most one result, no cancellation becomes a failure, and a job
    failure fails each outstanding item exactly once.
    """

    STEPS: Steps = (("a", 3, ()), ("b", 2, ("a",)))
    KEYS: List[TaskKey] = [
        TaskKey("a", 0),
        TaskKey("a", 1),
        TaskKey("a", 2),
        TaskKey("b", 0),
        TaskKey("b", 1),
    ]

    def __init__(self) -> None:
        super().__init__()
        # What the model expects, built up as the rules run.
        self.h: Harness = Harness(self.STEPS)
        self.h.start()
        self.offered: Set[str] = set()
        self.history: List[TaskOutcome] = []
        self.job_failed: bool = False
        self.outstanding_at_failure: Set[str] = set()

    @rule(key=st.sampled_from(KEYS))
    def offer(self, key: TaskKey) -> None:
        result: ScheduleResult = self.h.scheduler.on_schedule(handle(name_of(key)))
        if result is ScheduleResult.SUCCEEDED:
            self.offered.add(name_of(key))

    @precondition(lambda self: bool(self.h.jobs))
    @rule(
        data=st.data(),
        status=st.sampled_from(["SUCCEEDED", "FAILED", "CANCELED"]),
        key=st.sampled_from(KEYS),
        replay=st.integers(0, 3),
        fail_job=st.booleans(),
    )
    def tick(self, data: Any, status: str, key: TaskKey, replay: int, fail_job: bool) -> None:
        # A fresh outcome, mixed with replays of earlier ones in any order.
        fresh: TaskOutcome = TaskOutcome(key, TaskStatus(status))
        self.history.append(fresh)
        replays: List[TaskOutcome] = [
            data.draw(st.sampled_from(self.history)) for _ in range(replay)
        ]
        batch: List[TaskOutcome] = list(data.draw(st.permutations([fresh, *replays])))
        self.h.job.polls.append(batch)
        if fail_job and not self.job_failed:
            # Outcomes of this same tick are reported before the job check.
            results: Dict[str, List[ItemState]] = self.h.results()
            reported_now: Set[str] = {
                name_of(o.key) for o in batch if o.status is not TaskStatus.CANCELED
            }
            self.outstanding_at_failure = {
                n for n in self.offered if n not in results and n not in reported_now
            }
            self.h.job.statuses.append(JobStatus("UPDATE_SUCCEEDED", "FAILED"))
            self.job_failed = True
        self.h.tick()

    @invariant()
    def at_most_one_result_and_no_cancellation_is_a_failure(self) -> None:
        results: Dict[str, List[ItemState]] = self.h.results()
        for name, states in results.items():
            assert len(states) == 1, (name, states)
            assert name in self.offered
        if not self.job_failed:
            for name, states in results.items():
                if states == [ItemState.FAILED]:
                    node: str
                    index: str
                    node, index = name.rsplit("_", 1)
                    assert TaskOutcome(TaskKey(node, int(index)), TaskStatus.FAILED) in self.history

    def teardown(self) -> None:
        if self.job_failed:
            failed: Set[str] = {n for n, s in self.h.results().items() if s == [ItemState.FAILED]}
            assert self.outstanding_at_failure <= failed
        self.h.scheduler.on_stop_cook(False)


FullCookMachine.TestCase.settings = settings(max_examples=40, stateful_step_count=25, deadline=None)
test_full_cook: Any = FullCookMachine.TestCase
