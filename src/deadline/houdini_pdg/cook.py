# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler node's core: one cook at a time, from ``onStartCook`` to ``onStopCook``.

PDG calls the scheduler node from three threads. Each cook is a new :class:`Cook`,
so nothing carries over from the previous one, and every phase change is a
compare-and-set, so a submission still running when the cook stops cannot write a
phase afterwards.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import AbstractSet, Callable, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from .diagnostics import CookJournal
from .errors import GraphRefusedError, QuotaExceededError
from .graph import (
    PDG_DIR_TOKEN,
    DeclaredFile,
    GraphSnapshot,
    NodeInfo,
    TaskKey,
    WorkItemSpec,
    synthesize,
)
from .houdini.settings import SchedulerSettings
from .ports import (
    Clock,
    CookPhase,
    CookSummary,
    FarmJob,
    ItemOutcome,
    ItemState,
    JobStatus,
    JobSubmitter,
    OutputFile,
    PdgHost,
    TaskOutcome,
    TaskStatus,
    TaskStorage,
    WorkItemHandle,
)
from .scene_reads import ReadReport, SceneReads, match_reads
from .validation import RefusalScenario, ValidationContext, Verdict, validate, waived

_logger: logging.Logger = logging.getLogger(__name__)

#: The logger every module of the scheduler node logs under. The cook's journal
#: handler is attached here for the life of the cook.
_PACKAGE_LOGGER: str = "deadline.houdini_pdg"

_HEARTBEAT_INTERVAL: timedelta = timedelta(seconds=30)

# How often a tick polls the farm and reads the job's status. Each poll is a
# search of the job's tasks, and each status read a GetJob call.
_POLL_INTERVAL: timedelta = timedelta(seconds=5)

# How long the first work item may wait for PDG to generate the rest of the graph.
# Generation can wait on in-process nodes upstream, such as a Wedge.
_GENERATION_TIMEOUT: timedelta = timedelta(minutes=5)
_GENERATION_LOG_INTERVAL: timedelta = timedelta(seconds=10)
_LOG_TAIL_LINES: int = 50

# How many never-offered work items are named, each with PDG's reason, when a cook
# ends. Reading the reason reads the item's PDG log, so the rest are only counted.
_NAMED_NEVER_OFFERED: int = 10

# From these the job produces no further results.
_JOB_LIFECYCLE_FAILURES: FrozenSet[str] = frozenset(
    {"CREATE_FAILED", "UPLOAD_FAILED", "UPDATE_FAILED", "ARCHIVED"}
)
_JOB_RUN_FAILURES: FrozenSet[str] = frozenset({"FAILED", "CANCELED"})


class ScheduleResult(Enum):
    """The answer to ``onSchedule``."""

    SUCCEEDED = 0
    FAILED = 1
    DEFERRED = 2


def _allowed(*phases: CookPhase) -> FrozenSet[CookPhase]:
    # PDG may call onStopCook at any moment, so every phase can close.
    return frozenset(phases) | {CookPhase.IDLE}


#: The phases each phase may move to. DONE, REFUSED, and FAILED hold until the cook stops.
#:
#: Every phase may move to IDLE, because stopping closes the cook. A closed cook is
#: never reused: the scheduler node builds a new one for the next cook, and
#: :attr:`~deadline.houdini_pdg.ports.CookSummary.final_phase` keeps the phase it
#: closed from. An artist's cancel is a stop, not a phase.
ALLOWED_TRANSITIONS: Mapping[CookPhase, FrozenSet[CookPhase]] = MappingProxyType(
    {
        CookPhase.IDLE: _allowed(CookPhase.CONNECTED, CookPhase.FAILED),
        CookPhase.CONNECTED: _allowed(CookPhase.SNAPSHOTTING),
        CookPhase.SNAPSHOTTING: _allowed(CookPhase.BUILDING, CookPhase.REFUSED, CookPhase.FAILED),
        CookPhase.BUILDING: _allowed(CookPhase.SUBMITTING, CookPhase.REFUSED, CookPhase.FAILED),
        CookPhase.SUBMITTING: _allowed(CookPhase.RESOLVING, CookPhase.REFUSED, CookPhase.FAILED),
        CookPhase.RESOLVING: _allowed(CookPhase.COOKING, CookPhase.FAILED),
        CookPhase.COOKING: _allowed(CookPhase.DRAINING, CookPhase.FAILED),
        CookPhase.DRAINING: _allowed(CookPhase.DONE, CookPhase.FAILED),
        CookPhase.DONE: _allowed(),
        CookPhase.REFUSED: _allowed(),
        CookPhase.FAILED: _allowed(),
    }
)


@dataclass(frozen=True)
class CookServices:
    """What one cook needs beyond PDG and the clock, built for the workstation's farm and queue."""

    submitter: JobSubmitter
    storage: TaskStorage
    #: Opens the submitted job by its ID. Never raises: it builds the job's handle
    #: and makes no call to the farm.
    open_job: Callable[[str, GraphSnapshot], FarmJob]
    journal: CookJournal


@dataclass(frozen=True)
class _Running:
    """A cook with a job to watch, read in one step: its phase, its job, and its snapshot."""

    #: COOKING or DRAINING.
    phase: CookPhase
    job: FarmJob
    snapshot: GraphSnapshot


class _CookStateMachine:
    """The phase, and what the submission produced. Every change is a compare-and-set.

    Once closed, nothing moves, so a submission that outlives its cook learns that
    from the next move it attempts.
    """

    def __init__(self) -> None:
        # Every field below is read and written only under the lock.
        self._lock: threading.Lock = threading.Lock()
        self._phase: CookPhase = CookPhase.IDLE
        self._closed: bool = False
        self._history: List[Tuple[CookPhase, CookPhase]] = []
        self._snapshot: Optional[GraphSnapshot] = None
        self._refusals: Tuple[RefusalScenario, ...] = ()
        self._job_id: str = ""
        self._job: Optional[FarmJob] = None
        self._job_taken_to_end: bool = False

    def phase(self) -> CookPhase:
        with self._lock:
            return self._phase

    def running(self) -> Optional[_Running]:
        """The phase with the job and the snapshot, when the cook is COOKING or DRAINING."""
        with self._lock:
            # Both are recorded before COOKING, and never unset.
            if (
                self._phase not in (CookPhase.COOKING, CookPhase.DRAINING)
                or self._job is None
                or self._snapshot is None
            ):
                return None
            return _Running(self._phase, self._job, self._snapshot)

    def move(self, from_phase: CookPhase, to_phase: CookPhase) -> bool:
        """Move from ``from_phase`` to ``to_phase``, if the cook is still in ``from_phase``."""
        with self._lock:
            return self._move(from_phase, to_phase)

    def fail(self) -> bool:
        """Move the current phase to FAILED, unless the cook is closed or the table forbids it."""
        with self._lock:
            return self._move(self._phase, CookPhase.FAILED)

    def record_snapshot(self, snapshot: GraphSnapshot) -> bool:
        """Record the snapshot and move SNAPSHOTTING to BUILDING."""
        with self._lock:
            moved: bool = self._move(CookPhase.SNAPSHOTTING, CookPhase.BUILDING)
            if moved:
                self._snapshot = snapshot
            return moved

    def refuse(self, refusals: Tuple[RefusalScenario, ...], *, from_phase: CookPhase) -> bool:
        """Record the refusals and move ``from_phase`` to REFUSED."""
        with self._lock:
            moved: bool = self._move(from_phase, CookPhase.REFUSED)
            if moved:
                self._refusals = refusals
            return moved

    def record_job(self, job_id: str, job: FarmJob) -> bool:
        """Record the created job and move SUBMITTING to RESOLVING, or refuse it once closed."""
        with self._lock:
            moved: bool = self._move(CookPhase.SUBMITTING, CookPhase.RESOLVING)
            if moved:
                self._job_id, self._job = job_id, job
            return moved

    def take_job_to_end(self) -> Optional[FarmJob]:
        """The job to cancel or fail, the first time only, so it gets one such call."""
        with self._lock:
            if self._job is None or self._job_taken_to_end:
                return None
            self._job_taken_to_end = True
            return self._job

    def close(self) -> CookPhase:
        """End the cook and return the phase it ended in."""
        with self._lock:
            final: CookPhase = self._phase
            # A second close changes nothing, so the history ends in IDLE once.
            if not self._closed:
                self._closed = True
                self._history.append((final, CookPhase.IDLE))
                self._phase = CookPhase.IDLE
            return final

    def snapshot(self) -> Optional[GraphSnapshot]:
        with self._lock:
            return self._snapshot

    def job_id(self) -> str:
        with self._lock:
            return self._job_id

    def refusals(self) -> Tuple[RefusalScenario, ...]:
        with self._lock:
            return self._refusals

    def history(self) -> Tuple[Tuple[CookPhase, CookPhase], ...]:
        """Every phase change, in order, including the ones that lasted no time."""
        with self._lock:
            return tuple(self._history)

    def _move(self, from_phase: CookPhase, to_phase: CookPhase) -> bool:
        # Only close() reaches IDLE, and nothing moves once closed, when the
        # phase is no longer the one the caller saw, or outside the table.
        moved: bool = (
            not self._closed
            and self._phase is from_phase
            and to_phase is not CookPhase.IDLE
            and to_phase in ALLOWED_TRANSITIONS[from_phase]
        )
        if moved:
            self._history.append((from_phase, to_phase))
            self._phase = to_phase
        return moved


@dataclass(frozen=True)
class _OfferedItem:
    """A work item PDG offered, and what reporting it needs."""

    item: WorkItemHandle
    spec: WorkItemSpec
    offered_at: datetime


@dataclass(frozen=True)
class _ItemCounts:
    """The tracker's counts at one moment, read together under its lock."""

    offered: int
    #: Items whose result PDG was told, or is being told, by result.
    succeeded: int
    failed: int
    #: Tasks the farm canceled whose items got no other result. Never reported.
    canceled: int


class _WorkItemTracker:
    """The items PDG offered, and the one result each gets. PDG's threads share it.

    PDG must hear that an item started before it hears the item's result. So a
    result that arrives before the item's STARTED was sent waits for it. Once
    closed, the tracker records nothing more.
    """

    def __init__(self) -> None:
        # Every field below is read and written only under the lock.
        self._lock: threading.Lock = threading.Lock()
        self._expected_count: int = 0
        self._offered: Dict[TaskKey, _OfferedItem] = {}
        # Items PDG was told started.
        self._started: Set[TaskKey] = set()
        # Items whose result PDG was told, or is being told, and whether each
        # succeeded. Always a subset of _offered.
        self._reported_to_pdg: Dict[TaskKey, bool] = {}
        # The farm's results for items whose STARTED was not sent when they came.
        self._waiting_for_start: Dict[TaskKey, TaskOutcome] = {}
        # Items the job failed before their STARTED was sent. The thread releasing
        # each one reports the failure after STARTED.
        self._failed_before_start: Set[TaskKey] = set()
        self._canceled_by_farm: Set[TaskKey] = set()
        # Items reported failed because their payload could not be stored. Their
        # tasks were never released.
        self._never_released: Dict[TaskKey, _OfferedItem] = {}
        # Once the job failed no item can run, and once closed nothing is recorded.
        self._job_failed: bool = False
        self._closed: bool = False

    def set_expected_count(self, count: int) -> None:
        with self._lock:
            self._expected_count = count

    def record_offer(self, key: TaskKey, offered: _OfferedItem) -> bool:
        """Record the offer. False for a key offered before, and once the job failed or the cook closed."""
        with self._lock:
            if self._closed or self._job_failed or key in self._offered:
                return False
            self._offered[key] = offered
            return True

    def was_offered(self, key: TaskKey) -> bool:
        with self._lock:
            return key in self._offered

    def mark_started(self, key: TaskKey) -> bool:
        """Record that PDG was told the item started. True when the job failed it meanwhile.

        The caller then reports that failure, which PDG hears after STARTED.
        """
        with self._lock:
            # A job failure that came before STARTED becomes the item's result now.
            self._started.add(key)
            if self._closed or key not in self._failed_before_start:
                return False
            self._failed_before_start.discard(key)
            self._reported_to_pdg[key] = False
            return True

    def record_result(self, key: TaskKey, *, succeeded: bool) -> Optional[_OfferedItem]:
        """Record the item's one result, and return the item the first time only.

        None for a key PDG never offered, so its item can still report once offered,
        and None once closed. A result for an item whose STARTED was not sent yet
        waits for it, and :meth:`take_waiting` hands it back.
        """
        with self._lock:
            # Nothing for an unoffered item, a second result, or after the close.
            offered: Optional[_OfferedItem] = self._offered.get(key)
            if (
                self._closed
                or offered is None
                or key in self._reported_to_pdg
                or key in self._failed_before_start
            ):
                return None
            # Before STARTED the result waits, keeping the first one.
            if key not in self._started:
                status: TaskStatus = TaskStatus.SUCCEEDED if succeeded else TaskStatus.FAILED
                self._waiting_for_start.setdefault(key, TaskOutcome(key, status))
                return None
            self._reported_to_pdg[key] = succeeded
            return offered

    def take_waiting(self) -> List[TaskOutcome]:
        """The results that waited for their item's STARTED, now that it was sent."""
        with self._lock:
            ready: List[TaskOutcome] = [
                o for k, o in self._waiting_for_start.items() if k in self._started
            ]
            for outcome in ready:
                del self._waiting_for_start[outcome.key]
            return ready

    def record_delivery_failed(self, key: TaskKey) -> None:
        """Count the item failed: PDG could not be told it succeeded, so it is told it failed."""
        with self._lock:
            if not self._closed and key in self._reported_to_pdg:
                self._reported_to_pdg[key] = False

    def record_unreleased(self, key: TaskKey) -> None:
        """Record that the item's task will never be released, and its result as failed.

        One step, so a result the farm returns meanwhile is ignored. The caller
        tells PDG through ``onSchedule``'s return value.
        """
        with self._lock:
            # Only an offered item without a result gets this one, and nothing once closed.
            offered: Optional[_OfferedItem] = self._offered.get(key)
            if self._closed or offered is None or key in self._reported_to_pdg:
                return
            # Any result that came for it, or a job failure, gives way to this failure.
            self._waiting_for_start.pop(key, None)
            self._failed_before_start.discard(key)
            self._reported_to_pdg[key] = False
            self._never_released[key] = offered

    def never_released(self) -> List[_OfferedItem]:
        """The items whose tasks were never released, in the order they were recorded."""
        with self._lock:
            return list(self._never_released.values())

    def fail_unreported(self) -> List[_OfferedItem]:
        """Fail every item without a result, refuse later offers, and return the items to tell PDG.

        An item whose STARTED was not sent yet is left to the thread releasing it,
        so PDG hears STARTED first.
        """
        with self._lock:
            # From now on no offer is recorded, and nothing at all once closed.
            if self._closed:
                return []
            self._job_failed = True
            # Each item without a result fails now, or after its STARTED.
            outstanding: List[_OfferedItem] = []
            for key, offered in self._offered.items():
                if key in self._reported_to_pdg:
                    continue
                self._waiting_for_start.pop(key, None)
                if key in self._started:
                    self._reported_to_pdg[key] = False
                    outstanding.append(offered)
                else:
                    self._failed_before_start.add(key)
            return outstanding

    def record_cancellation(self, key: TaskKey) -> bool:
        with self._lock:
            if self._closed or key in self._canceled_by_farm:
                return False
            self._canceled_by_farm.add(key)
            return True

    def all_reported(self) -> bool:
        with self._lock:
            return self._expected_count > 0 and len(self._reported_to_pdg) >= self._expected_count

    def offered_keys(self) -> Set[TaskKey]:
        """Every key PDG offered, reported or not."""
        with self._lock:
            return set(self._offered)

    def counts(self) -> _ItemCounts:
        with self._lock:
            return self._counts()

    def close(self) -> _ItemCounts:
        """Stop recording anything, and return the final counts."""
        with self._lock:
            self._closed = True
            return self._counts()

    def _counts(self) -> _ItemCounts:
        succeeded: int = sum(1 for ok in self._reported_to_pdg.values() if ok)
        return _ItemCounts(
            offered=len(self._offered),
            succeeded=succeeded,
            failed=len(self._reported_to_pdg) - succeeded,
            canceled=len(self._canceled_by_farm - self._reported_to_pdg.keys()),
        )


@dataclass
class _TickReads:
    """What one tick has read so far, handed from part to part."""

    #: The cook's phase, job, and snapshot, when it has a job to watch.
    running: Optional[_Running] = None
    #: Whether this tick polls the farm and reads the job's status.
    polls: bool = False
    #: The job's status, once this tick has read it.
    status: Optional[JobStatus] = None


class Cook:
    """One cook.

    :meth:`start` enters CONNECTED. The first :meth:`schedule` snapshots,
    validates, submits, and starts the job, and later calls hand items to the farm.
    :meth:`tick` polls the farm, reports results, and checks the job.
    """

    def __init__(
        self,
        *,
        cook_id: str,
        host: PdgHost,
        clock: Clock,
        settings: SchedulerSettings,
        services: CookServices,
    ) -> None:
        self._cook_id: str = cook_id
        self._host: PdgHost = host
        self._clock: Clock = clock
        self._settings: SchedulerSettings = settings
        self._services: CookServices = services
        self._state: _CookStateMachine = _CookStateMachine()
        self._work_items: _WorkItemTracker = _WorkItemTracker()
        # Waiting for PDG to finish generating, before the snapshot.
        self._generation_lock: threading.Lock = threading.Lock()
        self._generation_wait_started_at: Optional[datetime] = None
        self._generation_wait_logged_at: Optional[datetime] = None
        # Tick thread only.
        self._last_polled_at: Optional[datetime] = None
        self._last_heartbeat_at: Optional[datetime] = None
        # Set once by close, which only one thread calls for a given cook.
        self._summary: Optional[CookSummary] = None
        # The journal's handler sees only what the logger lets through, so the
        # logger passes INFO for the life of the cook.
        self._log_handler: logging.Handler = services.journal.log_handler()
        self._package_logger: logging.Logger = logging.getLogger(_PACKAGE_LOGGER)
        self._package_logger_previous_level: int = self._package_logger.level
        if (
            self._package_logger_previous_level == logging.NOTSET
            or self._package_logger_previous_level > logging.INFO
        ):
            self._package_logger.setLevel(logging.INFO)
        self._package_logger.addHandler(self._log_handler)

    def phase(self) -> CookPhase:
        """The current phase."""
        return self._state.phase()

    def start(self) -> None:
        """Enter CONNECTED. Nothing is submitted until PDG offers the first work item.

        A stop that closed the cook meanwhile leaves it closed. Raises when PDG's
        working directory cannot be set, with the cook recorded as FAILED.
        """
        try:
            self._host.set_working_dir(self._settings.working_dir)
        except Exception:
            self._state.move(CookPhase.IDLE, CookPhase.FAILED)
            raise
        if not self._state.move(CookPhase.IDLE, CookPhase.CONNECTED):
            return
        _logger.info(
            "Cook %s started in %s. The job is submitted when PDG offers its first work item.",
            self._cook_id,
            self._settings.working_dir,
        )

    def schedule(self, item: WorkItemHandle) -> ScheduleResult:
        """Submit on the first call, then store the item's payload and release its task.

        Until PDG has generated every step it asked for, the item is deferred and
        PDG offers it again later. A failed upload fails the item, and its task is
        never released.
        """
        if self._state.phase() is CookPhase.CONNECTED and self._waiting_for_generation():
            return ScheduleResult.DEFERRED
        # Only the call that moves CONNECTED on submits. A stop during the
        # submission leaves the cook outside COOKING, and the item then fails
        # like any other offered too late.
        if self._state.move(CookPhase.CONNECTED, CookPhase.SNAPSHOTTING):
            self._submit()
        # Outside COOKING an offer fails and changes nothing.
        running: Optional[_Running] = self._state.running()
        if running is None or running.phase is not CookPhase.COOKING:
            _logger.debug(
                "Work item '%s' was offered while the cook is %s, so it fails.",
                item.name,
                self._state.phase().value,
            )
            return ScheduleResult.FAILED
        return self._release_item(item, running)

    def tick(self) -> None:
        """Poll the farm, report results, and check the job, at most every 5 s. Never raises.

        Each part runs in its own guard, so one failing does not stop the others.
        The heartbeat keeps its own interval.
        """
        reads: _TickReads = _TickReads()
        for part in (
            self._read_state,
            self._report_results,
            self._fail_if_job_cannot_finish,
            self._advance_when_complete,
            self._heartbeat,
        ):
            try:
                part(reads)
            except Exception:
                _logger.exception("The tick failed in %s", part.__name__)

    def close(self, *, cancel: bool) -> CookSummary:
        """End the cook, canceling its job when the artist stopped it.

        When PDG ends the cook itself, the job is marked failed, and the cook ends
        FAILED, if some of its tasks can never be released. Each such task was
        created SUSPENDED, so the job could never finish. That happens when:

        * the cook ended while its job was being started, so no task was released;
        * the cook ended COOKING with work items PDG never offered. A node's own
          preparation can fail before scheduling, as FFmpeg Encode Video does when
          its ffmpeg lacks the requested codec, and an item whose dependency
          failed is never offered either;
        * the cook ended COOKING or DRAINING with an item whose payload could not
          be stored.

        A job whose tasks were all released is kept, since it may still be
        finishing. Any other phase either never had a job or already reached a
        verdict. From the close on, no result is recorded, so the summary counts
        exactly what PDG was told.
        """
        if self._summary is not None:
            return self._summary
        try:
            # Find what can never be released, and log it.
            resolving: bool = not cancel and self._state.phase() is CookPhase.RESOLVING
            never_offered: List[WorkItemSpec] = []
            never_released: List[_OfferedItem] = []
            if resolving:
                _logger.error(
                    "The cook ended while job %s was being started, so none of its tasks can "
                    "run, and the job is marked failed.",
                    self._state.job_id(),
                )
            elif not cancel:
                never_offered, never_released = self._unreleasable_items()
            self._log_never_offered(never_offered)
            for offered in never_released:
                _logger.error(
                    "Work item '%s' of node '%s' could not be stored, so its task can never "
                    "run, and the job is marked failed.",
                    offered.item.name,
                    offered.item.node_name,
                )
            # Recorded as a failure before the close, so the summary says the cook
            # failed rather than that it was still cooking.
            stranded: bool = resolving or bool(never_offered or never_released)
            if stranded:
                self._state.fail()
            # Close the phase and the tracker, so nothing is recorded afterwards.
            final: CookPhase = self._state.close()
            counts: _ItemCounts = self._work_items.close()
            # One cancel or fail per job: the artist's stop is a cancel, and a job
            # that can never finish is failed, so the farm does not show it as
            # stopped by the artist.
            if cancel or stranded:
                job: Optional[FarmJob] = self._state.take_job_to_end()
                if job is not None and cancel:
                    job.cancel()
                elif job is not None:
                    job.fail()
            summary: CookSummary = self._summarize(final, counts, stopped_by_user=cancel)
            self._summary = summary
            self._services.journal.closed(summary)
            return summary
        finally:
            # Hand the package logger back as the cook found it, whatever failed above.
            self._package_logger.removeHandler(self._log_handler)
            if self._package_logger.level != self._package_logger_previous_level:
                self._package_logger.setLevel(self._package_logger_previous_level)

    # -- submission --

    def _waiting_for_generation(self) -> bool:
        """Whether to defer the snapshot, because PDG is still generating a step.

        After :data:`_GENERATION_TIMEOUT` the snapshot goes ahead, and validation
        names any step that still has no items.
        """
        # A state that cannot be read must not hold the cook, so it reads as done.
        try:
            pending: Tuple[str, ...] = tuple(self._host.pending_generation())
        except Exception:
            _logger.warning("Could not tell whether PDG is still generating.", exc_info=True)
            return False
        now: datetime = self._now()
        with self._generation_lock:
            # Generation finished. The caller snapshots at once, so this is the
            # last call that asks.
            if not pending:
                if self._generation_wait_started_at is not None:
                    _logger.info(
                        "PDG generated every step after %.1f s.",
                        (now - self._generation_wait_started_at).total_seconds(),
                    )
                return False
            # The timeout runs from the first deferral.
            if self._generation_wait_started_at is None:
                self._generation_wait_started_at = now
            if now - self._generation_wait_started_at >= _GENERATION_TIMEOUT:
                _logger.warning(
                    "PDG has not generated %s after %d s. Reading the graph anyway.",
                    ", ".join(pending),
                    _GENERATION_TIMEOUT.total_seconds(),
                )
                return False
            # PDG offers items many times a second, so the wait is logged sparingly.
            if (
                self._generation_wait_logged_at is None
                or now - self._generation_wait_logged_at >= _GENERATION_LOG_INTERVAL
            ):
                self._generation_wait_logged_at = now
                _logger.info(
                    "Waiting for PDG to generate %s before reading the graph.", ", ".join(pending)
                )
        return True

    def _submit(self) -> None:
        """Read the graph, create the job, and start it, unless the cook stops first.

        Raises :class:`~deadline.houdini_pdg.errors.GraphRefusedError` for a refused
        graph, and the cause for any other failure. A failure after the cook
        stopped belongs to no open cook, so it is only logged.
        """
        # Each step moves on from the phase the one before it set, so a stop in
        # between makes its move fail, and the step returns None.
        try:
            # The snapshot, then the created job, then the started job.
            snapshot: Optional[GraphSnapshot] = self._read_graph()
            if snapshot is None:
                return
            created: Optional[Tuple[str, FarmJob]] = self._create_job(snapshot)
            if created is None:
                return
            self._start_job(*created)
        except GraphRefusedError:
            raise
        except Exception as error:
            # CreateJob refusing the job for a quota created nothing on the farm,
            # so the cook is refused rather than failed.
            if isinstance(error, QuotaExceededError) and self._state.refuse(
                (RefusalScenario.QUOTA_EXCEEDED,), from_phase=CookPhase.SUBMITTING
            ):
                _logger.error("The job was refused by a service quota.", exc_info=True)
                raise
            # Any other failure fails the cook from the step it reached. Only a
            # closed cook cannot fail here.
            if not self._state.fail():
                _logger.warning(
                    "The cook stopped while it was submitting, and the submission then failed.",
                    exc_info=True,
                )
                return
            _logger.error("The cook could not start its job.", exc_info=True)
            # A job exists only when its start failed. Its tasks are suspended, and
            # nothing would release them.
            job: Optional[FarmJob] = self._state.take_job_to_end()
            if job is not None:
                job.fail()
            raise

    def _read_graph(self) -> Optional[GraphSnapshot]:
        """Read and validate the graph, and record its snapshot. None once the cook stopped.

        Raises :class:`~deadline.houdini_pdg.errors.GraphRefusedError` for a refused graph.
        """
        # Read the graph and the scene, and record what was read.
        nodes: Tuple[NodeInfo, ...] = tuple(self._host.read_graph())
        scene: SceneReads = self._host.read_scene()
        snapshot: GraphSnapshot
        report: Optional[ReadReport]
        snapshot, report = self._build_snapshot(nodes, scene)
        self._services.journal.snapshot_taken(nodes, snapshot, report)
        # Validate, or keep every finding as a warning when the artist turned
        # validation off.
        found: Verdict = validate(
            ValidationContext(
                nodes=nodes,
                snapshot=snapshot,
                reads=report,
                working_dir=self._settings.working_dir,
                env=self._environment(),
            )
        )
        verdict: Verdict = found if self._settings.validate_graph else waived(found)
        self._log_validation_verdict(verdict)
        # A refused graph submits nothing. Each scenario is recorded once.
        if verdict.refusals:
            scenarios: Tuple[RefusalScenario, ...] = tuple(
                dict.fromkeys(r.scenario for r in verdict.refusals)
            )
            if self._state.refuse(scenarios, from_phase=CookPhase.SNAPSHOTTING):
                raise GraphRefusedError(verdict)
            return None
        _logger.info(
            "Snapshot: %d step(s) and %d task(s). Nodes that are not steps: %s.",
            len(snapshot.steps),
            snapshot.total_tasks,
            ", ".join(snapshot.skipped_nodes) or "none",
        )
        # The tracker learns the task count before any item can be offered,
        # because offers need COOKING.
        if not self._state.record_snapshot(snapshot):
            return None
        self._work_items.set_expected_count(snapshot.total_tasks)
        return snapshot

    def _create_job(self, snapshot: GraphSnapshot) -> Optional[Tuple[str, FarmJob]]:
        """Build the bundle into the journal's directory, and create the job. None once the cook stopped."""
        bundle_dir: str = self._services.journal.bundle_dir()
        if not self._state.move(CookPhase.BUILDING, CookPhase.SUBMITTING):
            return None
        job_id: str = self._services.submitter.submit(snapshot, bundle_dir)
        # Without the job's handle the cook cannot end the job, so the log names it.
        try:
            job: FarmJob = self._services.open_job(job_id, snapshot)
        except Exception:
            _logger.error(
                "Job %s was created, but the cook could not open it. Cancel it on the farm.",
                job_id,
            )
            raise
        if not self._state.record_job(job_id, job):
            # Nothing will ever drive the job, so it must not wait on the farm.
            _logger.warning("The cook stopped while job %s was created. Canceling it.", job_id)
            job.cancel()
            return None
        return job_id, job

    def _start_job(self, job_id: str, job: FarmJob) -> None:
        """Start the job, then enter COOKING unless the cook stopped meanwhile."""
        self._services.journal.job_submitted(job_id)
        # Learn the job's tasks and suspend its dependent steps.
        job.start()
        if self._state.move(CookPhase.RESOLVING, CookPhase.COOKING):
            _logger.info("Cooking job %s.", job_id)

    def _build_snapshot(
        self, nodes: Tuple[NodeInfo, ...], scene: SceneReads
    ) -> Tuple[GraphSnapshot, Optional[ReadReport]]:
        """The job's shape, with the data edges the scene's reads add."""
        provisional: GraphSnapshot = synthesize(nodes)
        if not scene.available:
            # Validation is told the walk did not run, so it says which checks it skipped.
            _logger.warning("The scene walk did not finish, so no data edges are added.")
            return provisional, None
        report: ReadReport = match_reads(provisional, scene.reads, self._settings.working_dir)
        return synthesize(nodes, data_edges=report.data_edges), report

    def _environment(self) -> Dict[str, str]:
        """This process's environment, with ``HIP`` from the saved scene when it is unset."""
        env: Dict[str, str] = dict(os.environ)
        if not env.get("HIP"):
            hip_file: str = self._host.scene_files().hip_file
            if hip_file:
                env["HIP"] = os.path.dirname(hip_file)
        return env

    @staticmethod
    def _log_validation_verdict(verdict: Verdict) -> None:
        for advisory in verdict.advisories:
            _logger.warning(
                "%s (%s): %s", advisory.subject, advisory.scenario.value, advisory.detail
            )
        for refusal in verdict.refusals:
            _logger.error(
                "Refused, %s (%s): %s %s",
                refusal.subject,
                refusal.scenario.value,
                refusal.detail,
                refusal.remedy,
            )

    # -- release --

    def _release_item(self, item: WorkItemHandle, running: _Running) -> ScheduleResult:
        """Record the offer, store the item's payload, release its task, then tell PDG it started."""
        # An item the snapshot lacks has no task to run on.
        spec: Optional[WorkItemSpec] = running.snapshot.item_by_name(item.name)
        if spec is None:
            _logger.error(
                "Work item '%s' of node '%s' has no task in the job: PDG generated it after "
                "the snapshot.",
                item.name,
                item.node_name,
            )
            return ScheduleResult.FAILED
        # Each item is recorded once, and none once the job has failed. A second
        # offer of an item is PDG's mistake; an offer after the job failed is not.
        key: TaskKey = TaskKey(spec.node_name, spec.task_index)
        if not self._work_items.record_offer(key, _OfferedItem(item, spec, self._now())):
            if self._work_items.was_offered(key):
                _logger.error("Work item '%s' was offered twice.", item.name)
            else:
                _logger.debug("Work item '%s' was offered after the job ended.", item.name)
            return ScheduleResult.FAILED
        self._services.journal.item_scheduled(item)
        # Store the payload before the release, so the task never runs without it.
        try:
            self._services.storage.put_payload(running.job.address(key), self._host.serialize(item))
        except Exception as e:
            # Recorded as failed here, so a later result for the task is ignored.
            self._work_items.record_unreleased(key)
            _logger.error("Could not store the data of work item '%s': %s", item.name, e)
            return ScheduleResult.FAILED
        running.job.release(key)
        try:
            self._host.notify(item, ItemOutcome(ItemState.STARTED))
        except Exception:
            _logger.exception("Could not tell PDG that work item '%s' started", item.name)
        # A job failure that came after the offer is reported now, after STARTED.
        if self._work_items.mark_started(key):
            return ScheduleResult.FAILED
        return ScheduleResult.SUCCEEDED

    # -- tick --

    def _read_state(self, reads: _TickReads) -> None:
        """Read the cook's job, and whether this tick polls the farm."""
        reads.running = self._state.running()
        if reads.running is None:
            return
        # The first tick with a job polls at once, and later ticks every _POLL_INTERVAL.
        now: datetime = self._now()
        if self._last_polled_at is None or now - self._last_polled_at >= _POLL_INTERVAL:
            self._last_polled_at = now
            reads.polls = True

    def _report_results(self, reads: _TickReads) -> None:
        """Report each new result to PDG once. A canceled task is counted, never reported."""
        # Once every item has reported, polling has nothing left to find.
        running: Optional[_Running] = reads.running
        if running is None or not reads.polls or running.phase is not CookPhase.COOKING:
            return
        # Results that waited for their item's STARTED go first. They are taken
        # after the poll, so a poll that raises leaves them for the next tick.
        # One outcome failing to report must not drop the rest.
        polled: List[TaskOutcome] = running.job.poll()
        for outcome in self._work_items.take_waiting() + polled:
            try:
                key: TaskKey = outcome.key
                if outcome.status is TaskStatus.CANCELED:
                    # The farm stopped the task, usually because the job stopped. That
                    # is no verdict on the work item, and the job check ends the cook.
                    if self._work_items.record_cancellation(key):
                        _logger.debug("Task %s[%d] was canceled.", key.step_name, key.task_index)
                    continue
                succeeded: bool = outcome.status is TaskStatus.SUCCEEDED
                offered: Optional[_OfferedItem] = self._work_items.record_result(
                    key, succeeded=succeeded
                )
                if offered is None:
                    continue
                if succeeded:
                    self._report_success(running.job, key, offered)
                else:
                    self._report_failure(running.job, key, offered)
            except Exception:
                _logger.exception("Could not report the result of %s", outcome.key)

    def _report_success(self, job: FarmJob, key: TaskKey, offered: _OfferedItem) -> None:
        """Tell PDG the item succeeded, with its outputs, or that it failed if PDG raises.

        Without a result PDG would wait on the item for ever. A retry could add its
        outputs twice, so the success is never retried.
        """
        # Report the success with its outputs and the time since the offer.
        duration: float = self._seconds_since(offered.offered_at)
        outputs: Tuple[OutputFile, ...] = self._outputs_of(job, key, offered)
        try:
            self._host.notify(offered.item, ItemOutcome(ItemState.SUCCEEDED, outputs, duration))
            return
        except Exception:
            _logger.exception(
                "Could not tell PDG that work item '%s' succeeded. Reporting it failed",
                offered.item.name,
            )
        # PDG rejected the success, so the item is counted failed and PDG told so, once.
        self._work_items.record_delivery_failed(key)
        self._host.notify(offered.item, ItemOutcome(ItemState.FAILED, duration_seconds=duration))

    def _report_failure(self, job: FarmJob, key: TaskKey, offered: _OfferedItem) -> None:
        """Log the failure with the worker's last lines, then tell PDG."""
        _logger.error(
            "Work item '%s' failed (task %s[%d]).",
            offered.item.name,
            key.step_name,
            key.task_index,
        )
        self._services.journal.item_failed(key, lambda: job.log_tail(key, _LOG_TAIL_LINES))
        self._host.notify(
            offered.item,
            ItemOutcome(ItemState.FAILED, duration_seconds=self._seconds_since(offered.offered_at)),
        )

    def _outputs_of(
        self, job: FarmJob, key: TaskKey, offered: _OfferedItem
    ) -> Tuple[OutputFile, ...]:
        """Declared outputs when PDG knows them, which costs nothing, else the task's own record."""
        # Read at onSchedule first: some nodes declare their outputs only then.
        declared: Tuple[DeclaredFile, ...] = (
            offered.item.declared_outputs or offered.spec.declared_outputs
        )
        if declared:
            return tuple(OutputFile(self._local_path(d.path), d.tag) for d in declared)
        # Otherwise the task published what it wrote. A record that cannot be read
        # reports no outputs rather than failing an item that succeeded.
        try:
            recorded: Optional[List[OutputFile]] = self._services.storage.get_outputs(
                job.address(key)
            )
        except Exception as e:
            _logger.warning("Could not read the outputs of '%s': %s", offered.item.name, e)
            return ()
        if recorded is None:
            _logger.debug("Work item '%s' published no outputs.", offered.item.name)
            return ()
        return tuple(OutputFile(self._local_path(o.path), o.tag) for o in recorded)

    def _local_path(self, path: str) -> str:
        return path.replace(PDG_DIR_TOKEN, self._settings.working_dir)

    def _fail_if_job_cannot_finish(self, reads: _TickReads) -> None:
        """Fail the cook, and every item still waiting, once the job can make no more progress."""
        running: Optional[_Running] = reads.running
        if running is None or not reads.polls:
            return
        status: JobStatus = running.job.status()
        reads.status = status
        if (
            status.lifecycle_status not in _JOB_LIFECYCLE_FAILURES
            and status.task_run_status not in _JOB_RUN_FAILURES
        ):
            return
        # Only the tick that moves the cook to FAILED fails the unreported items,
        # so each fails once.
        if not self._state.fail():
            return
        counts: str = ", ".join(
            f"{k}={v}" for k, v in sorted(status.task_run_status_counts.items()) if v
        )
        outstanding: List[_OfferedItem] = self._work_items.fail_unreported()
        _logger.error(
            "The job can make no more progress (lifecycle %s, tasks %s%s). Failing %d "
            "outstanding work item(s).",
            status.lifecycle_status or "?",
            status.task_run_status or "?",
            f": {counts}" if counts else "",
            len(outstanding),
        )
        # One item PDG rejects must not stop the others from failing.
        for offered in outstanding:
            try:
                self._host.notify(offered.item, ItemOutcome(ItemState.FAILED))
            except Exception:
                _logger.exception("Could not fail work item '%s'", offered.item.name)

    def _advance_when_complete(self, reads: _TickReads) -> None:
        """Drain once every item has reported, and finish once the job has succeeded."""
        if self._work_items.all_reported() and self._state.move(
            CookPhase.COOKING, CookPhase.DRAINING
        ):
            counts: _ItemCounts = self._work_items.counts()
            _logger.info(
                "All %d work item(s) reported: %d succeeded, %d failed.",
                counts.succeeded + counts.failed,
                counts.succeeded,
                counts.failed,
            )
        if (
            reads.status is not None
            and reads.status.task_run_status == "SUCCEEDED"
            and self._state.move(CookPhase.DRAINING, CookPhase.DONE)
        ):
            _logger.info("The job succeeded.")

    def _heartbeat(self, reads: _TickReads) -> None:
        """Log the cook's progress at most every :data:`_HEARTBEAT_INTERVAL`."""
        running: Optional[_Running] = reads.running
        now: datetime = self._now()
        if running is None or (
            self._last_heartbeat_at is not None
            and now - self._last_heartbeat_at < _HEARTBEAT_INTERVAL
        ):
            return
        # Offered and not yet reported means waiting on the farm, or on the upload.
        self._last_heartbeat_at = now
        counts: _ItemCounts = self._work_items.counts()
        reported: int = counts.succeeded + counts.failed
        _logger.info(
            "%s: %d of %d work item(s) reported (%d succeeded, %d failed), %d offered and "
            "waiting for a result, %d canceled.",
            running.phase.value,
            reported,
            running.snapshot.total_tasks,
            counts.succeeded,
            counts.failed,
            counts.offered - reported,
            counts.canceled,
        )

    # -- close --

    def _unreleasable_items(self) -> Tuple[List[WorkItemSpec], List[_OfferedItem]]:
        """The job's work items PDG never offered, and the offered ones whose task was never released.

        Only a cook still COOKING is owed offers, and only one COOKING or DRAINING
        can hold an unreleased task.
        """
        # A cook with no job to watch has nothing to release.
        running: Optional[_Running] = self._state.running()
        if running is None:
            return [], []
        # The snapshot's items PDG has not offered, while it still owes offers.
        offered: Set[TaskKey] = self._work_items.offered_keys()
        never_offered: List[WorkItemSpec] = (
            [
                spec
                for step in running.snapshot.steps
                for spec in step.items
                if TaskKey(spec.node_name, spec.task_index) not in offered
            ]
            if running.phase is CookPhase.COOKING
            else []
        )
        return never_offered, self._work_items.never_released()

    def _log_never_offered(self, specs: List[WorkItemSpec]) -> None:
        """Name the first never-offered items with PDG's own reason, then count the rest by node."""
        for spec in specs[:_NAMED_NEVER_OFFERED]:
            # Only an ERROR line is PDG's reason. Any other last line may be unrelated.
            error: str
            last_line: str
            error, last_line = self._host.failure_reason(spec.item_id)
            why: str = (
                f"PDG's reason: {error}"
                if error
                else (
                    f"PDG logged no error for it. Its last log line: {last_line}"
                    if last_line
                    else "PDG logged nothing for it."
                )
            )
            _logger.error(
                "Work item '%s' of node '%s' was never offered to the Deadline Cloud Scheduler "
                "node, so its task can never run, and the job is marked failed. %s",
                spec.name,
                spec.node_name,
                why,
            )
        # The rest get one line per node, in the snapshot's order.
        rest: Dict[str, int] = {}
        for spec in specs[_NAMED_NEVER_OFFERED:]:
            rest[spec.node_name] = rest.get(spec.node_name, 0) + 1
        for node_name, count in rest.items():
            _logger.error(
                "%d more work item(s) of node '%s' were never offered to the Deadline Cloud "
                "Scheduler node.",
                count,
                node_name,
            )

    def _summarize(
        self, final: CookPhase, counts: _ItemCounts, *, stopped_by_user: bool
    ) -> CookSummary:
        """The summary of the closed cook, also logged for the artist."""
        # A cook refused or stopped before its snapshot counts no steps or tasks.
        snapshot: Optional[GraphSnapshot] = self._state.snapshot()
        summary: CookSummary = CookSummary(
            cook_id=self._cook_id,
            final_phase=final,
            job_id=self._state.job_id(),
            step_count=len(snapshot.steps) if snapshot else 0,
            task_count=snapshot.total_tasks if snapshot else 0,
            succeeded=counts.succeeded,
            failed=counts.failed,
            canceled=counts.canceled,
            refusals=self._state.refusals(),
            stopped_by_user=stopped_by_user,
        )
        # One line the artist reads, with the counts PDG was told.
        _logger.info(
            "The cook ended %s%s: %d of %d work item(s) reported, %d succeeded, %d failed, "
            "%d task(s) canceled by the farm.",
            final.value,
            f" with job {summary.job_id}" if summary.job_id else "",
            summary.succeeded + summary.failed,
            summary.task_count,
            summary.succeeded,
            summary.failed,
            summary.canceled,
        )
        return summary

    # -- helpers --

    def _seconds_since(self, then: datetime) -> float:
        # A clock that steps back must not show a negative duration.
        return max(0.0, (self._now() - then).total_seconds())

    def _now(self) -> datetime:
        now: datetime = self._clock.now()
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


class PdgScheduler:
    """What the PDG callbacks reach. Holds at most one open :class:`Cook`."""

    def __init__(
        self,
        *,
        host: PdgHost,
        clock: Clock,
        read_settings: Callable[[], SchedulerSettings],
        connect: Callable[[SchedulerSettings, str], CookServices],
    ) -> None:
        self._host: PdgHost = host
        self._clock: Clock = clock
        self._read_settings: Callable[[], SchedulerSettings] = read_settings
        # Called with the settings and the cook's ID, so the journal, the job,
        # and the summary name the cook alike.
        self._connect: Callable[[SchedulerSettings, str], CookServices] = connect
        # Every field below is read and written only under the lock.
        self._lock: threading.Lock = threading.Lock()
        self._cook: Optional[Cook] = None
        # A generate only generates work items. It has no Cook, journal, or farm.
        self._generate_only: bool = False
        # A start is running, so a second start does not connect a second cook.
        self._start_in_progress: bool = False
        # How many stops have come, and whether the latest asked to cancel. A start
        # compares the count with the one it began with to learn of a stop meanwhile.
        self._stop_count: int = 0
        self._last_stop_canceled: bool = False

    def on_start_cook(
        self, generate_only: bool, cook_set: Optional[AbstractSet[str]] = None
    ) -> bool:
        """Start a cook. A generate needs no farm, and only sets the working directory.

        ``generate_only`` is PDG's ``static``. A stop that arrives while the start
        runs ends the cook being started.
        """
        # Only one start runs at a time, and only with no cook open.
        with self._lock:
            if self._cook is not None or self._generate_only or self._start_in_progress:
                _logger.warning(
                    "A cook started while another is open or starting. The new start is ignored."
                )
                return True
            self._start_in_progress = True
            stop_count: int = self._stop_count
        try:
            return self._start_cook(generate_only, cook_set, stop_count)
        finally:
            with self._lock:
                self._start_in_progress = False

    def _start_cook(
        self, generate_only: bool, cook_set: Optional[AbstractSet[str]], stop_count: int
    ) -> bool:
        settings: SchedulerSettings = self._read_settings()
        if generate_only:
            # PDG still copies its job scripts into the working directory. A stop
            # that came meanwhile has already ended the generate.
            self._host.set_working_dir(settings.working_dir)
            with self._lock:
                if self._stop_count == stop_count:
                    self._generate_only = True
            _logger.info("Generating work items only: no job is submitted.")
            return True
        # A farm cook gets a fresh ID and its own services, then enters CONNECTED.
        cook_id: str = uuid.uuid4().hex
        services: CookServices = self._connect(settings, cook_id)
        cook: Cook = Cook(
            cook_id=cook_id,
            host=self._host,
            clock=self._clock,
            settings=settings,
            services=services,
        )
        # The cook is open before it starts, so a stop from now on closes it. A stop
        # that came while connecting found nothing to close, so it is closed here.
        with self._lock:
            stopped: bool = self._stop_count != stop_count
            cancel: bool = self._last_stop_canceled
            if not stopped:
                self._cook = cook
        if stopped:
            _logger.info("The cook was stopped while it connected to the farm.")
            cook.close(cancel=cancel)
            return True
        if cook_set is not None:
            _logger.info("PDG asked to cook %d node(s).", len(cook_set))
        try:
            cook.start()
        except Exception:
            # PDG drops a scheduler node whose start failed, so the cook is closed here,
            # unless a stop already took it and closed it.
            if self._claim_close(cook):
                cook.close(cancel=False)
            raise
        try:
            self._host.watch_generation(cook_set)
        except Exception:
            # Without it the first work item reads the graph at once.
            _logger.warning("Could not follow PDG's generation of the graph.", exc_info=True)
        # A stop meanwhile may have stopped watching before the watch began.
        with self._lock:
            still_open: bool = self._cook is cook
        if not still_open:
            self._stop_watching_generation()
        return True

    def _claim_close(self, cook: Cook) -> bool:
        """Remove ``cook`` if it is the open one, so exactly one caller closes it."""
        with self._lock:
            if self._cook is not cook:
                return False
            self._cook = None
            return True

    def _stop_watching_generation(self) -> None:
        try:
            self._host.stop_watching_generation()
        except Exception:
            _logger.exception("Could not stop following PDG's generation")

    def on_schedule(self, item: WorkItemHandle) -> ScheduleResult:
        """Hand a work item to the open cook, or fail it when there is none."""
        with self._lock:
            cook: Optional[Cook] = self._cook
        if cook is None:
            _logger.error("Work item '%s' was offered with no farm cook open.", item.name)
            return ScheduleResult.FAILED
        return cook.schedule(item)

    def on_tick(self) -> None:
        """Advance the open cook. Never raises."""
        try:
            with self._lock:
                cook: Optional[Cook] = self._cook
            if cook is not None:
                cook.tick()
        except Exception:
            _logger.exception("The tick failed")

    def on_stop_cook(self, cancel: bool) -> bool:
        """Close the open cook, canceling its job when asked.

        A start still running learns of the stop, and ends the cook it is starting.
        """
        # Drop the cook first, so the next start builds a fresh one.
        with self._lock:
            cook: Optional[Cook]
            cook, self._cook = self._cook, None
            self._generate_only = False
            # A start still running compares the count with its own to learn of this stop.
            self._stop_count += 1
            self._last_stop_canceled = cancel
        self._stop_watching_generation()
        if cook is not None:
            try:
                cook.close(cancel=cancel)
            except Exception:
                _logger.exception("Could not close the cook")
        return True


class SystemClock:
    """The production :class:`~deadline.houdini_pdg.ports.Clock`."""

    def now(self) -> datetime:
        """Timezone-aware, in UTC."""
        return datetime.now(timezone.utc)
