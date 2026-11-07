# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""A submitted job, activated task by task through the Deadline Cloud API.

The job is created with its tasks suspended. PDG offers work items one at a time,
and each task may run only once its work item's payload is stored and every step
it depends on has finished on the farm. This module is the only code that talks
to the Deadline Cloud API for a running cook: it maps PDG's tasks to farm IDs,
suspends the dependent steps, activates released tasks when the farm allows it,
and finds the tasks that finished.
"""

from __future__ import annotations

# The standard library, the client library's session log reader, and the
# package's errors, graph keys, and port value types.
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Pattern,
    Set,
    Tuple,
)

from deadline.client import api

from .errors import TaskMapIncompleteError, TaskSuspensionError
from .graph import GraphSnapshot, StepSpec, TaskKey
from .ports import Clock, JobStatus, TaskAddress, TaskOutcome, TaskStatus

_logger: logging.Logger = logging.getLogger(__name__)

# The service's run statuses this module acts on.
_SUCCEEDED: str = "SUCCEEDED"
_FAILED: str = "FAILED"
_CANCELED: str = "CANCELED"
# NOT_COMPATIBLE means no worker on the queue can run the task: a real verdict.
_FAILURES: FrozenSet[str] = frozenset({_FAILED, "NOT_COMPATIBLE"})
# The statuses of a task whose run has ended with a result.
_RUN_ENDED: FrozenSet[str] = _FAILURES | {_SUCCEEDED}
# The statuses of a task the farm is running now, or is about to run.
_RUN_IN_PROGRESS: FrozenSet[str] = frozenset(
    {"ASSIGNED", "STARTING", "SCHEDULED", "RUNNING", "INTERRUPTING"}
)
# CANCELED is searched for so a cancellation is returned once. It is never
# returned as a failure.
_SEARCHED_STATUSES: List[str] = sorted(_FAILURES | {_SUCCEEDED, _CANCELED})

# An update still being applied to a step's tasks. A task activated inside that
# window is overwritten when the update lands.
_UPDATE_IN_PROGRESS: str = "UPDATE_IN_PROGRESS"

# The service's own limits: BatchUpdateTask and BatchGetTask take at most 100
# tasks, SearchTasks returns pages of at most 100 and caps itemOffset at 10,000.
_BATCH_LIMIT: int = 100
_PAGE_SIZE: int = 100
_OFFSET_LIMIT: int = 10_000

# Step statuses are read at most this often: PDG ticks many times a second,
# and a step takes minutes.
_STEP_STATUS_REFRESH_INTERVAL: timedelta = timedelta(seconds=5)
# The search index publishes a finished task late and out of order, so the
# search window's start trails the newest result. A result the index publishes
# even later is caught by the reconcile.
_SEARCH_WINDOW_LAG: timedelta = timedelta(seconds=120)
_RECONCILE_INTERVAL: timedelta = timedelta(seconds=60)

_STEP_ID: Pattern[str] = re.compile(r"step-([0-9a-f]{32})")
_SESSION_ACTION_ID: Pattern[str] = re.compile(r"sessionaction-([0-9a-f]{32})-(?:0|[1-9][0-9]*)")
# A task index as the service writes it: no sign, no leading zero.
_INDEX: Pattern[str] = re.compile(r"0|[1-9][0-9]*")

_EPOCH: datetime = datetime.min.replace(tzinfo=timezone.utc)


class _ResultDecision(NamedTuple):
    """What poll() should do with one final result from the farm."""

    # Hand the result to the cook: the task was released and has no result returned yet.
    return_to_cook: bool
    # Log a run the scheduler node didn't start: the farm ran the task before
    # the scheduler node activated it, and this is the first time that run is marked.
    log_run_not_started_by_scheduler_node: bool


class _TaskTracker:
    """Where each task PDG released stands, from its release to its final result.

    PDG's schedule thread calls ``release``. Everything else runs on the tick
    thread. The lock guards every field.

    A released task moves through these, and is in at most one at a time:

    * the release queue: released by PDG, waiting for its status to be checked
      and for its step's dependencies to succeed on the farm;
    * activating: in a ``BatchUpdateTask`` call the scheduler node is making;
    * activated: set ``READY`` on the farm by the scheduler node;
    * finished: its final result was returned to the cook, so no later result
      for it is returned again.

    The release queue keeps release order, except that a task whose activation
    the service rejected goes behind the tasks queued at that point. A task in
    the release queue is never dropped, unless it has no task on the farm.

    A task is activated only after its status has been checked since its
    release. A task that had already ended by then ran early: that run may have
    come before PDG stored the item's payload, so its end time is kept, and only
    a result that ended after it counts. Both end times come from the service's
    clock. A task the farm is running when a check finds it unactivated stays
    unchecked until that run ends, and then counts as an early run in the same
    way. The status is checked again just before activation, and a task that
    ended in between is finished instead of activated.

    Every run the farm makes of a task the scheduler node never activated is marked
    once per task, whatever it ended with, so that run is logged once.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        # Insertion-ordered, so the queue is FIFO and membership is O(1). The
        # value says whether the task's status has been checked since its release.
        self._release_queue: Dict[TaskKey, bool] = {}
        self._activating: Set[TaskKey] = set()
        # Each task the scheduler node set READY on the farm. A run the farm
        # makes of any other task is a run the scheduler node didn't start.
        self._activated: Set[TaskKey] = set()
        # Each task whose final result was returned to the cook. Nothing is
        # returned for it again.
        self._finished: Set[TaskKey] = set()
        # Each task that ran early, with the end time of its latest early run,
        # or None when the service gave none.
        self._early_run_ended_at: Dict[TaskKey, Optional[datetime]] = {}
        # Each task the farm ran before the scheduler node activated it, so that run
        # is logged once. Every task that ran early is in it.
        self._ran_without_scheduler_node: Set[TaskKey] = set()

    def release(self, key: TaskKey) -> None:
        """Put a task PDG released in the release queue, unchecked, unless PDG released it before."""
        with self._lock:
            if (
                key in self._release_queue
                or key in self._activating
                or key in self._activated
                or key in self._finished
            ):
                return
            self._release_queue[key] = False

    def awaiting_check(self) -> List[TaskKey]:
        """The queued tasks whose status has not been checked since their release, oldest first."""
        with self._lock:
            return [key for key, checked in self._release_queue.items() if not checked]

    def record_checks(
        self, checked: Iterable[TaskKey], ended_early: Dict[TaskKey, Optional[datetime]]
    ) -> List[TaskKey]:
        """Record tasks whose status was checked, and the ones among them that had already ended.

        Returns the tasks whose run the scheduler node didn't start is marked
        for the first time. A checked task keeps its place in the release queue.
        """
        with self._lock:
            # A checked task is marked in its place. An ended task's run is
            # logged only the first time it is marked, and counts as an early run.
            for key in checked:
                self._release_queue[key] = True
            first: List[TaskKey] = [
                key for key in ended_early if key not in self._ran_without_scheduler_node
            ]
            self._ran_without_scheduler_node.update(ended_early)
            for key, end in ended_early.items():
                self._early_run_ended_at[key] = _later_end(self._early_run_ended_at.get(key), end)
            return first

    def require_recheck(self, keys: Iterable[TaskKey]) -> None:
        """Mark queued tasks as unchecked, in their place, so their status is checked again before any activation."""
        with self._lock:
            for key in keys:
                if key in self._release_queue:
                    self._release_queue[key] = False

    def drop_missing_task(self, key: TaskKey) -> None:
        """Remove a key that has no task on the farm from the release queue, so it is never checked or activated."""
        with self._lock:
            self._release_queue.pop(key, None)

    def ready_to_activate(
        self,
        unblocked_steps: AbstractSet[str],
        limit: int,
        considered_this_poll: AbstractSet[TaskKey],
    ) -> List[TaskKey]:
        """Up to ``limit`` checked queued tasks whose step is unblocked, oldest first, skipping ``considered_this_poll``.

        A step is unblocked when every step it depends on has succeeded on the
        farm. The tasks stay in the release queue, in their place, until
        ``begin_activation`` moves them.
        """
        with self._lock:
            ready: List[TaskKey] = []
            for key, checked in self._release_queue.items():
                if len(ready) >= limit:
                    break
                if checked and key.step_name in unblocked_steps and key not in considered_this_poll:
                    ready.append(key)
            return ready

    def begin_activation(self, keys: Iterable[TaskKey]) -> List[TaskKey]:
        """Move the tasks still in the release queue to activating, for one activation call, and return them."""
        with self._lock:
            activating: List[TaskKey] = [key for key in keys if key in self._release_queue]
            for key in activating:
                del self._release_queue[key]
            self._activating.update(activating)
            return activating

    def end_activation(
        self,
        attempted: Iterable[TaskKey],
        activated: List[TaskKey],
        to_retry: Iterable[TaskKey],
    ) -> None:
        """Record an activation call: the tasks it activated, and the tasks to retry on a later poll."""
        with self._lock:
            self._activating.difference_update(attempted)
            self._activated.update(activated)
            # Back at the end of the release queue, so a task the service keeps
            # rejecting does not hold up the tasks released after it. An
            # activating task cannot have finished meanwhile: results are
            # read on the same thread, after this call.
            for key in to_retry:
                self._release_queue[key] = True

    def record_result(self, key: TaskKey, ended_at: Optional[datetime]) -> _ResultDecision:
        """Record a final result, and say whether to return it to the cook and whether to log a run the scheduler node didn't start.

        A task that is not yet released and checked is not finished, and
        its run counts as an early run: it may have come before the item's
        payload was stored, so its result is never returned. The task runs
        again once it is released.
        """
        with self._lock:
            # The early-run check comes before the activated and queued ones,
            # because the index keeps showing an early run's result after the
            # task runs again.
            if key in self._finished:
                return _ResultDecision(
                    return_to_cook=False, log_run_not_started_by_scheduler_node=False
                )
            if key in self._early_run_ended_at and not _ended_after(
                ended_at, self._early_run_ended_at[key]
            ):
                return _ResultDecision(
                    return_to_cook=False, log_run_not_started_by_scheduler_node=False
                )
            if key in self._activated:
                self._finished.add(key)
                return _ResultDecision(
                    return_to_cook=True, log_run_not_started_by_scheduler_node=False
                )
            # A checked task that ended unactivated was idle at every check
            # since its release, so its run started after its release check, and
            # after its payload was stored. A task whose run the scheduler node
            # didn't start was marked before already had that run logged.
            if self._release_queue.get(key):
                del self._release_queue[key]
                self._finished.add(key)
                return _ResultDecision(
                    return_to_cook=True,
                    log_run_not_started_by_scheduler_node=self._mark_ran_without_scheduler_node(
                        key
                    ),
                )
            first: bool = self._mark_ran_without_scheduler_node(key)
            self._early_run_ended_at[key] = _later_end(self._early_run_ended_at.get(key), ended_at)
            return _ResultDecision(
                return_to_cook=False, log_run_not_started_by_scheduler_node=first
            )

    def record_retryable_failure(self, key: TaskKey) -> bool:
        """Record a failure the farm will retry, and say whether it is the task's first run the scheduler node didn't start.

        The failure is not final, so nothing about the task changes, but a run
        of a task the scheduler node never activated is still a run the farm
        made on its own.
        """
        with self._lock:
            # A run of a task the scheduler node activated is one it asked for.
            return key not in self._activated and self._mark_ran_without_scheduler_node(key)

    def _mark_ran_without_scheduler_node(self, key: TaskKey) -> bool:
        """Mark the task as run without the scheduler node. True the first time."""
        # The caller holds the lock.
        first: bool = key not in self._ran_without_scheduler_node
        self._ran_without_scheduler_node.add(key)
        return first

    def awaiting_result(self) -> List[TaskKey]:
        """The tasks the scheduler node activated that are not finished yet, in a stable order."""
        with self._lock:
            return sorted(self._activated - self._finished)

    def held_count_by_step(self, unblocked_steps: AbstractSet[str]) -> Dict[str, int]:
        """How many queued tasks each blocked step is holding back."""
        with self._lock:
            held: Dict[str, int] = {}
            for key in self._release_queue:
                if key.step_name not in unblocked_steps:
                    held[key.step_name] = held.get(key.step_name, 0) + 1
            return held


class DeadlineFarmJob:
    """Implements ``FarmJob``.

    A task is activated only after every step it depends on reads ``SUCCEEDED``
    on the farm, because activating a task also skips its step's dependency
    wait. How many results the cook has reported plays no part.
    """

    # PDG's schedule thread calls ``release``, which touches only
    # ``_task_tracker``, and ``address``, which reads only what ``start`` wrote
    # before the first release. Everything else runs on the tick thread, except
    # ``cancel`` and ``fail``, which read no state. So ``_task_tracker`` is the
    # only shared state, and it guards itself.

    def __init__(
        self,
        *,
        deadline_client: Any,
        farm_id: str,
        queue_id: str,
        job_id: str,
        snapshot: GraphSnapshot,
        clock: Clock,
        get_session_logs: Callable[..., Any] = api.get_session_logs,
    ) -> None:
        # The job, and the services it is reached through.
        self._client: Any = deadline_client
        self._farm_id: str = farm_id
        self._queue_id: str = queue_id
        self._job_id: str = job_id
        self._snapshot: GraphSnapshot = snapshot
        self._clock: Clock = clock
        self._get_session_logs: Callable[..., Any] = get_session_logs

        # Step IDs by name and back, fixed by ``start``.
        self._step_id_by_name: Dict[str, str] = {}
        self._step_name_by_id: Dict[str, str] = {}

        # Where each released task stands, and the tick thread's view of the farm.
        self._task_tracker: _TaskTracker = _TaskTracker()
        # Each step's run and lifecycle status by name, and when they were read.
        self._step_statuses: Dict[str, Tuple[str, str]] = {}
        self._step_statuses_read_at: Optional[datetime] = None
        # The search asks only for tasks that ended after this time. None until
        # the first search finds a result.
        self._search_ended_after: Optional[datetime] = None
        self._last_reconciled_at: Optional[datetime] = None
        # Each task whose cancellation was returned, so it is returned once.
        self._cancellation_returned: Set[TaskKey] = set()
        # The held counts by step that were logged last, so a log line is
        # written only when they change.
        self._last_logged_held_counts: Dict[str, int] = {}

        # The job's retry budget, from GetJob. Until it is known, a failure is
        # treated as one the farm may retry: the job's own status ends the cook
        # if it is not.
        self._max_retries_per_task: Optional[int] = None

    def start(self) -> None:
        """Derive every task ID, verify the derivation on each step, and suspend the steps that have dependencies.

        Suspension is one call per dependent step. A job-wide call lands
        asynchronously and can overwrite activations made after it.
        """
        listed: Dict[str, str] = self._list_step_ids()

        # Every problem is collected, so one error names every step that failed.
        problems: List[str] = []
        for step in self._snapshot.steps:
            step_id: str = listed.get(step.name, "")
            if not _STEP_ID.fullmatch(step_id):
                problems.append(
                    f"step '{step.name}' is missing from job {self._job_id}"
                    if not step_id
                    else f"step '{step.name}' has an unexpected ID '{step_id}'"
                )
                continue
            problems.extend(self._verify_step_tasks(step, step_id))
        if problems:
            raise TaskMapIncompleteError(
                f"Could not map every task of job {self._job_id}: {'; '.join(problems)}. "
                "A task's ID names where its work item's data is stored, so the cook cannot "
                "continue without it."
            )

        # The map is verified. Record it, then suspend: a failed suspension leaves
        # the cook nothing to drive.
        self._step_id_by_name = {step.name: listed[step.name] for step in self._snapshot.steps}
        self._step_name_by_id = {step_id: name for name, step_id in self._step_id_by_name.items()}
        self._last_reconciled_at = self._now()
        self._read_retry_budget()
        self._suspend_dependent_steps()

    def address(self, key: TaskKey) -> TaskAddress:
        """Where the task's payload and output record live."""
        # A key outside the verified map has no task, so it has no address either.
        step_id: Optional[str] = self._step_id_by_name.get(key.step_name)
        step: Optional[StepSpec] = self._snapshot.step(key.step_name)
        if step_id is None or step is None or not 0 <= key.task_index < step.task_count:
            raise TaskMapIncompleteError(
                f"Task {key.step_name}[{key.task_index}] is not in job {self._job_id}."
            )
        return TaskAddress(
            farm_id=self._farm_id,
            queue_id=self._queue_id,
            job_id=self._job_id,
            step_id=step_id,
            task_id=_derived_task_id(step_id, key.task_index),
        )

    def release(self, key: TaskKey) -> None:
        """Add the task to the release queue. A later poll checks its status, and activates it once its step is unblocked."""
        self._task_tracker.release(key)

    def poll(self) -> List[TaskOutcome]:
        """Run one round of farm work, and return the final results found since the last poll.

        Each poll runs five parts, in order:

        1. Refresh the step statuses, at most every 5 seconds.
        2. Check the status of each task PDG released since the last poll.
        3. Activate the checked tasks whose steps are unblocked, checking each
           batch's status again just before the activation call.
        4. Search for tasks that ended after the search window's start.
        5. Once a minute, read the activated tasks that still have no result.

        A task normally runs only after the scheduler node activates it. If the
        farm runs it earlier on its own, that run may not have the work item's
        payload:

        * If the task is running or has already ended at the first check after
          its release, or is running at the check just before activation, its
          run may have started before the payload was stored. Its result is not
          used. Once that run ends, only a later run's result is returned:
          normally the run after the scheduler node activates the task.
        * If the task was idle at the first check and its run ended before the
          check just before activation, the run started after the payload was
          stored. Its result is returned, and the task is not activated.

        The search filters on ``endedAt``, not ``updatedAt``, because ``updatedAt``
        does not change when a worker finishes a task. The search index
        publishes results late and out of order, so the search window trails
        the newest result, and the once-a-minute read catches anything it misses.
        """
        # Each part runs in its own guard, so one failing part does not stop the
        # others. Outcomes a part found before it failed are still returned,
        # because they are already marked final.
        outcomes: List[TaskOutcome] = []
        for part in (
            self._refresh_step_statuses,
            self._check_released_tasks,
            self._activate_ready_tasks,
            self._search_ended_tasks,
            self._reconcile_activated_tasks,
        ):
            try:
                part(outcomes)
            except Exception:
                _logger.exception("Polling job %s failed in %s", self._job_id, part.__name__)
        return outcomes

    def status(self) -> JobStatus:
        """The job's status, or an empty one when the read fails."""
        try:
            job: Dict[str, Any] = self._client.get_job(**self._job_args())
            self._store_retry_budget(job)
            return JobStatus(
                lifecycle_status=str(job.get("lifecycleStatus") or ""),
                task_run_status=str(job.get("taskRunStatus") or ""),
                task_run_status_counts=dict(job.get("taskRunStatusCounts") or {}),
            )
        except Exception as e:
            _logger.warning("Could not read the status of job %s: %s", self._job_id, e)
            return JobStatus()

    def cancel(self) -> None:
        """Cancel the job, logging a failure instead of raising it."""
        try:
            self._client.update_job(**self._job_args(), targetTaskRunStatus=_CANCELED)
            _logger.info("Canceled job %s.", self._job_id)
        except Exception as e:
            _logger.error("Could not cancel job %s: %s", self._job_id, e)

    def fail(self) -> None:
        """Mark the job failed, so no task stays suspended, or cancel it when the service refuses. Never raises.

        Failed rather than canceled, because a canceled job reads as one the
        artist stopped.
        """
        try:
            self._client.update_job(**self._job_args(), targetTaskRunStatus=_FAILED)
            _logger.info("Marked job %s as failed.", self._job_id)
        except Exception as e:
            _logger.warning(
                "Could not mark job %s as failed: %s. Canceling it instead.", self._job_id, e
            )
            self.cancel()

    def log_tail(self, key: TaskKey, lines: int) -> List[str]:
        """The last lines of the task's latest session log, through ``deadline.client.api.get_session_logs``."""
        if lines <= 0:
            return []
        try:
            # The session summaries carry no session ID, so the latest action's
            # ID names the session.
            task_id: str = self.address(key).task_id
            actions: List[Dict[str, Any]] = list(
                self._paginate(
                    self._client.list_session_actions,
                    "sessionActions",
                    **self._job_args(),
                    taskId=task_id,
                )
            )
            session_id: str = _latest_session_id(actions)
            if not session_id:
                return []

            # An event can hold several lines, so split before keeping the last ones.
            result: Any = self._get_session_logs(
                self._farm_id, self._queue_id, session_id=session_id, limit=lines
            )
            messages: List[str] = []
            for event in result.events:
                messages.extend(str(event.message).splitlines())
            return messages[-lines:]
        except Exception as e:
            _logger.debug("Could not read the worker log for %s: %s", key, e)
            return []

    # -- start --

    def _list_step_ids(self) -> Dict[str, str]:
        """Every step's ID by name, from one paginated ``ListSteps``."""
        try:
            return {
                step["name"]: step["stepId"]
                for step in self._paginate(self._client.list_steps, "steps", **self._job_args())
                if step.get("name") and step.get("stepId")
            }
        except Exception as e:
            raise TaskMapIncompleteError(
                f"Could not list the steps of job {self._job_id}: {e}. Without the step IDs, "
                "no work item's data can be stored or its task released."
            ) from e

    def _verify_step_tasks(self, step: StepSpec, step_id: str) -> List[str]:
        """The problems, if any, when the derived IDs of the step's first and last tasks do not name tasks with those ``TaskIndex`` values.

        One ``GetTask`` per distinct index, so a one-task step makes one call.
        """
        problems: List[str] = []
        for index in sorted({0, step.task_count - 1}):
            # A task that cannot be read, or that carries another index, means
            # the derivation does not hold for this step.
            task_id: str = _derived_task_id(step_id, index)
            try:
                task: Dict[str, Any] = self._client.get_task(
                    **self._job_args(), stepId=step_id, taskId=task_id
                )
            except Exception as e:
                problems.append(f"task '{task_id}' for {step.name}[{index}] could not be read: {e}")
                continue
            actual: Optional[int] = _task_index_of(task)
            if actual != index:
                problems.append(
                    f"task '{task_id}' for {step.name}[{index}] has TaskIndex {actual}, so task "
                    "IDs no longer follow the step ID and the task index"
                )
        return problems

    def _suspend_dependent_steps(self) -> None:
        """Suspend each step that has dependencies, with one call per step.

        Raises ``TaskSuspensionError`` naming every step that could not be
        suspended, after trying them all.
        """
        # CreateJob suspends only the tasks schedulable at creation, the root
        # steps. A dependent step's tasks are created PENDING and would run, without
        # payloads, as soon as their dependencies finish. A root step is left
        # alone: its suspension sweep would overwrite the first activations.
        problems: List[str] = []
        for step in self._snapshot.steps:
            if not step.dependencies:
                continue
            step_id: str = self._step_id_by_name[step.name]
            try:
                self._client.update_step(
                    **self._job_args(), stepId=step_id, targetTaskRunStatus="SUSPENDED"
                )
            except Exception as e:
                problems.append(f"step '{step.name}': {e}")
        if problems:
            raise TaskSuspensionError(
                f"Could not suspend {len(problems)} step(s) of job {self._job_id}: "
                f"{'; '.join(problems)}. Their tasks would run before their work items' data "
                "is stored."
            )

    def _read_retry_budget(self) -> None:
        """Read the job's retry budget, logging a failure: ``status`` reads it again."""
        try:
            self._store_retry_budget(self._client.get_job(**self._job_args()))
        except Exception as e:
            _logger.warning("Could not read the retry budget of job %s: %s", self._job_id, e)

    def _store_retry_budget(self, job: Dict[str, Any]) -> None:
        """Keep the job's ``maxRetriesPerTask`` when it is a count."""
        value: Any = job.get("maxRetriesPerTask")
        if _is_count(value):
            self._max_retries_per_task = value

    # -- poll --

    def _refresh_step_statuses(self, outcomes: List[TaskOutcome]) -> None:
        """Refresh each step's run and lifecycle status, at most every few seconds."""
        now: datetime = self._now()
        if (
            self._step_statuses_read_at is not None
            and now - self._step_statuses_read_at < _STEP_STATUS_REFRESH_INTERVAL
        ):
            return
        self._step_statuses_read_at = now
        try:
            statuses: Dict[str, Tuple[str, str]] = {
                str(step["name"]): (
                    str(step.get("taskRunStatus") or ""),
                    str(step.get("lifecycleStatus") or ""),
                )
                for step in self._paginate(self._client.list_steps, "steps", **self._job_args())
                if step.get("name")
            }
        except Exception as e:
            # The previous read stands. Treating a failed read as "nothing
            # finished" would block steps that are already unblocked.
            _logger.warning("Could not read the step statuses of job %s: %s", self._job_id, e)
            return
        if statuses:
            self._step_statuses = statuses

    def _unblocked_steps(self) -> Set[str]:
        """The steps whose dependencies all read ``SUCCEEDED``, and that no update is being applied to."""
        unblocked_steps: Set[str] = set()
        for step in self._snapshot.steps:
            if self._step_statuses.get(step.name, ("", ""))[1] == _UPDATE_IN_PROGRESS:
                continue
            if all(
                self._step_statuses.get(dep, ("", ""))[0] == _SUCCEEDED for dep in step.dependencies
            ):
                unblocked_steps.add(step.name)
        return unblocked_steps

    def _check_released_tasks(self, outcomes: List[TaskOutcome]) -> None:
        """Check the status of each newly released task, so a task that already ran is known before it is activated.

        The search index can show a finished task minutes late, so only the
        task itself says whether the farm ran it before the scheduler node could
        activate it. A task the farm is running stays unchecked, and is checked
        again on each poll until that run ends.
        """
        keys: List[TaskKey] = self._task_tracker.awaiting_check()
        for start in range(0, len(keys), _BATCH_LIMIT):
            batch: Dict[str, Tuple[TaskKey, TaskAddress]] = self._addresses_by_task_id(
                keys[start : start + _BATCH_LIMIT]
            )
            if not batch:
                continue

            # A failed call ends this check. The tasks stay unchecked, so none
            # of them is activated, and the next poll checks them again.
            try:
                response: Dict[str, Any] = self._client.batch_get_task(
                    identifiers=[_task_args(address) for _, address in batch.values()]
                )
                tasks: List[Dict[str, Any]] = response.get("tasks") or []
                errors: List[Dict[str, Any]] = response.get("errors") or []
            except Exception as e:
                _logger.warning("Could not read %d released task(s): %s", len(batch), e)
                return

            # A task the answer does not return stays unchecked, and is logged
            # so a task that is never activated can be traced.
            answered: Set[str] = self._record_release_checks(batch, tasks)
            codes: Dict[str, Any] = {
                str(error.get("taskId")): error.get("code") for error in errors
            }
            for task_id in sorted(batch.keys() - answered):
                _logger.warning(
                    "Could not read released task %s: %s",
                    task_id,
                    codes.get(task_id, "not in the answer"),
                )

    def _addresses_by_task_id(self, keys: List[TaskKey]) -> Dict[str, Tuple[TaskKey, TaskAddress]]:
        """Each key and its address, by task ID. A key with no task is dropped from the queue."""
        addresses: Dict[str, Tuple[TaskKey, TaskAddress]] = {}
        for key in keys:
            try:
                address: TaskAddress = self.address(key)
            except TaskMapIncompleteError as e:
                # Not retried, and never checked or activated: a key with no
                # task can never run.
                _logger.error("%s It was released, and is dropped.", e)
                self._task_tracker.drop_missing_task(key)
                continue
            addresses[address.task_id] = (key, address)
        return addresses

    def _record_release_checks(
        self, batch: Dict[str, Tuple[TaskKey, TaskAddress]], tasks: List[Dict[str, Any]]
    ) -> Set[str]:
        """Record the release check's answers in the tracker, and return the task IDs the service answered.

        A task that had already ended ran early, and its run is logged once. A
        running task isn't marked checked, so it's checked again next poll and
        counts as an early run once it ends.
        """
        answered: Set[str] = set()
        checked: List[TaskKey] = []
        ended: Dict[TaskKey, Optional[datetime]] = {}
        statuses: Dict[TaskKey, str] = {}
        for task in tasks:
            # Only tasks this check asked for count. A task that had ended ran
            # before the scheduler node activated it, so its end time is kept.
            task_id: str = str(task.get("taskId"))
            entry: Optional[Tuple[TaskKey, TaskAddress]] = batch.get(task_id)
            if entry is None:
                continue
            answered.add(task_id)
            key: TaskKey = entry[0]
            status: str = str(task.get("runStatus") or "")
            if status in _RUN_IN_PROGRESS:
                continue
            checked.append(key)
            if status in _RUN_ENDED:
                ended[key] = _as_utc(task.get("endedAt"))
                statuses[key] = status
        # A run found at the release check never goes to the cook.
        for key in self._task_tracker.record_checks(checked, ended):
            _log_run_not_started_by_scheduler_node(key, statuses[key], result_used=False)
        return answered

    def _activate_ready_tasks(self, outcomes: List[TaskOutcome]) -> None:
        """Activate the checked queued tasks of unblocked steps, in batches, oldest first.

        Each batch is checked again just before its activation call. A held
        task can wait minutes in the queue, and the farm may run it meanwhile
        without the scheduler node, so only that check says whether it still
        needs to run.
        """
        unblocked_steps: Set[str] = self._unblocked_steps()
        # The tasks this poll has already checked, so a task left queued is
        # checked once per poll and the loop ends.
        considered_this_poll: Set[TaskKey] = set()
        while True:
            # The next batch of checked tasks of unblocked steps, checked again
            # just before activation; the ones that still need to run are activated.
            ready: List[TaskKey] = self._task_tracker.ready_to_activate(
                unblocked_steps, _BATCH_LIMIT, considered_this_poll
            )
            if not ready:
                break
            considered_this_poll.update(ready)
            to_activate: Optional[List[TaskKey]] = self._recheck_before_activation(ready, outcomes)
            if to_activate is None:
                break
            batch: List[TaskKey] = self._task_tracker.begin_activation(to_activate)
            if not batch:
                continue
            activated, to_retry = self._activate_batch(batch)
            self._task_tracker.end_activation(batch, activated, to_retry)
            if activated:
                _logger.info("Activated %d task(s) to run.", len(activated))
            if to_retry:
                # Retried next tick, behind the tasks still queued, so a call
                # the service keeps refusing is not repeated in a tight loop.
                break
        self._log_held_tasks(unblocked_steps)

    def _recheck_before_activation(
        self, ready: List[TaskKey], outcomes: List[TaskOutcome]
    ) -> Optional[List[TaskKey]]:
        """Check the ready tasks' status with one ``BatchGetTask``, and return the ones to activate, in release order.

        Returns None when the call fails. For each task: idle, so it's
        activated; running, so it isn't activated and needs a recheck, and its
        run counts as an early run once it ends; still showing the early run's
        end, so it's activated; ended since the release check, so it isn't
        activated and its result goes to the cook, because that run started
        after the payload was stored; failed with retries left, so it's
        activated, and that run is logged once.
        """
        addresses: Dict[str, Tuple[TaskKey, TaskAddress]] = self._addresses_by_task_id(ready)

        # A failed call stops activation for this poll. The tasks stay queued in
        # their place, and the next poll checks them again. Every ready task was
        # checked at its release, so every one has an address.
        try:
            response: Dict[str, Any] = self._client.batch_get_task(
                identifiers=[_task_args(address) for _, address in addresses.values()]
            )
            tasks: Dict[str, Dict[str, Any]] = {
                str(task.get("taskId")): task for task in response.get("tasks") or []
            }
        except Exception as e:
            _logger.warning(
                "Could not check %d task(s) before activating them: %s", len(addresses), e
            )
            return None

        # Release order is kept. A task the answer leaves out stays queued, and
        # the next poll checks it again.
        to_activate: List[TaskKey] = []
        running: List[TaskKey] = []
        for task_id, (key, _) in addresses.items():
            task: Optional[Dict[str, Any]] = tasks.get(task_id)
            if task is None:
                _logger.warning("Could not check task %s before activating it.", task_id)
                continue

            # A running task needs a recheck, in its place. An ended task goes
            # to ``_handle_result``, which finishes it unless that end is the
            # early run's or a failure the farm will retry. Both of those still
            # have to run. ``begin_activation`` skips any task
            # ``_handle_result`` finished.
            status: str = str(task.get("runStatus") or "")
            if status in _RUN_IN_PROGRESS:
                running.append(key)
                continue
            if status in _RUN_ENDED:
                self._handle_result(task, outcomes)
            to_activate.append(key)
        self._task_tracker.require_recheck(running)
        return to_activate

    def _activate_batch(self, batch: List[TaskKey]) -> Tuple[List[TaskKey], List[TaskKey]]:
        """One ``BatchUpdateTask``: the tasks it activated, and the tasks to retry."""
        # Truncating an oversized batch would lose tasks, and the service rejects it.
        if len(batch) > _BATCH_LIMIT:
            raise ValueError(
                f"BatchUpdateTask takes at most {_BATCH_LIMIT} tasks, not {len(batch)}."
            )
        # Every task here was checked, so every one has an address.
        addresses: Dict[str, Tuple[TaskKey, TaskAddress]] = self._addresses_by_task_id(batch)

        # Any error in the call or in its response retries the whole batch.
        # Activating a task twice does nothing, and losing one stalls the cook.
        try:
            response: Any = self._client.batch_update_task(
                tasks=[
                    {**_task_args(address), "targetRunStatus": "READY"}
                    for _, address in addresses.values()
                ]
            )
            rejected: Set[str] = _rejected_task_ids(response)
        except Exception as e:
            _logger.warning("Could not activate %d task(s): %s", len(addresses), e)
            return [], [key for key, _ in addresses.values()]
        activated: List[TaskKey] = [
            key for task_id, (key, _) in addresses.items() if task_id not in rejected
        ]
        to_retry: List[TaskKey] = [
            key for task_id, (key, _) in addresses.items() if task_id in rejected
        ]
        return activated, to_retry

    def _log_held_tasks(self, unblocked_steps: AbstractSet[str]) -> None:
        """Log the tasks waiting for their dependencies, only when the counts change."""
        # On change only: a large graph holds tasks for minutes.
        held: Dict[str, int] = self._task_tracker.held_count_by_step(unblocked_steps)
        if held == self._last_logged_held_counts:
            return
        self._last_logged_held_counts = held
        if held:
            detail: str = ", ".join(f"{count} in {name}" for name, count in sorted(held.items()))
            _logger.info(
                "Holding %d task(s) until the steps they depend on succeed: %s",
                sum(held.values()),
                detail,
            )

    def _search_ended_tasks(self, outcomes: List[TaskOutcome]) -> None:
        """Find the tasks that ended since the search window's start, and move the start after them."""
        request: Dict[str, Any] = self._search_arguments()
        ended_seen: List[datetime] = []
        # Each page names the offset of the next. Appending it continues the loop.
        offsets: List[int] = [0]
        for offset in offsets:
            # The service refuses an offset past its limit. The window's start
            # moves past what was read, so the next poll continues from there.
            if offset >= _OFFSET_LIMIT:
                _logger.warning(
                    "SearchTasks stopped at its %d-result limit. The rest are read next tick.",
                    _OFFSET_LIMIT,
                )
                break
            try:
                response: Dict[str, Any] = self._client.search_tasks(**request, itemOffset=offset)
            except Exception as e:
                # The window's start stays put, so the next poll asks again.
                _logger.warning("Could not search the tasks of job %s: %s", self._job_id, e)
                return

            # Every result moves the window's start, even one that names none of
            # the job's tasks.
            tasks: List[Dict[str, Any]] = response.get("tasks") or []
            for task in tasks:
                ended: Optional[datetime] = _as_utc(task.get("endedAt"))
                if ended is not None:
                    ended_seen.append(ended)
                self._handle_result(task, outcomes)

            # A short page, or an offset that does not move forward, is the end.
            following: Any = response.get("nextItemOffset")
            if len(tasks) < _PAGE_SIZE or not isinstance(following, int) or following <= offset:
                break
            offsets.append(following)
        if ended_seen:
            self._advance_search_ended_after(max(ended_seen))

    def _search_arguments(self) -> Dict[str, Any]:
        """``SearchTasks`` arguments: the final statuses, after the search window's start once there is one, oldest first."""
        filters: List[Dict[str, Any]] = [
            {
                "stringListFilter": {
                    "name": "RUN_STATUS",
                    "operator": "ANY_EQUALS",
                    "values": list(_SEARCHED_STATUSES),
                }
            }
        ]
        if self._search_ended_after is not None:
            filters.append(
                {
                    "dateTimeFilter": {
                        "name": "ENDED_AT",
                        "operator": "GREATER_THAN",
                        "dateTime": self._search_ended_after,
                    }
                }
            )
        return {
            "farmId": self._farm_id,
            "queueIds": [self._queue_id],
            "jobId": self._job_id,
            "filterExpressions": {"operator": "AND", "filters": filters},
            "sortExpressions": [{"fieldSort": {"name": "ENDED_AT", "sortOrder": "ASCENDING"}}],
            "pageSize": _PAGE_SIZE,
        }

    def _advance_search_ended_after(self, newest_seen: datetime) -> None:
        """Move the search window's start to trail the newest result, never backwards."""
        # Never past now minus the lag either, so one clock-skewed endedAt cannot
        # jump over every task still running.
        candidate: datetime = min(
            newest_seen - _SEARCH_WINDOW_LAG, self._now() - _SEARCH_WINDOW_LAG
        )
        if self._search_ended_after is None or candidate > self._search_ended_after:
            self._search_ended_after = candidate

    def _reconcile_activated_tasks(self, outcomes: List[TaskOutcome]) -> None:
        """Every minute, read each activated task that has no result yet, in case the search hasn't shown it."""
        now: datetime = self._now()
        if (
            self._last_reconciled_at is not None
            and now - self._last_reconciled_at < _RECONCILE_INTERVAL
        ):
            return
        self._last_reconciled_at = now

        # One BatchGetTask per hundred tasks. A failed call ends this reconcile;
        # the next one asks again.
        keys: List[TaskKey] = self._task_tracker.awaiting_result()
        for start in range(0, len(keys), _BATCH_LIMIT):
            identifiers: List[Dict[str, str]] = [
                _task_args(self.address(key)) for key in keys[start : start + _BATCH_LIMIT]
            ]
            try:
                response: Dict[str, Any] = self._client.batch_get_task(identifiers=identifiers)
            except Exception as e:
                _logger.warning("Could not reconcile %d task(s): %s", len(identifiers), e)
                return
            for task in response.get("tasks") or []:
                self._handle_result(task, outcomes)
            for error in response.get("errors") or []:
                _logger.debug(
                    "Could not reconcile task %s: %s", error.get("taskId"), error.get("code")
                )

    def _handle_result(self, task: Dict[str, Any], outcomes: List[TaskOutcome]) -> None:
        """Decide what to do with one task the service returned: append its outcome when it's a new final result for a released task, return a cancellation once, and log a run the scheduler node didn't start once."""
        # A result that names none of the job's tasks has no work item to go to.
        step_id: str = str(task.get("stepId") or "")
        task_id: str = str(task.get("taskId") or "")
        key: Optional[TaskKey] = self._key_for_task(step_id, task_id)
        if key is None:
            _logger.error(
                "Task %s in step %s is not one of this job's tasks, so its result is ignored.",
                task_id,
                step_id,
            )
            return

        # A cancellation is returned once and never marks the task final: the
        # task can still run again if someone requeues it.
        status: str = str(task.get("runStatus") or "")
        ended: Optional[datetime] = _as_utc(task.get("endedAt"))
        if status == _CANCELED:
            if key not in self._cancellation_returned:
                self._cancellation_returned.add(key)
                outcomes.append(TaskOutcome(key=key, status=TaskStatus.CANCELED, ended_at=ended))
            return

        # A failure the farm will retry is not final. If the scheduler node never
        # activated the task, the farm still ran it on its own, which is logged
        # once, as for any other result.
        if status == _FAILED and self._farm_will_retry(task):
            if self._task_tracker.record_retryable_failure(key):
                _log_run_not_started_by_scheduler_node(key, status, result_used=False)
            return
        final: Optional[TaskStatus] = _pdg_status(status)
        if final is None:
            return

        # Each final result is returned once, and only for a released task. If
        # the farm ran the task before the scheduler node activated it, that run
        # is logged once, however often the search shows it.
        decision: _ResultDecision = self._task_tracker.record_result(key, ended)
        if decision.log_run_not_started_by_scheduler_node:
            _log_run_not_started_by_scheduler_node(key, status, result_used=decision.return_to_cook)
        if decision.return_to_cook:
            outcomes.append(TaskOutcome(key=key, status=final, ended_at=ended))

    def _farm_will_retry(self, task: Dict[str, Any]) -> bool:
        """Whether the farm will run a failed task again."""
        # Until the budget is read, assume a retry: reporting a failure the farm
        # then retries would fail a work item that may still succeed.
        if self._max_retries_per_task is None:
            return True
        # failureRetryCount counts the retries already used. A task without a
        # readable count is treated as out of retries.
        used: Any = task.get("failureRetryCount")
        return _is_count(used) and bool(used < self._max_retries_per_task)

    def _key_for_task(self, step_id: str, task_id: str) -> Optional[TaskKey]:
        """The key a farm task ID names, or None for an ID that names none of the job's tasks."""
        # The step must be one of this job's, and the rest of the ID an index in
        # range, written as the service writes it.
        name: Optional[str] = self._step_name_by_id.get(step_id)
        step: Optional[StepSpec] = self._snapshot.step(name) if name else None
        if step is None:
            return None
        prefix: str = f"task-{step_id[len('step-'):]}-"
        suffix: str = task_id[len(prefix) :] if task_id.startswith(prefix) else ""
        if not _INDEX.fullmatch(suffix):
            return None
        index: int = int(suffix)
        return TaskKey(step.name, index) if index < step.task_count else None

    # -- helpers --

    def _job_args(self) -> Dict[str, str]:
        """The arguments that name this job in every call."""
        return {"farmId": self._farm_id, "queueId": self._queue_id, "jobId": self._job_id}

    def _now(self) -> datetime:
        """The clock's time, read as UTC when it has no zone."""
        now: datetime = self._clock.now()
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    @staticmethod
    def _paginate(
        operation: Callable[..., Dict[str, Any]], result_key: str, **kwargs: Any
    ) -> Iterator[Dict[str, Any]]:
        """Every item of a ``nextToken``-paginated listing."""
        # A truncated step list would leave a whole step unresolvable. Each page
        # names the token of the next; appending it continues the loop.
        tokens: List[Optional[str]] = [None]
        for token in tokens:
            # The first call carries no token. The last page names no next one.
            response: Dict[str, Any] = operation(
                **kwargs, **({"nextToken": token} if token else {})
            )
            yield from response.get(result_key) or []
            following: Optional[str] = response.get("nextToken")
            if not following:
                return
            tokens.append(following)


def _derived_task_id(step_id: str, task_index: int) -> str:
    """The farm ID of the task with this index in this step."""
    # Measured across thousands of tasks: a task's ID is its step's hex body and
    # its TaskIndex, which holds while the step has one dense INT parameter.
    # ``start`` verifies it on every step before anything relies on it.
    return f"task-{step_id[len('step-'):]}-{task_index}"


def _task_args(address: TaskAddress) -> Dict[str, str]:
    """The five IDs that name one task in a batch call."""
    return {
        "farmId": address.farm_id,
        "queueId": address.queue_id,
        "jobId": address.job_id,
        "stepId": address.step_id,
        "taskId": address.task_id,
    }


def _rejected_task_ids(response: Any) -> Set[str]:
    """The task IDs a ``BatchUpdateTask`` response rejected, each one logged."""
    errors: List[Dict[str, Any]] = (response or {}).get("errors") or []
    for error in errors:
        _logger.warning(
            "Could not activate task %s: %s %s",
            error.get("taskId", "?"),
            error.get("code", "?"),
            error.get("message", ""),
        )
    return {str(error.get("taskId", "")) for error in errors}


def _task_index_of(task: Dict[str, Any]) -> Optional[int]:
    """``TaskIndex`` from the task's parameters, which the service returns as ``{"int": "N"}``."""
    raw: Any = (task.get("parameters") or {}).get("TaskIndex")
    value: Any = raw.get("int") if isinstance(raw, dict) else raw
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_count(value: Any) -> bool:
    """Whether a service field holds a whole number of zero or more. A bool is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _pdg_status(status: str) -> Optional[TaskStatus]:
    """What a run status means for PDG, or None for a status that is not a result.

    The caller has already set aside a failure the farm will retry.
    """
    # SUCCEEDED succeeds, FAILED and NOT_COMPATIBLE fail, and any other status
    # is not final yet.
    if status == _SUCCEEDED:
        return TaskStatus.SUCCEEDED
    if status in _FAILURES:
        return TaskStatus.FAILED
    return None


def _latest_session_id(actions: List[Dict[str, Any]]) -> str:
    """``session-<hex>`` for the most recently started action.

    A session action's ID embeds its session's. The summaries carry no
    ``sessionId``.
    """
    # An action without a start time sorts first, and a later action with the
    # same start time wins.
    latest: str = ""
    latest_at: datetime = _EPOCH
    for action in actions:
        match: Optional[re.Match[str]] = _SESSION_ACTION_ID.fullmatch(
            str(action.get("sessionActionId") or "")
        )
        if not match:
            continue
        started: datetime = _as_utc(action.get("startedAt")) or _EPOCH
        if not latest or started >= latest_at:
            latest, latest_at = f"session-{match.group(1)}", started
    return latest


def _as_utc(value: Any) -> Optional[datetime]:
    """A service timestamp as an aware datetime, read as UTC when it has no zone, or None."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _later_end(first: Optional[datetime], second: Optional[datetime]) -> Optional[datetime]:
    """The later of two end times, where a missing one counts as earliest."""
    known: List[datetime] = [end for end in (first, second) if end is not None]
    return max(known) if known else None


def _ended_after(ended: Optional[datetime], early: Optional[datetime]) -> bool:
    """Whether a result ended after a task's early run, so it comes from a later run.

    Both times are on the service's clock. A finished task runs again only when
    it is set ready, so a later end is a later run.
    """
    return ended is not None and (early is None or ended > early)


def _log_run_not_started_by_scheduler_node(key: TaskKey, status: str, result_used: bool) -> None:
    """Log that the farm ran a task before the scheduler node started it, and whether that run's result goes to the cook.

    The artist needs to know: such a run may have come before the work item's
    payload was stored.
    """
    _logger.error(
        "Task %s[%d] reached %s, but the farm ran it before the Deadline Cloud Scheduler node started it. %s",
        key.step_name,
        key.task_index,
        status,
        # The not-used sentence promises no later run: the farm may run the task
        # again on its own before the scheduler node activates it, and that run's
        # result then goes to the cook.
        "Its result is reported." if result_used else "Its result is not used.",
    )
