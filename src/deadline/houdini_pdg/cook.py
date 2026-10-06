# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler's core: one cook at a time, from ``onStartCook`` to ``onStopCook``.

PDG calls the scheduler from three threads. Each cook is a new :class:`Cook`, so
nothing carries over from the previous one, and every phase change is a
compare-and-set, so a submission still running when the cook stops cannot write a
phase afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import AbstractSet, Callable, FrozenSet, Mapping, Optional

from .diagnostics import CookJournal
from .graph import GraphSnapshot
from .houdini.settings import SchedulerSettings
from .ports import (
    Clock,
    CookPhase,
    CookSummary,
    FarmJob,
    JobSubmitter,
    PdgHost,
    TaskStorage,
    WorkItemHandle,
)


class ScheduleResult(Enum):
    """The answer to ``onSchedule``."""

    SUCCEEDED = 0
    FAILED = 1
    DEFERRED = 2


def _allowed(*phases: CookPhase) -> FrozenSet[CookPhase]:
    # onStopCook may close a cook from any phase.
    return frozenset(phases) | {CookPhase.IDLE}


#: The phases each phase may move to. DONE, REFUSED, and FAILED hold until the cook stops.
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
        raise NotImplementedError

    def phase(self) -> CookPhase:
        """The current phase."""
        raise NotImplementedError

    def schedule(self, item: WorkItemHandle) -> ScheduleResult:
        """Submit on the first call, then store the item's payload and release its task.

        A failed upload fails the item, and its task is never released.
        """
        raise NotImplementedError

    def tick(self) -> None:
        """Release, poll, report, and check the job's health. Never raises.

        Each part runs in its own guard, so one failing does not stop the others.
        """
        raise NotImplementedError

    def close(self, *, cancel: bool) -> CookSummary:
        """End the cook, cancelling its job when asked."""
        raise NotImplementedError


class PdgScheduler:
    """What the PDG callbacks reach. Holds at most one open :class:`Cook`."""

    def __init__(
        self,
        *,
        host: PdgHost,
        clock: Clock,
        read_settings: Callable[[], SchedulerSettings],
        connect: Callable[[SchedulerSettings], CookServices],
    ) -> None:
        raise NotImplementedError

    def on_start_cook(self, static: bool, cook_set: Optional[AbstractSet[str]] = None) -> bool:
        """Start a cook. A static cook needs no farm and is done at once."""
        raise NotImplementedError

    def on_schedule(self, item: WorkItemHandle) -> ScheduleResult:
        """Hand a work item to the open cook, or fail it when there is none."""
        raise NotImplementedError

    def on_tick(self) -> None:
        """Advance the open cook. Never raises."""
        raise NotImplementedError

    def on_stop_cook(self, cancel: bool) -> bool:
        """Close the open cook, cancelling its job when asked."""
        raise NotImplementedError


class SystemClock:
    """The production :class:`~deadline.houdini_pdg.ports.Clock`."""

    def now(self) -> datetime:
        """Timezone-aware, in UTC."""
        return datetime.now(timezone.utc)
