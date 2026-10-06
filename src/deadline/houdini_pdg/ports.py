# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The seams between a cook and the outside world, and the values that cross them.

The cook depends only on these protocols. Houdini, the Deadline Cloud API, and S3
sit behind them, so a unit test replaces each one with a small fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .graph import DeclaredFile, GraphSnapshot, NodeInfo, TaskKey
from .scene_reads import SceneReads
from .validation import RefusalScenario


@dataclass(frozen=True)
class TaskAddress:
    """The IDs that name one task on the farm."""

    farm_id: str
    queue_id: str
    job_id: str
    step_id: str
    task_id: str

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "TaskAddress":
        """Read the IDs from a worker session's ``DEADLINE_*`` variables, ``os.environ`` by default.

        Raises :class:`~deadline.houdini_pdg.errors.PayloadTransferError` naming
        every variable that is missing or empty.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class OutputFile:
    """One file a task produced."""

    path: str
    #: PDG routes a file downstream by its tag. Empty means PDG infers it from the extension.
    tag: str = ""


@dataclass(frozen=True)
class WorkItemHandle:
    """A work item PDG offered, as the cook sees it."""

    item_id: int
    index: int
    name: str
    node_name: str
    #: Read at ``onSchedule``. PDG can change them after generation.
    declared_outputs: Tuple[DeclaredFile, ...] = ()
    #: The live PDG object. Only the Houdini adapter reads it.
    native: Any = field(default=None, compare=False, repr=False)


class ItemState(Enum):
    """What PDG is told about a work item."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ItemOutcome:
    """One update for PDG about one work item."""

    state: ItemState
    #: Only for :attr:`ItemState.SUCCEEDED`.
    outputs: Tuple[OutputFile, ...] = ()
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class SceneFiles:
    """The files every task needs: the saved ``.hip`` and the scene's assets."""

    hip_file: str
    #: Absolute and fully expanded.
    assets: Tuple[str, ...] = ()


@dataclass(frozen=True)
class JobStatus:
    """The job as ``GetJob`` reports it. Empty strings mean the read failed."""

    lifecycle_status: str = ""
    task_run_status: str = ""
    task_run_status_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskOutcome:
    """A task that reached a final state.

    ``status`` is ``SUCCEEDED``, ``FAILED``, or ``CANCELED``. A failure the farm will
    retry is not final, and is never an outcome.
    """

    key: TaskKey
    status: str
    ended_at: Optional[datetime] = None


class CookPhase(Enum):
    """Where a cook is.

    ``REFUSED`` means nothing was submitted. ``FAILED`` means the cook broke,
    possibly with a job on the farm. ``DONE`` is success, or a static cook that
    needed no farm.
    """

    IDLE = "Idle"
    #: Settings read and the farm reachable. Nothing is submitted until PDG offers
    #: the first work item, because the graph is not generated before then.
    CONNECTED = "Connected"
    SNAPSHOTTING = "Snapshotting"
    BUILDING = "Building"
    SUBMITTING = "Submitting"
    RESOLVING = "Resolving"
    COOKING = "Cooking"
    DRAINING = "Draining"
    DONE = "Done"
    REFUSED = "Refused"
    FAILED = "Failed"


@dataclass(frozen=True)
class CookSummary:
    """How a cook ended."""

    cook_id: str
    final_phase: CookPhase
    job_id: str = ""
    step_count: int = 0
    task_count: int = 0
    succeeded: int = 0
    failed: int = 0
    #: Tasks the farm canceled, for example after another task failed. Not
    #: reported to PDG as failures, because they never ran.
    canceled: int = 0
    #: Items PDG satisfied from its cache. They count as complete.
    cached: int = 0
    refusals: Tuple[RefusalScenario, ...] = ()
    stopped_by_user: bool = False


class PdgHost(Protocol):
    """PDG and the Houdini scene."""

    def read_graph(self) -> Sequence[NodeInfo]:
        """The TOP topology, merged with the items PDG has generated."""
        ...

    def read_scene(self) -> SceneReads:
        """The file references each step's scene will read."""
        ...

    def scene_files(self) -> SceneFiles:
        """The ``.hip`` and the scene assets to attach."""
        ...

    def serialize(self, item: WorkItemHandle) -> str:
        """PDG's JSON for a work item: the payload a worker needs."""
        ...

    def notify(self, item: WorkItemHandle, outcome: ItemOutcome) -> None:
        """Tell PDG a work item started, succeeded, or failed."""
        ...

    def set_working_dir(self, path: str) -> None:
        """Make ``path`` PDG's local and remote working directory."""
        ...


class JobSubmitter(Protocol):
    """Creates the cook's job."""

    def submit(self, snapshot: GraphSnapshot, bundle_dir: str) -> str:
        """Write the job bundle to ``bundle_dir``, create the job suspended, and return its ID."""
        ...


class FarmJob(Protocol):
    """A submitted job, released task by task."""

    def start(self) -> None:
        """Learn every task's farm ID and suspend the steps that have dependencies.

        Raises :class:`~deadline.houdini_pdg.errors.TaskMapIncompleteError` or
        :class:`~deadline.houdini_pdg.errors.TaskSuspensionError`.
        """
        ...

    def address(self, key: TaskKey) -> TaskAddress:
        """Where the task's payload and output record live."""
        ...

    def release(self, key: TaskKey) -> None:
        """Let the task run once every step it depends on has succeeded on the farm. Never blocks."""
        ...

    def poll(self) -> List[TaskOutcome]:
        """Run the released tasks that may now run, and return the outcomes not returned before. Never raises."""
        ...

    def status(self) -> JobStatus:
        """The job's status. Never raises."""
        ...

    def cancel(self) -> None:
        """Cancel the job. Never raises."""
        ...

    def log_tail(self, key: TaskKey, lines: int) -> List[str]:
        """The last lines of the task's latest session log, or ``[]``."""
        ...


class TaskStorage(Protocol):
    """Payloads and output records, stored at a task's address. Both the scheduler and the worker use it."""

    def put_payload(self, address: TaskAddress, payload_json: str) -> None:
        """Store a work item payload. Raises ``PayloadTransferError`` naming the key."""
        ...

    def get_payload(self, address: TaskAddress) -> Dict[str, Any]:
        """Fetch and decode a payload. Raises ``PayloadTransferError``."""
        ...

    def put_outputs(self, address: TaskAddress, outputs: Sequence[OutputFile]) -> None:
        """Publish what a task produced, for a step that declares no outputs."""
        ...

    def get_outputs(self, address: TaskAddress) -> Optional[List[OutputFile]]:
        """What a task published, or None when it published nothing.

        Raises :class:`~deadline.houdini_pdg.errors.OutputManifestError` for a record
        it cannot parse.
        """
        ...


class Clock(Protocol):
    """The current time."""

    def now(self) -> datetime:
        """Timezone-aware, in UTC."""
        ...
