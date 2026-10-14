# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Map a TOP network to the shape of one Deadline Cloud job.

* A TOP node becomes a **step** when its work items run a command on a worker.
  Partitioners, wedges, and other in-process nodes map to nothing.
* Each work item becomes a **task** in its node's step.
* TOP wires become step dependencies, with the nodes that map to nothing
  collapsed into the edge across them.
* A step also depends on any step whose outputs its scene reads, even when no TOP
  wire says so. These are the data edges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AbstractSet, Mapping, Optional, Sequence, Tuple

#: The token PDG writes for the cook's working directory in declared paths.
PDG_DIR_TOKEN = "__PDG_DIR__"

#: The prefix of every PDG path token. A path that still holds one is resolved
#: on the worker, not on the workstation.
PDG_TOKEN_PREFIX = "__PDG_"


@dataclass(frozen=True)
class DeclaredFile:
    """A file PDG declares, with its tag.

    PDG hands a file downstream by matching its tag, so the right file with the
    wrong tag is invisible to the consuming node.
    """

    path: str
    tag: str


@dataclass(frozen=True)
class RawItem:
    """One work item as PDG reports it."""

    name: str
    index: int
    item_id: int
    command: str
    is_out_of_process: bool = False
    #: True for a batch parent.
    is_batch: bool = False
    #: The batch parent's name, or ``""`` when the item is not batched.
    batch_parent: str = ""
    declared_outputs: Tuple[DeclaredFile, ...] = ()
    declared_inputs: Tuple[DeclaredFile, ...] = ()
    #: The upstream work items this one waits on. Steps keep only the node-level
    #: edge, so this is the only record of the item-level one.
    depends_on: Tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeInfo:
    """One TOP node as PDG reports it."""

    name: str
    is_dynamic: bool
    top_type: str
    upstream: Tuple[str, ...]
    items: Tuple[RawItem, ...]
    #: Attributes flagged ``EnvExport``, spelled as PDG spells them. Internal
    #: attributes such as ``hip`` are left out, or ``$HIP`` would look like a
    #: misspelled attribute.
    exported_attribute_names: Tuple[str, ...] = ()
    #: Whether the node runs a command on a worker, known from the node itself
    #: before it generates items. None when unknown.
    schedulable: Optional[bool] = None
    #: Whether PDG asked to cook this node. None when unknown.
    in_cook_set: Optional[bool] = None
    #: ``InProcess``, ``OutOfProcess``, or ``Service``. None when unknown.
    cook_type: Optional[str] = None
    #: The scheduler this node overrides to, or ``""``.
    scheduler_override: str = ""

    @property
    def is_schedulable(self) -> bool:
        """Whether the node becomes a step: :attr:`schedulable` when known, else whether any item has a command."""
        raise NotImplementedError


@dataclass(frozen=True, order=True)
class TaskKey:
    """A task's identity before the farm assigns IDs: its step and its task index."""

    step_name: str
    task_index: int


@dataclass(frozen=True)
class WorkItemSpec:
    """One work item, mapped to a task."""

    name: str
    #: The OpenJD ``TaskIndex``: dense, 0 to N-1.
    task_index: int
    #: PDG's own index. Differs from :attr:`task_index` when PDG's indices have gaps.
    pdg_index: int
    item_id: int
    node_name: str
    declared_outputs: Tuple[DeclaredFile, ...] = ()
    declared_inputs: Tuple[DeclaredFile, ...] = ()
    #: The command at generation time, to detect a rewrite before ``onSchedule``.
    command: str = ""

    @property
    def declares_outputs(self) -> bool:
        """Whether PDG knows this item's outputs in advance, so reporting needs no S3 call."""
        raise NotImplementedError


@dataclass(frozen=True)
class StepSpec:
    """One schedulable TOP node, mapped to a step."""

    name: str
    #: Every step this one depends on, emitted as ``dependsOn``.
    dependencies: Tuple[str, ...]
    items: Tuple[WorkItemSpec, ...]
    #: The dependencies that exist because this step reads the other's outputs.
    #: A worker syncs outputs only from direct dependencies, so these are never
    #: pruned, even when another path implies the order.
    data_dependencies: Tuple[str, ...] = ()
    exported_attribute_names: Tuple[str, ...] = ()

    @property
    def task_count(self) -> int:
        """One task per work item."""
        raise NotImplementedError

    @property
    def task_range(self) -> str:
        """The INT range ``"0-{N-1}"``. A list range would cap the step at 1,024 tasks."""
        raise NotImplementedError

    @property
    def declares_outputs(self) -> bool:
        """Whether every item declares its outputs."""
        raise NotImplementedError


@dataclass(frozen=True)
class GraphSnapshot:
    """The job's shape, fixed before submission. The template is built from it and tasks are keyed by it."""

    #: Dependencies first.
    steps: Tuple[StepSpec, ...]
    #: Nodes that map to no step.
    skipped_nodes: Tuple[str, ...] = ()
    #: Steps whose PDG indices had gaps and were made dense.
    remapped_steps: Tuple[str, ...] = ()
    #: Step nodes left out because they generated no items. Validation decides
    #: whether that is a refusal.
    steps_without_items: Tuple[str, ...] = ()
    #: Data edges left out because they would close a cycle, by consuming step.
    refused_data_edges: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    #: Dependencies left out because they named a step without items, by consuming step.
    dependencies_on_dropped: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    @property
    def total_tasks(self) -> int:
        """Every task in the job."""
        raise NotImplementedError

    @property
    def step_names(self) -> Tuple[str, ...]:
        """Step names, dependencies first."""
        raise NotImplementedError

    def step(self, name: str) -> Optional[StepSpec]:
        """The step with this name, or None."""
        raise NotImplementedError

    def item(self, key: TaskKey) -> Optional[WorkItemSpec]:
        """The work item a task was reserved for, or None."""
        raise NotImplementedError

    def item_by_name(self, name: str) -> Optional[WorkItemSpec]:
        """The work item with this PDG name, or None. How ``onSchedule`` finds its task."""
        raise NotImplementedError

    def leaf_steps(self) -> Tuple[str, ...]:
        """Steps no other step depends on."""
        raise NotImplementedError


def synthesize(
    nodes: Sequence[NodeInfo],
    *,
    data_edges: Optional[Mapping[str, AbstractSet[str]]] = None,
) -> GraphSnapshot:
    """Map the TOP nodes, plus any data edges by consuming step, to the job's shape.

    Never refuses: whether the shape can be cooked is validation's decision. The
    result does not depend on the order of ``nodes``.
    """
    raise NotImplementedError
