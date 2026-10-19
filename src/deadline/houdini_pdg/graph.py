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
from functools import cached_property
from typing import AbstractSet, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

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
    #: Whether the cook being read will schedule the item, from its PDG state.
    #: None when unknown.
    in_cook: Optional[bool] = None


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
    #: The node's Cache Mode, as its menu shows it, for example ``Write Files``.
    #: None when unknown.
    cache_mode: Optional[str] = None
    #: The Houdini node type name, for example ``houdiniserver``, or ``""`` when it
    #: cannot be read. ``top_type`` cannot tell a command server from a node that
    #: renders, and this can.
    node_type_name: str = ""

    @property
    def is_schedulable(self) -> bool:
        """Whether the node becomes a step: :attr:`schedulable` when known, else whether any item has a command."""
        if self.schedulable is not None:
            return self.schedulable
        return any(item.command for item in self.items)


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
        return bool(self.declared_outputs)


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
        return len(self.items)

    @property
    def task_range(self) -> str:
        """The INT range ``"0-{N-1}"``. A list range would cap the step at 1,024 tasks."""
        if not self.items:
            raise ValueError(f"Step '{self.name}' has no tasks, so it has no task range.")
        return f"0-{self.task_count - 1}"

    @property
    def declares_outputs(self) -> bool:
        """Whether every item declares its outputs."""
        return all(item.declares_outputs for item in self.items)


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
        return sum(step.task_count for step in self.steps)

    @property
    def step_names(self) -> Tuple[str, ...]:
        """Step names, dependencies first."""
        return tuple(step.name for step in self.steps)

    def step(self, name: str) -> Optional[StepSpec]:
        """The step with this name, or None."""
        return self._steps_by_name.get(name)

    def item(self, key: TaskKey) -> Optional[WorkItemSpec]:
        """The work item a task was reserved for, or None."""
        return self._items_by_key.get(key)

    def item_by_name(self, name: str) -> Optional[WorkItemSpec]:
        """The work item with this PDG name, or None. How ``onSchedule`` finds its task."""
        return self._items_by_name.get(name)

    def leaf_steps(self) -> Tuple[str, ...]:
        """Steps no other step depends on."""
        consumed: Set[str] = {dep for step in self.steps for dep in step.dependencies}
        return tuple(step.name for step in self.steps if step.name not in consumed)

    # Lookups run once per scheduled item and once per step per tick, so they are
    # indexed once. ``cached_property`` writes the instance dict directly, which a
    # frozen dataclass allows, and the indexes take no part in equality.
    @cached_property
    def _steps_by_name(self) -> Dict[str, StepSpec]:
        return {step.name: step for step in self.steps}

    @cached_property
    def _items_by_name(self) -> Dict[str, WorkItemSpec]:
        return {item.name: item for step in self.steps for item in step.items}

    @cached_property
    def _items_by_key(self) -> Dict[TaskKey, WorkItemSpec]:
        # Keyed by each item's own task index, not its position, so a hand-built
        # step whose indices are not 0..N-1 still finds the right item. Built from
        # the steps :meth:`step` returns, and the first item wins a repeated index.
        by_key: Dict[TaskKey, WorkItemSpec] = {}
        for step in self._steps_by_name.values():
            for item in step.items:
                by_key.setdefault(TaskKey(step.name, item.task_index), item)
        return by_key


def synthesize(
    nodes: Sequence[NodeInfo],
    *,
    data_edges: Optional[Mapping[str, AbstractSet[str]]] = None,
) -> GraphSnapshot:
    """Map the TOP nodes, plus any data edges by consuming step, to the job's shape.

    Never refuses: whether the shape can be cooked is validation's decision. The
    result does not depend on the order of ``nodes``.
    """
    by_name: Dict[str, NodeInfo] = {node.name: node for node in sorted(nodes, key=lambda n: n.name)}
    schedulable: Set[str] = {name for name, node in by_name.items() if node.is_schedulable}

    projected: Dict[str, Set[str]] = _project(by_name, schedulable)
    merged, accepted, refused = _merge_data_edges(projected, data_edges or {}, schedulable)
    # No edge is pruned, even one a longer path implies: a worker syncs outputs only
    # from its step's direct dependencies, so removing an edge removes files.
    order: List[str] = _topological_order(schedulable, merged)

    # The step nodes that generated items. Only these become steps; the rest are
    # listed in ``steps_without_items``.
    steps_with_items: Set[str] = {name for name in schedulable if by_name[name].items}
    steps: List[StepSpec] = []
    remapped: List[str] = []
    on_dropped: Dict[str, Tuple[str, ...]] = {}
    # Build each kept step in order.
    for name in order:
        if name not in steps_with_items:
            continue
        node: NodeInfo = by_name[name]
        items, was_remapped = _dense_items(node)
        if was_remapped:
            remapped.append(name)
        # A dependency on a step without items is moved to ``on_dropped``,
        # because the job has no such step to depend on.
        dependencies: Set[str] = merged.get(name, set())
        kept: Set[str] = dependencies & steps_with_items
        if dependencies - kept:
            on_dropped[name] = tuple(sorted(dependencies - kept))
        steps.append(
            StepSpec(
                name=name,
                dependencies=tuple(sorted(kept)),
                items=items,
                data_dependencies=tuple(sorted(accepted.get(name, set()) & kept)),
                exported_attribute_names=node.exported_attribute_names,
            )
        )

    return GraphSnapshot(
        steps=tuple(steps),
        skipped_nodes=tuple(sorted(set(by_name) - schedulable)),
        remapped_steps=tuple(remapped),
        steps_without_items=tuple(sorted(schedulable - steps_with_items)),
        refused_data_edges={name: tuple(sorted(deps)) for name, deps in sorted(refused.items())},
        dependencies_on_dropped=on_dropped,
    )


def _project(by_name: Mapping[str, NodeInfo], schedulable: AbstractSet[str]) -> Dict[str, Set[str]]:
    """Each step's nearest schedulable upstream nodes, walking through the nodes that map to nothing.

    Iterative with a visited set, so a cyclic or dangling ``upstream`` terminates.
    """
    # Walk upstream from each step. A step ends the walk along that path, and
    # anything else is walked through.
    projected: Dict[str, Set[str]] = {}
    for name in schedulable:
        found: Set[str] = set()
        seen: Set[str] = set()
        stack: List[str] = list(by_name[name].upstream)
        while stack:
            # Each name is visited once, so a cycle ends the walk.
            current: str = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in schedulable:
                found.add(current)
                continue
            # A name that matches no node has no upstream to walk.
            node: Optional[NodeInfo] = by_name.get(current)
            if node is not None:
                stack.extend(node.upstream)
        found.discard(name)
        projected[name] = found
    return projected


def _ancestors(deps: Mapping[str, AbstractSet[str]], start: str) -> Set[str]:
    """Every name ``start`` depends on, directly or through others. Terminates on a cycle."""
    # Depth-first walk with a visited set, so a cycle is walked once and ends.
    seen: Set[str] = set()
    stack: List[str] = list(deps.get(start, ()))
    while stack:
        current: str = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(deps.get(current, ()))
    return seen


def _merge_data_edges(
    projected: Mapping[str, Set[str]],
    requested: Mapping[str, AbstractSet[str]],
    schedulable: AbstractSet[str],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Fold data edges into the projected graph: ``(merged, accepted, refused)``.

    An edge already projected counts as accepted. An edge that would close a cycle
    is refused, because ``dependsOn`` must stay acyclic. Requests are taken in
    sorted order, so which of two mutually cyclic edges wins is deterministic.
    """
    merged: Dict[str, Set[str]] = {name: set(deps) for name, deps in projected.items()}
    accepted: Dict[str, Set[str]] = {}
    refused: Dict[str, Set[str]] = {}
    # Only an edge between two distinct steps can become a dependency; the rest
    # are ignored rather than refused.
    for name in sorted(requested):
        if name not in schedulable:
            continue
        for dep in sorted(requested[name]):
            if dep == name or dep not in schedulable:
                continue
            # Already wired, would close a cycle, or new.
            if dep in merged[name]:
                accepted.setdefault(name, set()).add(dep)
            elif name in _ancestors(merged, dep):
                refused.setdefault(name, set()).add(dep)
            else:
                merged[name].add(dep)
                accepted.setdefault(name, set()).add(dep)
    return merged, accepted, refused


def _topological_order(names: Iterable[str], deps: Mapping[str, AbstractSet[str]]) -> List[str]:
    """Order ``names`` dependencies first, one layer at a time, alphabetical within a layer.

    Each round emits every name whose dependencies have all been emitted, sorted, so
    the same graph gives the same order whatever order its nodes arrive in, and the
    template reads from inputs to outputs. ``dependsOn`` itself needs no order, only
    no cycle. A dependency outside ``names`` never blocks. When a round finds nothing
    ready, what remains is a cycle or depends on one: it is emitted sorted, so the
    call returns instead of looping forever.
    """
    # One sorted layer per round.
    remaining: Dict[str, Set[str]] = {name: set(deps.get(name, ())) for name in names}
    ordered: List[str] = []
    while remaining:
        ready: List[str] = sorted(n for n, d in remaining.items() if not (d & remaining.keys()))
        if not ready:
            ordered.extend(sorted(remaining))
            break
        ordered.extend(ready)
        for name in ready:
            del remaining[name]
    return ordered


def _dense_items(node: NodeInfo) -> Tuple[Tuple[WorkItemSpec, ...], bool]:
    """Items ordered by ``(index, name)`` with task indices 0..N-1, and whether PDG's had gaps."""
    ordered: List[RawItem] = sorted(node.items, key=lambda item: (item.index, item.name))
    items: Tuple[WorkItemSpec, ...] = tuple(
        WorkItemSpec(
            name=raw.name,
            task_index=position,
            pdg_index=raw.index,
            item_id=raw.item_id,
            node_name=node.name,
            declared_outputs=raw.declared_outputs,
            declared_inputs=raw.declared_inputs,
            command=raw.command,
        )
        for position, raw in enumerate(ordered)
    )
    # Only records whether PDG's indices had gaps, for ``remapped_steps``. The items
    # are dense either way: the task range is 0-{N-1} and task IDs are derived from
    # the task index, so a gap would name tasks that do not exist.
    had_gaps: bool = [raw.index for raw in ordered] != list(range(len(ordered)))
    return items, had_gaps
