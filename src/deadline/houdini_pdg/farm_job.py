# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""A submitted job, released task by task through the Deadline Cloud API."""

from __future__ import annotations

from typing import Any, List

from .graph import GraphSnapshot, TaskKey
from .ports import Clock, JobStatus, TaskAddress, TaskOutcome


class DeadlineFarmJob:
    """Implements ``FarmJob``.

    Activating a task also lets it skip the wait for its step dependencies. So a
    released task is activated only after every step it depends on reads
    ``SUCCEEDED`` on the farm. The scheduler's own count of reported items never
    releases anything.
    """

    def __init__(
        self,
        *,
        deadline_client: Any,
        farm_id: str,
        queue_id: str,
        job_id: str,
        snapshot: GraphSnapshot,
        clock: Clock,
    ) -> None:
        raise NotImplementedError

    def start(self) -> None:
        """Derive every task ID, prove the derivation on each step, and suspend the steps that have dependencies.

        Suspension is one call per dependent step. A job-wide call lands
        asynchronously and can overwrite activations made after it.
        """
        raise NotImplementedError

    def address(self, key: TaskKey) -> TaskAddress:
        """Where the task's payload and output record live."""
        raise NotImplementedError

    def release(self, key: TaskKey) -> None:
        """Queue the task to run once its step dependencies have succeeded."""
        raise NotImplementedError

    def poll(self) -> List[TaskOutcome]:
        """Activate what the dependencies allow, then return new final outcomes.

        Searches by ``endedAt``, never ``updatedAt``: ``updatedAt`` changes only when
        a caller touches a task, not when a worker finishes it. The search index
        publishes late and out of order, so the window trails the newest result,
        and a periodic reconcile catches anything it still misses.
        """
        raise NotImplementedError

    def status(self) -> JobStatus:
        """The job's status, or an empty one when the read fails."""
        raise NotImplementedError

    def cancel(self) -> None:
        """Cancel the job, logging a failure instead of raising it."""
        raise NotImplementedError

    def log_tail(self, key: TaskKey, lines: int) -> List[str]:
        """The last lines of the task's latest session log, through ``deadline.client.api.get_session_logs``."""
        raise NotImplementedError
