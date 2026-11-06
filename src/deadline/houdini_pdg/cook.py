# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler's core: one cook at a time, from ``onStartCook`` to ``onStopCook``.

PDG calls the scheduler from three threads. Each cook is a new :class:`Cook`, so
nothing carries over from the previous one, and every phase change is a
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

#: The logger every module of the scheduler logs under. The cook's journal
#: handler is attached here for the life of the cook.
_PACKAGE_LOGGER: str = "deadline.houdini_pdg"

_HEARTBEAT_INTERVAL: timedelta = timedelta(seconds=30)

# How long the first work item may wait for PDG to generate the rest of the graph.
# Generation can wait on in-process nodes upstream, such as a Wedge.
_GENERATION_TIMEOUT: timedelta = timedelta(minutes=5)
_GENERATION_LOG_INTERVAL: timedelta = timedelta(seconds=10)
_LOG_TAIL_LINES: int = 50

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
#: never reused: the scheduler builds a new one for the next cook, and
#: :attr:`~deadline.houdini_pdg.ports.CookSummary.final_phase` keeps the phase it
#: closed from. An artist's cancel is a stop, not a phase.
ALLOWED_TRANSITIONS: Mapping[CookPhase, FrozenSet[CookPhase]] = MappingProxyType(
    {
        CookPhase.IDLE: _allowed(CookPhase.CONNECTED, CookPhase.DONE, CookPhase.FAILED),
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
    #: Opens the submitted job by its ID.
    open_job: Callable[[str, GraphSnapshot], FarmJob]
    journal: CookJournal


class _Control:
    """The phase, and what the submission produced. Every change is a compare-and-set.

    Once closed, nothing advances, so a submission that outlives its cook learns
    that from the next advance it attempts.
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
        self._cancel_claimed: bool = False

    def phase(self) -> CookPhase:
        with self._lock:
            return self._phase

    def advance(self, to: CookPhase, *, expect: CookPhase) -> bool:
        with self._lock:
            return self._advance(to, expect)

    def adopt_snapshot(self, snapshot: GraphSnapshot) -> bool:
        """Record the snapshot and move SNAPSHOTTING to BUILDING."""
        with self._lock:
            moved: bool = self._advance(CookPhase.BUILDING, CookPhase.SNAPSHOTTING)
            if moved:
                self._snapshot = snapshot
            return moved

    def refuse(self, refusals: Tuple[RefusalScenario, ...], *, expect: CookPhase) -> bool:
        with self._lock:
            moved: bool = self._advance(CookPhase.REFUSED, expect)
            if moved:
                self._refusals = refusals
            return moved

    def adopt_job(self, job_id: str, job: FarmJob) -> bool:
        """Record the created job and move SUBMITTING to RESOLVING, or refuse it once closed."""
        with self._lock:
            moved: bool = self._advance(CookPhase.RESOLVING, CookPhase.SUBMITTING)
            if moved:
                self._job_id, self._job = job_id, job
            return moved

    def claim_cancel(self) -> Optional[FarmJob]:
        """The job to cancel or fail, the first time only, so it gets one such call."""
        with self._lock:
            if self._job is None or self._cancel_claimed:
                return None
            self._cancel_claimed = True
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

    def job(self) -> Tuple[str, Optional[FarmJob]]:
        with self._lock:
            return self._job_id, self._job

    def refusals(self) -> Tuple[RefusalScenario, ...]:
        with self._lock:
            return self._refusals

    def history(self) -> Tuple[Tuple[CookPhase, CookPhase], ...]:
        with self._lock:
            return tuple(self._history)

    def _advance(self, to: CookPhase, expect: CookPhase) -> bool:
        # Only close() reaches IDLE, and nothing moves once closed, when the
        # phase is no longer the one the caller saw, or outside the table.
        moved: bool = (
            not self._closed
            and self._phase is expect
            and to is not CookPhase.IDLE
            and to in ALLOWED_TRANSITIONS[expect]
        )
        if moved:
            self._history.append((expect, to))
            self._phase = to
        return moved


@dataclass(frozen=True)
class _Entry:
    """A work item PDG offered, and what reporting it needs."""

    item: WorkItemHandle
    spec: WorkItemSpec
    registered_at: datetime


@dataclass(frozen=True)
class _Tallies:
    """The ledger's counts at one moment, read together under its lock."""

    #: The snapshot's task count, and how many items PDG offered.
    expected: int
    registered: int
    #: Items reported to PDG, by result. Canceled tasks are never reported.
    succeeded: int
    failed: int
    canceled: int

    @property
    def reported(self) -> int:
        return self.succeeded + self.failed


class _Ledger:
    """The items PDG offered, and which have had their one result. PDG's threads share it."""

    def __init__(self) -> None:
        # Every field below is read and written only under the lock.
        self._lock: threading.Lock = threading.Lock()
        self._expected: int = 0
        self._entries: Dict[TaskKey, _Entry] = {}
        # Reported keys, and whether each succeeded. Always a subset of _entries.
        self._reported: Dict[TaskKey, bool] = {}
        self._canceled: Set[TaskKey] = set()
        # Keys reported failed because their payload could not be stored. Their
        # tasks were never released.
        self._unreleased: Dict[TaskKey, _Entry] = {}

    def expect(self, count: int) -> None:
        with self._lock:
            self._expected = count

    def register(self, key: TaskKey, entry: _Entry) -> bool:
        with self._lock:
            if key in self._entries:
                return False
            self._entries[key] = entry
            return True

    def claim(self, key: TaskKey, *, succeeded: bool) -> Optional[_Entry]:
        """The entry to report, once. None for a reported key, and for a key PDG never offered.

        An unoffered key claims nothing, so its item can still report once offered.
        """
        with self._lock:
            entry: Optional[_Entry] = self._entries.get(key)
            if entry is None or key in self._reported:
                return None
            self._reported[key] = succeeded
            return entry

    def hold_back(self, key: TaskKey) -> None:
        """Report an offered key failed because its task will never be released.

        One step, so a result the farm returns meanwhile cannot claim it first.
        """
        with self._lock:
            entry: Optional[_Entry] = self._entries.get(key)
            if entry is None or key in self._reported:
                return
            self._reported[key] = False
            self._unreleased[key] = entry

    def unreleased(self) -> List[_Entry]:
        """The items whose tasks were never released, in the order they were held back."""
        with self._lock:
            return list(self._unreleased.values())

    def fail_outstanding(self) -> List[_Entry]:
        """Mark every unreported item failed, and return them."""
        with self._lock:
            outstanding: List[_Entry] = [
                e for k, e in self._entries.items() if k not in self._reported
            ]
            for key in self._entries:
                self._reported.setdefault(key, False)
            return outstanding

    def note_canceled(self, key: TaskKey) -> bool:
        with self._lock:
            if key in self._canceled:
                return False
            self._canceled.add(key)
            return True

    def is_complete(self) -> bool:
        with self._lock:
            return self._expected > 0 and len(self._reported) >= self._expected

    def offered(self) -> Set[TaskKey]:
        """Every key PDG offered, reported or not."""
        with self._lock:
            return set(self._entries)

    def tallies(self) -> _Tallies:
        with self._lock:
            succeeded: int = sum(1 for ok in self._reported.values() if ok)
            return _Tallies(
                expected=self._expected,
                registered=len(self._entries),
                succeeded=succeeded,
                failed=len(self._reported) - succeeded,
                canceled=len(self._canceled),
            )


class Cook:
    """One cook.

    The first :meth:`schedule` snapshots, validates, submits, and starts the job;
    later calls hand items to the farm. :meth:`tick` releases, polls, and reports.
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
        self._control: _Control = _Control()
        self._ledger: _Ledger = _Ledger()
        # Waiting for PDG to finish generating, before the snapshot.
        self._generation_lock: threading.Lock = threading.Lock()
        self._waiting_since: Optional[datetime] = None
        self._waiting_logged_at: Optional[datetime] = None
        # Tick thread only.
        self._job_status: JobStatus = JobStatus()
        self._heartbeat_at: Optional[datetime] = None
        # Stop thread only.
        self._summary: Optional[CookSummary] = None
        # The journal's handler sees only what the logger lets through, so the
        # logger passes INFO for the life of the cook.
        self._log_handler: logging.Handler = services.journal.log_handler()
        self._package_logger: logging.Logger = logging.getLogger(_PACKAGE_LOGGER)
        self._previous_level: int = self._package_logger.level
        if self._previous_level == logging.NOTSET or self._previous_level > logging.INFO:
            self._package_logger.setLevel(logging.INFO)
        self._package_logger.addHandler(self._log_handler)

    def phase(self) -> CookPhase:
        """The current phase."""
        return self._control.phase()

    def schedule(self, item: WorkItemHandle) -> ScheduleResult:
        """Submit on the first call, then store the item's payload and release its task.

        Until PDG has generated every step it asked for, the item is deferred and
        PDG offers it again later. A failed upload fails the item, and its task is
        never released.
        """
        if self._control.phase() is CookPhase.CONNECTED and self._still_generating():
            return ScheduleResult.DEFERRED
        # Only the call that moves CONNECTED on submits. Any other call outside
        # COOKING fails the item and changes nothing.
        if self._control.advance(CookPhase.SNAPSHOTTING, expect=CookPhase.CONNECTED):
            # A stop during the submission leaves the cook outside COOKING, and
            # the item then fails like any other offered too late.
            self._submit()
            if self._control.phase() is not CookPhase.COOKING:
                return ScheduleResult.FAILED
        elif self._control.phase() is not CookPhase.COOKING:
            _logger.debug(
                "Work item '%s' was offered while the cook is %s, so it fails.",
                item.name,
                self._control.phase().value,
            )
            return ScheduleResult.FAILED
        return self._dispatch(item)

    def tick(self) -> None:
        """Release, poll, report, and check the job's health. Never raises.

        Each part runs in its own guard, so one failing does not stop the others.
        """
        try:
            # Read the phase and the job once. Before COOKING there is nothing to poll.
            phase: CookPhase = self._control.phase()
            if phase not in (CookPhase.COOKING, CookPhase.DRAINING):
                return
            job: Optional[FarmJob]
            _, job = self._control.job()
            if job is None:
                return
        except Exception:
            _logger.exception("The tick could not read the cook's state")
            return
        # Once every item has reported, polling has nothing left to find.
        farm_job: FarmJob = job
        if phase is CookPhase.COOKING:
            self._guarded("reporting", lambda: self._report(farm_job))
        self._guarded("checking the job", lambda: self._check_job(farm_job))
        self._guarded("checking completion", self._check_completion)
        self._guarded("the heartbeat", self._heartbeat)

    def close(self, *, cancel: bool) -> CookSummary:
        """End the cook, cancelling its job when asked.

        A cook PDG ends, without a stop by the artist, while some of the job's
        tasks can never be released also fails the job and ends FAILED (see
        :meth:`_stranded`). That includes a cook that ended while its job was
        being started.
        """
        if self._summary is not None:
            return self._summary
        # A job with tasks nothing will release can never finish. Recorded as a
        # failure before the close, so the summary says the cook failed rather
        # than that it was still cooking.
        stranded: bool = not cancel and self._stranded()
        final: CookPhase = self._control.close()
        # One cancel or fail per job: the artist's stop is a cancel, and a job that
        # can never finish is failed, so the farm does not show it as stopped by
        # the artist.
        if cancel or stranded:
            job: Optional[FarmJob] = self._control.claim_cancel()
            if job is not None:
                if cancel:
                    self._cancel(job)
                else:
                    self._fail(job)
        # Build the summary from what the cook recorded.
        job_id: str
        job_id, _ = self._control.job()
        snapshot: Optional[GraphSnapshot] = self._control.snapshot()
        tallies: _Tallies = self._ledger.tallies()
        summary: CookSummary = CookSummary(
            cook_id=self._cook_id,
            final_phase=final,
            job_id=job_id,
            step_count=len(snapshot.steps) if snapshot else 0,
            task_count=snapshot.total_tasks if snapshot else 0,
            succeeded=tallies.succeeded,
            failed=tallies.failed,
            canceled=tallies.canceled,
            refusals=self._control.refusals(),
            stopped_by_user=cancel,
        )
        self._summary = summary
        _logger.info(
            "The cook ended %s%s: %d of %d work item(s) reported, %d succeeded, %d failed, "
            "%d task(s) canceled by the farm.",
            final.value,
            f" with job {job_id}" if job_id else "",
            tallies.reported,
            tallies.expected,
            tallies.succeeded,
            tallies.failed,
            tallies.canceled,
        )
        try:
            self._services.journal.closed(summary)
        except Exception:
            _logger.exception("Could not record the end of the cook")
        # Hand the package logger back as the cook found it.
        self._package_logger.removeHandler(self._log_handler)
        if self._package_logger.level != self._previous_level:
            self._package_logger.setLevel(self._previous_level)
        return summary

    # -- start --

    def _begin(self) -> None:
        """Enter CONNECTED. Nothing is submitted until PDG offers the first work item.

        A stop that closed the cook meanwhile leaves it closed.
        """
        try:
            self._host.set_working_dir(self._settings.working_dir)
        except Exception:
            self._control.advance(CookPhase.FAILED, expect=CookPhase.IDLE)
            raise
        if not self._control.advance(CookPhase.CONNECTED, expect=CookPhase.IDLE):
            return
        _logger.info(
            "Cook %s started in %s. The job is submitted when PDG offers its first work item.",
            self._cook_id,
            self._settings.working_dir,
        )

    # -- submission --

    def _still_generating(self) -> bool:
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
                if self._waiting_since is not None:
                    _logger.info(
                        "PDG generated every step after %.1f s.",
                        (now - self._waiting_since).total_seconds(),
                    )
                return False
            # The timeout runs from the first deferral.
            if self._waiting_since is None:
                self._waiting_since = now
            if now - self._waiting_since >= _GENERATION_TIMEOUT:
                _logger.warning(
                    "PDG has not generated %s after %d s. Reading the graph anyway.",
                    ", ".join(pending),
                    _GENERATION_TIMEOUT.total_seconds(),
                )
                return False
            # PDG offers items many times a second, so the wait is logged sparingly.
            if (
                self._waiting_logged_at is None
                or now - self._waiting_logged_at >= _GENERATION_LOG_INTERVAL
            ):
                self._waiting_logged_at = now
                _logger.info(
                    "Waiting for PDG to generate %s before reading the graph.", ", ".join(pending)
                )
        return True

    def _submit(self) -> None:
        """Snapshot, validate, submit, and start the job, unless the cook stops first.

        Raises :class:`~deadline.houdini_pdg.errors.GraphRefusedError` for a refused
        graph, and the cause for any other failure.
        """
        # Each stage advances from the phase the one before it set, so a stop in
        # between makes the advance fail and the submission end.
        try:
            # Read the graph and the scene, and record what was read.
            nodes: Tuple[NodeInfo, ...] = tuple(self._host.read_graph())
            scene: SceneReads = self._host.read_scene()
            snapshot: GraphSnapshot
            report: Optional[ReadReport]
            snapshot, report = self._snapshot_of(nodes, scene)
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
            self._log_verdict(verdict)
            # A refused graph submits nothing. Each scenario is recorded once.
            if verdict.refusals:
                scenarios: Tuple[RefusalScenario, ...] = tuple(
                    dict.fromkeys(r.scenario for r in verdict.refusals)
                )
                if self._control.refuse(scenarios, expect=CookPhase.SNAPSHOTTING):
                    raise GraphRefusedError(verdict)
                return
            _logger.info(
                "Snapshot: %d step(s) and %d task(s). Nodes that are not steps: %s.",
                len(snapshot.steps),
                snapshot.total_tasks,
                ", ".join(snapshot.skipped_nodes) or "none",
            )
            self._ledger.expect(snapshot.total_tasks)
            if not self._control.adopt_snapshot(snapshot):
                return
            # Build the bundle into the journal's directory and create the job.
            bundle_dir: str = self._services.journal.bundle_dir()
            if not self._control.advance(CookPhase.SUBMITTING, expect=CookPhase.BUILDING):
                return
            job_id: str = self._services.submitter.submit(snapshot, bundle_dir)
            job: FarmJob = self._services.open_job(job_id, snapshot)
            if not self._control.adopt_job(job_id, job):
                # Nothing will ever drive the job, so it must not wait on the farm.
                _logger.warning("The cook stopped while job %s was created. Canceling it.", job_id)
                self._cancel(job)
                return
            # Learn the job's tasks and suspend its dependent steps.
            self._services.journal.job_submitted(job_id)
            job.start()
            if not self._control.advance(CookPhase.COOKING, expect=CookPhase.RESOLVING):
                return
            _logger.info("Cooking job %s.", job_id)
        except GraphRefusedError:
            raise
        except Exception as error:
            # CreateJob refusing the job for a quota created nothing on the farm,
            # so the cook is refused rather than failed.
            if isinstance(error, QuotaExceededError) and self._control.refuse(
                (RefusalScenario.QUOTA_EXCEEDED,), expect=CookPhase.SUBMITTING
            ):
                _logger.error("The job was refused by a service quota.", exc_info=True)
                raise
            # Any other failure fails the cook from the stage it reached, unless the
            # cook stopped first. Until it stops, the phase is that stage.
            if not self._control.advance(CookPhase.FAILED, expect=self._control.phase()):
                return
            _logger.error("The cook could not start its job.", exc_info=True)
            created: Optional[FarmJob] = self._control.claim_cancel()
            if created is not None:
                # Its tasks are suspended, and nothing would release or cancel them.
                self._cancel(created)
            raise

    def _snapshot_of(
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
    def _log_verdict(verdict: Verdict) -> None:
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

    # -- dispatch --

    def _dispatch(self, item: WorkItemHandle) -> ScheduleResult:
        """Register the item, store its payload, release its task, then tell PDG it started."""
        # An item the snapshot lacks has no task to run on.
        snapshot: Optional[GraphSnapshot] = self._control.snapshot()
        job: Optional[FarmJob]
        _, job = self._control.job()
        spec: Optional[WorkItemSpec] = snapshot.item_by_name(item.name) if snapshot else None
        if spec is None or job is None:
            _logger.error(
                "Work item '%s' of node '%s' has no task in the job: PDG generated it after "
                "the snapshot.",
                item.name,
                item.node_name,
            )
            return ScheduleResult.FAILED
        key: TaskKey = TaskKey(spec.node_name, spec.task_index)
        if not self._ledger.register(key, _Entry(item, spec, self._now())):
            _logger.error("Work item '%s' was offered twice.", item.name)
            return ScheduleResult.FAILED
        self._services.journal.item_scheduled(item)
        try:
            self._services.storage.put_payload(job.address(key), self._host.serialize(item))
        except Exception as e:
            # Reported as failed here, so a later result for the task is ignored.
            self._ledger.hold_back(key)
            _logger.error("Could not store the data of work item '%s': %s", item.name, e)
            return ScheduleResult.FAILED
        job.release(key)
        try:
            self._host.notify(item, ItemOutcome(ItemState.STARTED))
        except Exception:
            _logger.exception("Could not tell PDG that work item '%s' started", item.name)
        return ScheduleResult.SUCCEEDED

    # -- tick --

    def _guarded(self, part: str, run: Callable[[], None]) -> None:
        try:
            run()
        except Exception:
            _logger.exception("The tick failed while %s", part)

    def _report(self, job: FarmJob) -> None:
        # One outcome failing to report must not drop the rest of the poll.
        for outcome in job.poll():
            try:
                self._report_one(job, outcome)
            except Exception:
                _logger.exception("Could not report the result of %s", outcome.key)

    def _report_one(self, job: FarmJob, outcome: TaskOutcome) -> None:
        key: TaskKey = outcome.key
        if outcome.status is TaskStatus.CANCELED:
            # The farm stopped the task, usually because the job stopped. That is no
            # verdict on the work item, and the job check ends the cook.
            if self._ledger.note_canceled(key):
                _logger.info("Task %s[%d] was canceled.", key.step_name, key.task_index)
            return
        # Each offered item reports once. A result for an item not offered yet
        # waits until PDG offers it.
        succeeded: bool = outcome.status is TaskStatus.SUCCEEDED
        entry: Optional[_Entry] = self._ledger.claim(key, succeeded=succeeded)
        if entry is None:
            return
        duration: float = max(0.0, (self._now() - entry.registered_at).total_seconds())
        if succeeded:
            outputs: Tuple[OutputFile, ...] = self._outputs_of(job, key, entry)
            self._host.notify(entry.item, ItemOutcome(ItemState.SUCCEEDED, outputs, duration))
            return
        # A failure is logged with the worker's last lines before PDG hears of it.
        _logger.error(
            "Work item '%s' failed (task %s[%d]).", entry.item.name, key.step_name, key.task_index
        )
        try:
            self._services.journal.item_failed(key, lambda: job.log_tail(key, _LOG_TAIL_LINES))
        except Exception:
            _logger.exception("Could not record the failure of '%s'", entry.item.name)
        self._host.notify(entry.item, ItemOutcome(ItemState.FAILED, duration_seconds=duration))

    def _outputs_of(self, job: FarmJob, key: TaskKey, entry: _Entry) -> Tuple[OutputFile, ...]:
        """Declared outputs when PDG knows them, which costs nothing, else the task's own record."""
        # Read at onSchedule first: some nodes declare their outputs only then.
        declared: Tuple[DeclaredFile, ...] = (
            entry.item.declared_outputs or entry.spec.declared_outputs
        )
        if declared:
            return tuple(OutputFile(self._local(d.path), d.tag) for d in declared)
        # Otherwise the task published what it wrote. A record that cannot be read
        # reports no outputs rather than failing an item that succeeded.
        try:
            recorded: Optional[List[OutputFile]] = self._services.storage.get_outputs(
                job.address(key)
            )
        except Exception as e:
            _logger.warning("Could not read the outputs of '%s': %s", entry.item.name, e)
            return ()
        if recorded is None:
            _logger.info("Work item '%s' published no outputs.", entry.item.name)
            return ()
        return tuple(OutputFile(self._local(o.path), o.tag) for o in recorded)

    def _local(self, path: str) -> str:
        return path.replace(PDG_DIR_TOKEN, self._settings.working_dir)

    def _check_job(self, job: FarmJob) -> None:
        """Fail the cook, and every item still waiting, once the job can make no more progress."""
        status: JobStatus = job.status()
        self._job_status = status
        if (
            status.lifecycle_status not in _JOB_LIFECYCLE_FAILURES
            and status.task_run_status not in _JOB_RUN_FAILURES
        ):
            return
        # Only the tick that moves the cook to FAILED fails the outstanding items,
        # so each fails once.
        phase: CookPhase = self._control.phase()
        if not self._control.advance(CookPhase.FAILED, expect=phase):
            return
        counts: str = ", ".join(
            f"{k}={v}" for k, v in sorted(status.task_run_status_counts.items()) if v
        )
        outstanding: List[_Entry] = self._ledger.fail_outstanding()
        _logger.error(
            "The job can make no more progress (lifecycle %s, tasks %s%s). Failing %d "
            "outstanding work item(s).",
            status.lifecycle_status or "?",
            status.task_run_status or "?",
            f": {counts}" if counts else "",
            len(outstanding),
        )
        for entry in outstanding:
            try:
                self._host.notify(entry.item, ItemOutcome(ItemState.FAILED))
            except Exception:
                _logger.exception("Could not fail work item '%s'", entry.item.name)

    def _check_completion(self) -> None:
        """Drain once every item has reported, and finish once the job has succeeded."""
        if self._ledger.is_complete() and self._control.advance(
            CookPhase.DRAINING, expect=CookPhase.COOKING
        ):
            tallies: _Tallies = self._ledger.tallies()
            _logger.info(
                "All %d work item(s) reported: %d succeeded, %d failed.",
                tallies.reported,
                tallies.succeeded,
                tallies.failed,
            )
        if self._job_status.task_run_status == "SUCCEEDED" and self._control.advance(
            CookPhase.DONE, expect=CookPhase.DRAINING
        ):
            _logger.info("The job succeeded.")

    def _heartbeat(self) -> None:
        """Log the cook's progress at most every :data:`_HEARTBEAT_INTERVAL`."""
        now: datetime = self._now()
        if self._heartbeat_at is not None and now - self._heartbeat_at < _HEARTBEAT_INTERVAL:
            return
        # Released and not yet reported means waiting on the farm.
        self._heartbeat_at = now
        t: _Tallies = self._ledger.tallies()
        _logger.info(
            "%s: %d of %d work item(s) reported (%d succeeded, %d failed), %d released and "
            "waiting for the farm, %d canceled.",
            self._control.phase().value,
            t.reported,
            t.expected,
            t.succeeded,
            t.failed,
            t.registered - t.reported,
            t.canceled,
        )

    # -- helpers --

    def _stranded(self) -> bool:
        """Whether the job has tasks that can never be released, each named in the log.

        PDG can end a cook without offering every item: a node's own preparation
        can fail before scheduling, as FFmpeg Encode Video does when its ffmpeg
        lacks the requested codec, and an item whose dependency failed is never
        offered either. An offered item whose payload could not be stored is
        never released. Each such task was created SUSPENDED, so the job could
        never finish. A cook that ended RESOLVING, while its job was being
        started, released no task at all.

        Besides that, only a cook that ended COOKING has items PDG still owed it,
        and only one that ended COOKING or DRAINING can hold an unreleased task;
        any other phase either never had a job or already reached a verdict.
        """
        # Read the phase once, so the failure below is recorded from the phase judged.
        phase: CookPhase = self._control.phase()
        # RESOLVING is entered only with the job recorded, and nothing was released yet.
        if phase is CookPhase.RESOLVING:
            job_id: str
            job_id, _ = self._control.job()
            _logger.error(
                "The cook ended while job %s was being started, so none of its tasks can "
                "run, and the job is marked failed.",
                job_id,
            )
            # Recorded before the close, so the summary says the cook failed.
            self._control.advance(CookPhase.FAILED, expect=phase)
            return True
        never_offered: List[WorkItemSpec] = (
            self._never_offered() if phase is CookPhase.COOKING else []
        )
        unreleased: List[_Entry] = (
            self._ledger.unreleased() if phase in (CookPhase.COOKING, CookPhase.DRAINING) else []
        )
        for entry in unreleased:
            _logger.error(
                "Work item '%s' of node '%s' could not be stored, so its task can never run, "
                "and the job is marked failed.",
                entry.item.name,
                entry.item.node_name,
            )
        # Nothing stranded keeps the job, which may still be finishing on the farm.
        stranded: bool = bool(never_offered or unreleased)
        if stranded:
            self._control.advance(CookPhase.FAILED, expect=phase)
        return stranded

    def _never_offered(self) -> List[WorkItemSpec]:
        """The job's work items PDG never offered, each named in the log with PDG's own reason."""
        # A cook in COOKING always has its snapshot.
        snapshot: Optional[GraphSnapshot] = self._control.snapshot()
        offered: Set[TaskKey] = self._ledger.offered()
        missing: List[WorkItemSpec] = [
            spec
            for step in (snapshot.steps if snapshot is not None else ())
            for spec in step.items
            if TaskKey(spec.node_name, spec.task_index) not in offered
        ]
        for spec in missing:
            error: str
            last_line: str
            try:
                error, last_line = self._host.failure_reason(spec.item_id)
            except Exception:
                error, last_line = "", ""
            # Only an ERROR line is PDG's reason. Any other last line may be unrelated.
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
                "Work item '%s' of node '%s' was never offered to this scheduler, so its "
                "task can never run, and the job is marked failed. %s",
                spec.name,
                spec.node_name,
                why,
            )
        return missing

    @staticmethod
    def _fail(job: FarmJob) -> None:
        try:
            job.fail()
        except Exception:
            _logger.exception("Could not mark the job failed")

    @staticmethod
    def _cancel(job: FarmJob) -> None:
        try:
            job.cancel()
        except Exception:
            _logger.exception("Could not cancel the job")

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
        # A static cook only generates items. It has no Cook, journal, or farm.
        self._static: bool = False
        # A start is running, so a second start does not connect a second cook.
        self._starting: bool = False
        # How many stops have come, and whether the latest asked to cancel. A start
        # compares the count with the one it began with to learn of a stop meanwhile.
        self._stops: int = 0
        self._stop_cancel: bool = False

    def on_start_cook(self, static: bool, cook_set: Optional[AbstractSet[str]] = None) -> bool:
        """Start a cook. A static cook needs no farm, and only sets the working directory.

        A stop that arrives while the start runs ends the cook being started.
        """
        # Only one start runs at a time, and only with no cook open.
        with self._lock:
            if self._cook is not None or self._static or self._starting:
                _logger.warning(
                    "A cook started while another is open or starting. The new start is ignored."
                )
                return True
            self._starting = True
            stops: int = self._stops
        try:
            return self._start(static, cook_set, stops)
        finally:
            with self._lock:
                self._starting = False

    def _start(self, static: bool, cook_set: Optional[AbstractSet[str]], stops: int) -> bool:
        settings: SchedulerSettings = self._read_settings()
        if static:
            # PDG still copies its job scripts into the working directory. A stop
            # that came meanwhile has already ended the generate.
            self._host.set_working_dir(settings.working_dir)
            with self._lock:
                if self._stops == stops:
                    self._static = True
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
        # The cook is open before it begins, so a stop from now on closes it. A stop
        # that came while connecting found nothing to close, so it is closed here.
        with self._lock:
            stopped: bool = self._stops != stops
            cancel: bool = self._stop_cancel
            if not stopped:
                self._cook = cook
        if stopped:
            _logger.info("The cook was stopped while it connected to the farm.")
            cook.close(cancel=cancel)
            return True
        if cook_set is not None:
            _logger.info("PDG asked to cook %d node(s).", len(cook_set))
        try:
            cook._begin()
        except Exception:
            # PDG drops a scheduler whose start failed, so the cook is closed here,
            # unless a stop already took it and closed it.
            if self._take(cook):
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
            self._stop_watching()
        return True

    def _take(self, cook: Cook) -> bool:
        """Remove ``cook`` if it is the open one, so exactly one caller closes it."""
        with self._lock:
            if self._cook is not cook:
                return False
            self._cook = None
            return True

    def _stop_watching(self) -> None:
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
        """Close the open cook, cancelling its job when asked.

        A start still running learns of the stop, and ends the cook it is starting.
        """
        # Drop the cook first, so the next start builds a fresh one.
        with self._lock:
            cook: Optional[Cook]
            cook, self._cook = self._cook, None
            self._static = False
            # A start still running compares the count with its own to learn of this stop.
            self._stops += 1
            self._stop_cancel = cancel
        self._stop_watching()
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
