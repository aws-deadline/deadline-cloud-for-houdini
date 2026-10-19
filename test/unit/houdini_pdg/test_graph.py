# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for mapping a TOP network to the shape of one job.

The module is pure, so every test builds its input value types directly: there is
no fake, mock, or patch. The property tests check against independent models, a
brute-force path enumeration and a transitive closure, not against a copy of the
code under test.
"""

from __future__ import annotations

# Standard library, hypothesis for the "for any graph" properties, and the module's
# public surface only.
import copy
import random
from dataclasses import replace
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from deadline.houdini_pdg.graph import (
    DeclaredFile,
    GraphSnapshot,
    NodeInfo,
    RawItem,
    StepSpec,
    TaskKey,
    WorkItemSpec,
    synthesize,
)


def _item(name: str, index: int, command: str = "run", **kwargs: Any) -> RawItem:
    """A work item whose ``item_id`` is derived from its index, so tests can predict it."""
    return RawItem(name=name, index=index, item_id=index + 100, command=command, **kwargs)


def _items(node: str, count: int, command: str = "run") -> Tuple[RawItem, ...]:
    """``count`` items named ``<node>_<i>`` with dense indices 0..count-1."""
    return tuple(_item(f"{node}_{i}", i, command) for i in range(count))


def _node(
    name: str,
    upstream: Sequence[str] = (),
    count: int = 1,
    *,
    schedulable: Optional[bool] = True,
    items: Optional[Tuple[RawItem, ...]] = None,
    **kwargs: Any,
) -> NodeInfo:
    """A node that is a step unless told otherwise, with ``count`` items unless given its own."""
    return NodeInfo(
        name=name,
        is_dynamic=False,
        top_type="Processor",
        upstream=tuple(upstream),
        items=items if items is not None else _items(name, count),
        schedulable=schedulable,
        **kwargs,
    )


def _deps(snapshot: GraphSnapshot) -> Dict[str, Tuple[str, ...]]:
    """Each step's dependencies by step name, for comparing a whole graph at once."""
    return {step.name: step.dependencies for step in snapshot.steps}


# -- strategies ---------------------------------------------------------------


@st.composite
def node_dags(draw: st.DrawFn, max_nodes: int = 8) -> List[NodeInfo]:
    """Unique names, upstream drawn from earlier names, any schedulability, 0..3 items."""
    # Upstream only names earlier nodes, so the TOP network itself is acyclic.
    count: int = draw(st.integers(1, max_nodes))
    names: List[str] = [f"n{i}" for i in range(count)]
    nodes: List[NodeInfo] = []
    for i, name in enumerate(names):
        upstream: List[str] = draw(st.lists(st.sampled_from(names[:i]), unique=True)) if i else []
        schedulable: Optional[bool] = draw(st.sampled_from([True, False, None]))
        # Zero items and empty commands are drawn too, so steps without items and
        # nodes classified by their commands both occur.
        item_count: int = draw(st.integers(0, 3))
        command: str = draw(st.sampled_from(["", "run"]))
        items: Tuple[RawItem, ...] = tuple(
            _item(f"{name}_{k}", k, command, is_out_of_process=bool(command))
            for k in range(item_count)
        )
        nodes.append(_node(name, upstream, schedulable=schedulable, items=items))
    return nodes


def _paths_model(nodes: Sequence[NodeInfo]) -> Dict[str, Set[str]]:
    """Brute force: s depends on t iff some upstream path from s reaches t with only non-steps between."""
    by_name: Dict[str, NodeInfo] = {n.name: n for n in nodes}
    steps: Set[str] = {n.name for n in nodes if n.is_schedulable}
    result: Dict[str, Set[str]] = {}

    def paths(start: str, visited: Tuple[str, ...]) -> Iterator[Tuple[str, ...]]:
        """Every simple upstream path from ``start``, stopping at the first step on it."""
        for up in by_name[start].upstream:
            if up in visited or up not in by_name:
                continue
            yield visited + (up,)
            if up not in steps:
                yield from paths(up, visited + (up,))

    # A step's dependencies are the steps that end some path, never itself.
    for name in steps:
        result[name] = {path[-1] for path in paths(name, (name,)) if path[-1] in steps} - {name}
    return result


def _closure(deps: Mapping[str, Iterable[str]]) -> Set[Tuple[str, str]]:
    """Every ``(step, ancestor)`` pair, by adding implied edges until none is new."""
    edges: Set[Tuple[str, str]] = {(s, d) for s, ds in deps.items() for d in ds}
    while True:
        extra: Set[Tuple[str, str]] = {
            (a, c) for a, b in edges for b2, c in edges if b == b2
        } - edges
        if not extra:
            return edges
        edges |= extra


# -- classification -----------------------------------------------------------


@pytest.mark.parametrize(
    "schedulable, commands, expected",
    [
        (None, [], False),
        (None, ["", ""], False),
        (None, ["", "hython job.py"], True),
        (False, ["hython job.py"], False),
        (True, [], True),
    ],
)
def test_known_schedulability_wins_and_unknown_falls_back_to_commands(
    schedulable: Optional[bool], commands: List[str], expected: bool
) -> None:
    """What the node says about itself decides; only when it says nothing do the items' commands.

    Trusting the commands over a known answer would turn a wedge into a step, or
    drop a step that has not generated items yet.
    """
    items: Tuple[RawItem, ...] = tuple(
        _item(f"i{k}", k, command, is_out_of_process=bool(command))
        for k, command in enumerate(commands)
    )
    node: NodeInfo = _node("n", schedulable=schedulable, items=items)
    assert node.is_schedulable is expected


@given(st.lists(st.sampled_from([True, False, None]), min_size=1, max_size=4))
def test_an_items_cook_state_does_not_change_the_shape(states: List[Optional[bool]]) -> None:
    """Whether an item is in the cook leaves the shape alone.

    Dropping items the cook will not schedule is the scene reader's job; doing it
    here too would drop them twice and lose tasks.
    """
    unknown: Tuple[RawItem, ...] = tuple(_item(f"i{k}", k) for k in range(len(states)))
    known: Tuple[RawItem, ...] = tuple(
        _item(f"i{k}", k, in_cook=state) for k, state in enumerate(states)
    )
    assert synthesize([_node("n", items=known)]) == synthesize([_node("n", items=unknown)])


#: Type names a reader can report: unknown, two command servers, and two ordinary nodes.
_TYPE_NAMES: List[str] = ["", "houdiniserver", "commandserverbegin", "ropfetch", "genericgenerator"]


@given(node_dags(), st.data())
def test_a_nodes_type_name_does_not_change_the_shape(
    nodes: List[NodeInfo], data: st.DataObject
) -> None:
    """A node's Houdini type name leaves the shape alone, command servers included.

    Deciding what a command server means is validation's job. A shape that changed
    with the type name, for example by dropping a server node's step, would have
    validation refuse or drop the same steps a second time.
    """
    # Give every node a drawn type name and compare with the same graph without one.
    named: List[NodeInfo] = [
        replace(node, node_type_name=data.draw(st.sampled_from(_TYPE_NAMES))) for node in nodes
    ]
    assert synthesize(named) == synthesize(nodes)


def test_a_step_node_without_items_is_left_out_and_listed() -> None:
    """A step node that generated no items is not a step, but is listed, not skipped.

    A step with no tasks has no task range, so keeping it would build an invalid
    job; calling it skipped would hide it from validation.
    """
    # A step node with no items at all.
    snapshot: GraphSnapshot = synthesize([_node("a", count=0)])
    assert snapshot.steps == ()
    assert snapshot.steps_without_items == ("a",)
    assert snapshot.skipped_nodes == ()


def test_non_steps_become_skipped_nodes_and_contribute_no_tasks() -> None:
    """A wedge's items are not tasks: it maps to no step and adds nothing to the count.

    Counting them would reserve tasks that no worker ever runs.
    """
    # A wedge with three items feeding a cache with three.
    nodes: List[NodeInfo] = [
        _node("wedge", schedulable=False, items=_items("wedge", 3, command="")),
        _node("cache", ["wedge"], 3),
    ]
    snapshot: GraphSnapshot = synthesize(nodes)
    assert snapshot.step_names == ("cache",)
    assert snapshot.skipped_nodes == ("wedge",)
    assert snapshot.total_tasks == 3


# -- projection ---------------------------------------------------------------


def test_a_partitioner_collapses_into_the_edge_across_it() -> None:
    """A partitioner maps to no step, so the step after it depends on the step before it.

    Without that edge the render would start before the cache exists.
    """
    nodes: List[NodeInfo] = [
        _node("cache"),
        _node("partition", ["cache"], schedulable=False),
        _node("render", ["partition"]),
    ]
    assert _deps(synthesize(nodes)) == {"cache": (), "render": ("cache",)}


def test_chained_non_steps_collapse_into_one_edge() -> None:
    """Two non-steps in a row still collapse into one edge, so the walk does not stop after one hop."""
    nodes: List[NodeInfo] = [
        _node("a"),
        _node("part1", ["a"], schedulable=False),
        _node("part2", ["part1"], schedulable=False),
        _node("b", ["part2"]),
    ]
    assert _deps(synthesize(nodes))["b"] == ("a",)


def test_projection_stops_at_the_nearest_step() -> None:
    """A step depends on its nearest step ancestor, not on the steps beyond it.

    Walking past a step would add edges no TOP wire implies.
    """
    nodes: List[NodeInfo] = [_node("a"), _node("b", ["a"]), _node("c", ["b"])]
    assert _deps(synthesize(nodes))["c"] == ("b",)


def test_self_edges_cycles_and_dangling_names_terminate() -> None:
    """A self-edge, a cycle of non-steps, and a missing name end the walk with no dependency.

    A recursive or unvisited walk would hang or crash on any of them.
    """
    nodes: List[NodeInfo] = [
        _node("a", ["a", "missing", "p"]),
        _node("p", ["q"], schedulable=False),
        _node("q", ["p"], schedulable=False),
    ]
    snapshot: GraphSnapshot = synthesize(nodes)
    assert _deps(snapshot) == {"a": ()}


def test_a_cyclic_step_graph_still_produces_every_step() -> None:
    """Steps on a cycle are still emitted, sorted, after the acyclic ones, instead of hanging or vanishing."""
    snapshot: GraphSnapshot = synthesize([_node("b", ["a"]), _node("a", ["b"]), _node("c")])
    assert snapshot.step_names == ("c", "a", "b")


@settings(max_examples=150)
@given(node_dags())
def test_projection_matches_a_path_model(nodes: List[NodeInfo]) -> None:
    """For any graph, each step depends on exactly the steps a brute-force path search finds.

    The ones the search finds that were left out for having no items are recorded
    as dropped instead, so no edge is lost silently.
    """
    # Split the model's answer into the steps kept and the steps left out.
    model: Dict[str, Set[str]] = _paths_model(nodes)
    snapshot: GraphSnapshot = synthesize(nodes)
    steps_with_items: Set[str] = set(snapshot.step_names)
    for step in snapshot.steps:
        assert set(step.dependencies) == model[step.name] & steps_with_items
        assert set(snapshot.dependencies_on_dropped.get(step.name, ())) == (
            model[step.name] - steps_with_items
        )


@settings(max_examples=100)
@given(node_dags(), st.integers(1, 3), st.data())
def test_inserting_non_steps_on_an_edge_changes_nothing(
    nodes: List[NodeInfo], hops: int, data: st.DataObject
) -> None:
    """For any graph, putting a chain of non-steps on one wire changes no step.

    A projection that counted hops, or stopped at the first non-step, would fail.
    """
    # Pick one wire, from ``source`` into ``target``, to lengthen.
    edges: List[Tuple[str, str]] = [(n.name, up) for n in nodes for up in n.upstream]
    if not edges:
        return
    target, source = data.draw(st.sampled_from(edges))
    chain: List[str] = [f"x{k}" for k in range(hops)]
    # Point ``target`` at the end of the chain instead of at ``source``.
    rewired: List[NodeInfo] = []
    for node in nodes:
        if node.name != target:
            rewired.append(node)
            continue
        rewritten: Tuple[str, ...] = tuple(
            chain[-1] if up == source else up for up in node.upstream
        )
        rewired.append(_node(node.name, rewritten, schedulable=node.schedulable, items=node.items))
    # Then add the chain itself: x0 reads ``source``, and each next one the last.
    for k, name in enumerate(chain):
        rewired.append(
            _node(name, [source if k == 0 else chain[k - 1]], schedulable=False, items=())
        )
    before, after = synthesize(nodes), synthesize(rewired)
    assert before.steps == after.steps
    assert before.dependencies_on_dropped == after.dependencies_on_dropped


# -- data edges ---------------------------------------------------------------


def test_a_data_edge_adds_a_dependency_and_is_recorded_as_data() -> None:
    """A data edge with no TOP wire becomes a dependency, and both edges are marked as data.

    The render reads the pyro cache with no wire to it; without the edge it would
    start early, and without the mark its worker would not sync the cache.
    """
    # The wired edge (sparks) is also requested, so it is marked as data too.
    nodes: List[NodeInfo] = [_node("pyro"), _node("sparks"), _node("karma", ["sparks"])]
    snapshot: GraphSnapshot = synthesize(nodes, data_edges={"karma": {"pyro", "sparks"}})
    karma: Optional[StepSpec] = snapshot.step("karma")
    assert karma is not None
    assert karma.dependencies == ("pyro", "sparks")
    assert karma.data_dependencies == ("pyro", "sparks")


def test_a_data_edge_adds_only_itself_not_the_producers_ancestors() -> None:
    """A data edge to ``b`` adds ``b`` alone, not the steps ``b`` depends on.

    Adding the ancestors would make the consumer sync files it never reads.
    """
    # ``c`` reads ``b``'s outputs; ``b`` itself depends on ``a``.
    nodes: List[NodeInfo] = [_node("a"), _node("b", ["a"]), _node("c")]
    snapshot: GraphSnapshot = synthesize(nodes, data_edges={"c": {"b"}})
    c: Optional[StepSpec] = snapshot.step("c")
    assert c is not None
    assert c.dependencies == ("b",)
    assert c.data_dependencies == ("b",)


def test_a_data_edge_that_closes_a_cycle_is_refused() -> None:
    """A data edge against the wire would close a cycle, so it is refused and listed.

    Accepting it would make the job's dependencies cyclic, which the farm rejects.
    """
    # ``b`` is wired after ``a``, and ``a`` asks to read ``b``.
    nodes: List[NodeInfo] = [_node("a"), _node("b", ["a"])]
    snapshot: GraphSnapshot = synthesize(nodes, data_edges={"a": {"b"}})
    assert _deps(snapshot) == {"a": (), "b": ("a",)}
    assert snapshot.refused_data_edges == {"a": ("b",)}


def test_data_edges_naming_non_steps_or_themselves_are_ignored() -> None:
    """A data edge to itself, to a non-step, to a missing name, or from a non-step adds nothing.

    None of them names two steps, so none can be a dependency, and none is a refusal.
    """
    # ``w`` is a non-step and ``ghost`` names no node.
    nodes: List[NodeInfo] = [_node("a"), _node("w", schedulable=False)]
    snapshot: GraphSnapshot = synthesize(nodes, data_edges={"a": {"a", "w", "ghost"}, "w": {"a"}})
    assert _deps(snapshot) == {"a": ()}
    assert snapshot.refused_data_edges == {}


def test_a_redundant_edge_is_kept_because_it_carries_files() -> None:
    """A wired edge that a longer path implies is kept.

    A worker syncs files only from its step's direct dependencies, so pruning
    ``c -> a`` would lose ``a``'s outputs on ``c``'s workers.
    """
    # ``c`` is wired to both ``a`` and ``b``, and ``b`` already follows ``a``.
    nodes: List[NodeInfo] = [_node("a"), _node("b", ["a"]), _node("c", ["a", "b"])]
    snapshot: GraphSnapshot = synthesize(nodes)
    assert _deps(snapshot)["c"] == ("a", "b")
    assert all(step.data_dependencies == () for step in snapshot.steps)


@settings(max_examples=150)
@given(node_dags(), st.data())
def test_the_merge_stays_acyclic_and_accounts_for_every_request(
    nodes: List[NodeInfo], data: st.DataObject
) -> None:
    """For any graph and any data edges, the result is acyclic and every request is accounted for.

    Each valid request is either landed or refused, never both or neither, and the
    dependencies are exactly the wired ones plus the accepted requests. An empty
    mapping gives the same snapshot as no data edges at all.
    """
    # Requests may name any node, steps or not, including the consumer itself.
    names: List[str] = [n.name for n in nodes]
    requested: Dict[str, Set[str]] = data.draw(
        st.dictionaries(st.sampled_from(names), st.sets(st.sampled_from(names)), max_size=6)
    )
    plain: GraphSnapshot = synthesize(nodes)
    # No data edges and an empty mapping must mean the same thing, so a caller that
    # passes `{}` gets the same job as one that passes nothing.
    assert synthesize(nodes, data_edges={}) == plain
    snapshot: GraphSnapshot = synthesize(nodes, data_edges=requested)
    steps: Set[str] = {n.name for n in nodes if n.is_schedulable}
    steps_with_items: Set[str] = set(snapshot.step_names)

    # Count dependencies on dropped steps too: no cycle may hide behind one, and
    # merging data edges never removes a wired edge.
    deps: Dict[str, Set[str]] = {
        s.name: set(s.dependencies) | set(snapshot.dependencies_on_dropped.get(s.name, ()))
        for s in snapshot.steps
    }
    assert all((s, s) not in _closure(deps) for s in deps)
    for before in plain.steps:
        after: Optional[StepSpec] = snapshot.step(before.name)
        assert after is not None and set(before.dependencies) <= set(after.dependencies)

    # Every accepted edge must be either landed or refused, never both; one that
    # landed as a dependency is marked as data.
    for consumer, producers in requested.items():
        if consumer not in steps_with_items:
            continue
        step: Optional[StepSpec] = snapshot.step(consumer)
        assert step is not None
        refused: Set[str] = set(snapshot.refused_data_edges.get(consumer, ()))
        for producer in producers:
            # A request that names no other step is ignored, not refused.
            if producer == consumer or producer not in steps:
                assert producer not in refused
                continue
            landed: bool = (
                producer in step.dependencies
                or producer in snapshot.dependencies_on_dropped.get(consumer, ())
            )
            assert landed != (producer in refused)
            if producer in step.dependencies:
                assert producer in step.data_dependencies
    # Nothing beyond the wired edges and the accepted requests is added.
    model: Dict[str, Set[str]] = _paths_model(nodes)
    for spec in snapshot.steps:
        valid: Set[str] = {p for p in requested.get(spec.name, ()) if p != spec.name and p in steps}
        accepted: Set[str] = valid - set(snapshot.refused_data_edges.get(spec.name, ()))
        assert deps[spec.name] == model[spec.name] | accepted
        assert set(spec.data_dependencies) == accepted & set(spec.dependencies)


@settings(max_examples=100)
@given(node_dags(), st.data())
def test_a_refused_edge_would_close_a_cycle(nodes: List[NodeInfo], data: st.DataObject) -> None:
    """For any graph and data edges, a refused edge really would have closed a cycle.

    Refusing an edge that was safe would let a step start before the files it
    reads exist.
    """
    names: List[str] = [n.name for n in nodes]
    requested: Dict[str, Set[str]] = data.draw(
        st.dictionaries(st.sampled_from(names), st.sets(st.sampled_from(names)), max_size=6)
    )
    snapshot: GraphSnapshot = synthesize(nodes, data_edges=requested)
    # A dropped step's own dependencies are not in the snapshot, so a cycle through
    # one cannot be checked from it.
    assume(not snapshot.steps_without_items)
    deps: Dict[str, Set[str]] = {s.name: set(s.dependencies) for s in snapshot.steps}
    closure: Set[Tuple[str, str]] = _closure(deps)
    for consumer, producers in snapshot.refused_data_edges.items():
        for producer in producers:
            if consumer in deps and producer in deps:
                assert (producer, consumer) in closure


# -- dense indices ------------------------------------------------------------


def test_gapped_indices_become_dense_and_the_step_is_listed() -> None:
    """Items are ordered by PDG index and renumbered 0..N-1, keeping PDG's own index, and the step is listed.

    A task range of 0..N-1 over indices 3, 7, 9 would run tasks with no item.
    """
    items: Tuple[RawItem, ...] = (_item("b", 7), _item("a", 3), _item("c", 9))
    snapshot: GraphSnapshot = synthesize([_node("s", items=items)])
    step: Optional[StepSpec] = snapshot.step("s")
    assert step is not None
    # (name, task index, PDG index), in PDG index order rather than input order.
    assert [(i.name, i.task_index, i.pdg_index) for i in step.items] == [
        ("a", 0, 3),
        ("b", 1, 7),
        ("c", 2, 9),
    ]
    assert snapshot.remapped_steps == ("s",)


def test_dense_indices_are_not_listed_as_remapped() -> None:
    """A step whose indices are already 0..N-1 is not reported as remapped, so the report is not noise."""
    snapshot: GraphSnapshot = synthesize([_node("s", count=3)])
    assert snapshot.remapped_steps == ()


@given(
    st.lists(
        st.tuples(st.integers(0, 20), st.text("abc", min_size=1, max_size=3)),
        unique=True,
        min_size=1,
        max_size=12,
    )
)
def test_dense_indices_cover_the_range_and_round_trip(pairs: List[Tuple[int, str]]) -> None:
    """For any indices, with repeats, task indices are exactly 0..N-1 and each task finds its item.

    A lost or doubled item would leave a task with no work, or two tasks with one.
    """
    # Names make every item unique even when two share a PDG index.
    items: Tuple[RawItem, ...] = tuple(_item(f"{name}#{index}", index) for index, name in pairs)
    snapshot: GraphSnapshot = synthesize([_node("s", items=items)])
    step: Optional[StepSpec] = snapshot.step("s")
    assert step is not None
    assert [i.task_index for i in step.items] == list(range(len(items)))
    assert sorted(i.pdg_index for i in step.items) == sorted(index for index, _ in pairs)
    # Remapped exactly when PDG's sorted indices are not already 0..N-1.
    assert (snapshot.remapped_steps == ("s",)) == (
        sorted(index for index, _ in pairs) != list(range(len(items)))
    )
    for spec in step.items:
        assert snapshot.item(TaskKey("s", spec.task_index)) is snapshot.item_by_name(spec.name)


def test_unknown_lookups_return_none() -> None:
    """Unknown step names, out-of-range task indices, and unknown item names give None.

    The scheduler asks about items it did not reserve; an exception there would
    reach PDG, and a wrong item would run the wrong task.
    """
    # Task indices 2 and -1 are just outside a two-task step.
    snapshot: GraphSnapshot = synthesize([_node("s", count=2)])
    assert snapshot.step("nope") is None
    assert snapshot.item(TaskKey("nope", 0)) is None
    assert snapshot.item(TaskKey("s", 2)) is None
    assert snapshot.item(TaskKey("s", -1)) is None
    assert snapshot.item_by_name("nope") is None


def test_item_lookup_tolerates_a_hand_built_step_with_offset_indices() -> None:
    """The item lookup goes by each item's task index, not its position in the step.

    A positional read would return this item for task 0 and nothing for task 5.
    """
    # One item at position 0 whose task index is 5.
    spec: WorkItemSpec = WorkItemSpec(name="x", task_index=5, pdg_index=5, item_id=1, node_name="s")
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(StepSpec(name="s", dependencies=(), items=(spec,)),)
    )
    assert snapshot.item(TaskKey("s", 5)) is spec
    assert snapshot.item(TaskKey("s", 0)) is None


# -- shape --------------------------------------------------------------------


def test_steps_are_in_topological_order_with_alphabetical_ties() -> None:
    """Dependencies come first, and steps in the same layer are sorted by name.

    Any other tie-break would make the job's step order depend on PDG's node order.
    """
    nodes: List[NodeInfo] = [_node("z"), _node("b", ["z"]), _node("a", ["z"]), _node("m")]
    assert synthesize(nodes).step_names == ("m", "z", "a", "b")


def test_steps_are_ordered_one_layer_at_a_time() -> None:
    # Every step whose dependencies are all placed is emitted, sorted, before
    # any step that only becomes ready in a later layer.
    nodes: List[NodeInfo] = [_node("a", ["b"]), _node("b"), _node("c")]
    assert synthesize(nodes).step_names == ("b", "c", "a")


def test_a_step_left_out_still_counts_toward_the_layers() -> None:
    # "a" has no items, so it is left out of the job, but "abc" still waits a
    # layer for it, even though it keeps no dependency in the snapshot.
    nodes: List[NodeInfo] = [_node("a", count=0), _node("abc", ["a"]), _node("c")]
    snapshot: GraphSnapshot = synthesize(nodes)
    assert snapshot.step_names == ("c", "abc")
    assert _deps(snapshot) == {"abc": (), "c": ()}
    assert snapshot.dependencies_on_dropped == {"abc": ("a",)}


def test_a_dependency_on_a_step_without_items_is_dropped_and_recorded() -> None:
    """A dependency on a step left out for having no items is removed and recorded.

    Keeping it would name a step the job does not have; dropping it silently would
    hide that the consumer lost an upstream.
    """
    # ``b`` has no items; ``c`` depends on both ``b`` and ``a``.
    nodes: List[NodeInfo] = [_node("a"), _node("b", ["a"], count=0), _node("c", ["b", "a"])]
    snapshot: GraphSnapshot = synthesize(nodes)
    assert _deps(snapshot) == {"a": (), "c": ("a",)}
    assert snapshot.steps_without_items == ("b",)
    assert snapshot.dependencies_on_dropped == {"c": ("b",)}


def test_totals_and_leaves() -> None:
    """The task total sums every step, and the leaves are the steps nothing depends on.

    Getting either wrong would misreport the job's size or its final steps.
    """
    # ``a`` fans out to ``b`` and ``c``, so both are leaves.
    nodes: List[NodeInfo] = [
        _node("a", count=2),
        _node("b", ["a"], count=3),
        _node("c", ["a"], count=1),
    ]
    snapshot: GraphSnapshot = synthesize(nodes)
    assert snapshot.total_tasks == 6
    assert snapshot.leaf_steps() == ("b", "c")


def test_work_items_carry_their_declarations_and_command() -> None:
    """Each task keeps its item's declared files, command, node, and ID, and the step its exported attributes.

    These feed output reporting and the rewrite check; losing one would make a
    worker sync the wrong files or miss a changed command.
    """
    out: DeclaredFile = DeclaredFile("__PDG_DIR__/geo/a.bgeo.sc", "file/geo")
    inp: DeclaredFile = DeclaredFile("/src/in.bgeo.sc", "file/geo")
    items: Tuple[RawItem, ...] = (
        _item("a0", 0, declared_outputs=(out,), declared_inputs=(inp,)),
        _item("a1", 1),
    )
    snapshot: GraphSnapshot = synthesize(
        [_node("a", items=items, exported_attribute_names=("wedgeindex",))]
    )
    step: Optional[StepSpec] = snapshot.step("a")
    assert step is not None
    # The first item declares files and the second does not, so the step does not.
    first: WorkItemSpec = step.items[0]
    assert (first.declared_outputs, first.declared_inputs, first.command) == ((out,), (inp,), "run")
    assert first.node_name == "a" and first.item_id == 100
    assert first.declares_outputs and not step.items[1].declares_outputs
    assert not step.declares_outputs
    assert step.exported_attribute_names == ("wedgeindex",)


def test_a_step_declares_outputs_only_when_every_item_does() -> None:
    """A step declares its outputs when every one of its items does.

    Reporting a step's outputs needs no storage listing only in that case, so a
    step that always said no, or said yes for one declaring item, would be wrong.
    """
    # Two items that both declare an output, so the step must declare its outputs too.
    out: DeclaredFile = DeclaredFile("__PDG_DIR__/geo/a.bgeo.sc", "file/geo")
    items: Tuple[RawItem, ...] = (
        _item("a0", 0, declared_outputs=(out,)),
        _item("a1", 1, declared_outputs=(out,)),
    )
    step: Optional[StepSpec] = synthesize([_node("a", items=items)]).step("a")
    assert step is not None
    assert step.declares_outputs is True


def test_task_range_is_an_int_range() -> None:
    """A step of 2,000 tasks gets the range ``0-1999``.

    A list of indices would cap the step at 1,024 tasks.
    """
    # 2,000 items is past the 1,024-value limit a list of indices would hit.
    snapshot: GraphSnapshot = synthesize([_node("s", count=2_000)])
    step: Optional[StepSpec] = snapshot.step("s")
    assert step is not None
    assert step.task_count == 2_000
    assert step.task_range == "0-1999"


def test_an_empty_step_has_no_task_range() -> None:
    """A step with no items raises instead of giving the range ``0--1``, which the farm would reject."""
    with pytest.raises(ValueError):
        StepSpec(name="s", dependencies=(), items=()).task_range


@settings(max_examples=150)
@given(node_dags(), st.randoms())
def test_the_snapshot_partitions_names_and_ignores_input_order(
    nodes: List[NodeInfo], rnd: random.Random
) -> None:
    """For any graph, every node is in exactly one of steps, steps without items, or skipped.

    The steps are in dependency order, and shuffling the input gives the same
    snapshot, so the job does not change with PDG's node order.
    """
    # Each node is accounted for once.
    snapshot: GraphSnapshot = synthesize(nodes)
    steps: Set[str] = set(snapshot.step_names)
    dropped: Set[str] = set(snapshot.steps_without_items)
    skipped: Set[str] = set(snapshot.skipped_nodes)
    assert steps | dropped | skipped == {n.name for n in nodes}
    assert len(steps) + len(dropped) + len(skipped) == len(nodes)
    # Every dependency comes before its consumer.
    position: Dict[str, int] = {name: i for i, name in enumerate(snapshot.step_names)}
    for step in snapshot.steps:
        for dep in step.dependencies:
            assert dep in position and position[dep] < position[step.name]
    # The input order does not matter.
    shuffled: List[NodeInfo] = list(nodes)
    rnd.shuffle(shuffled)
    assert synthesize(shuffled) == snapshot


@settings(max_examples=100)
@given(node_dags(), st.data())
def test_less_generation_only_moves_steps_to_steps_without_items(
    nodes: List[NodeInfo], data: st.DataObject
) -> None:
    """For any graph, generating fewer items only moves steps into steps without items.

    The scheduler reads the graph before every item exists; this checks a partial
    read never turns a step into a skipped node or invents a new step.
    """
    # Empty the items of some nodes whose schedulability is known, so their
    # classification cannot change with their items.
    keep: Set[str] = data.draw(st.sets(st.sampled_from([n.name for n in nodes])))
    fewer: List[NodeInfo] = [
        (
            n
            if n.name in keep or n.schedulable is None
            else _node(n.name, n.upstream, schedulable=n.schedulable, items=())
        )
        for n in nodes
    ]
    # The same nodes are skipped, and every step that went missing was moved.
    full, partial = synthesize(nodes), synthesize(fewer)
    assert set(full.skipped_nodes) == set(partial.skipped_nodes)
    assert set(partial.step_names) <= set(full.step_names)
    moved: Set[str] = set(full.step_names) - set(partial.step_names)
    assert moved | set(full.steps_without_items) == set(partial.steps_without_items)


def test_pyro_bonfire_shape() -> None:
    """Pyro Bonfire's TOP network: 12 nodes, 6 steps, 2,481 tasks.

    The example scene's real shape: two wedges, partitioners, and a wait-for-all
    between steps, and a render that reads both caches through a data edge.
    """
    # The nodes as PDG reports them, in network order.
    nodes: List[NodeInfo] = [
        _node("wedge1", schedulable=False, items=_items("wedge1", 2, "")),
        _node("wedge2", ["wedge1"], schedulable=False, items=_items("wedge2", 6, "")),
        _node("ropfetch_pyro_cache", ["wedge2"], 720),
        _node("partitionbyattribute1", ["ropfetch_pyro_cache"], schedulable=False, items=()),
        _node("ropfetch_sparks_cache", ["partitionbyattribute1"], 720),
        _node("partitionbyattribute2", ["ropfetch_sparks_cache"], schedulable=False, items=()),
        _node("ropfetch_karma", ["partitionbyattribute2"], 480),
        _node("overlaytext", ["ropfetch_karma"], 480),
        _node("partitionbyframe1", ["overlaytext"], schedulable=False, items=()),
        _node("mosaic_bonfire", ["partitionbyframe1"], 80),
        _node("waitforall1", ["mosaic_bonfire"], schedulable=False, items=()),
        _node("bonfire_montage_video", ["waitforall1"], 1),
    ]
    # Karma reads the pyro cache with no wire to it, so it needs a data edge.
    snapshot: GraphSnapshot = synthesize(
        nodes, data_edges={"ropfetch_karma": {"ropfetch_pyro_cache", "ropfetch_sparks_cache"}}
    )
    assert len(snapshot.steps) == 6
    assert snapshot.total_tasks == 2_481
    karma: Optional[StepSpec] = snapshot.step("ropfetch_karma")
    assert karma is not None
    assert karma.dependencies == ("ropfetch_pyro_cache", "ropfetch_sparks_cache")
    assert snapshot.leaf_steps() == ("bonfire_montage_video",)
    assert len(snapshot.skipped_nodes) == 6


def test_the_lookups_survive_equality_and_copying() -> None:
    """Filled lookup indexes take no part in equality, and a deep copy still answers.

    If the cached indexes counted toward equality, a snapshot that had served one
    lookup would no longer equal the same shape freshly synthesized.
    """
    nodes: List[NodeInfo] = [_node("a", count=2)]
    used: GraphSnapshot = synthesize(nodes)
    # Fill every index before comparing.
    assert used.step("a") is not None
    assert used.item(TaskKey("a", 1)) is not None
    assert used.item_by_name("a_1") is not None
    assert used == synthesize(nodes)
    # The copy answers each lookup with an equal item.
    clone: GraphSnapshot = copy.deepcopy(used)
    assert clone == used
    assert clone.item(TaskKey("a", 1)) == used.item(TaskKey("a", 1))
    assert clone.item_by_name("a_1") == used.item_by_name("a_1")
