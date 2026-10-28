# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the validation module's public surface and its graph and node-kind checks.

The module is pure, so every test builds its input value types directly, and the
property tests check against independent models rather than a copy of the code.
"""

from __future__ import annotations

import os
from dataclasses import replace
from functools import reduce
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deadline.houdini_pdg import validation
from deadline.houdini_pdg.graph import (
    DeclaredFile,
    GraphSnapshot,
    NodeInfo,
    RawItem,
    synthesize,
)
from deadline.houdini_pdg.validation import (
    Advisory,
    Quotas,
    Refusal,
    RefusalScenario,
    ValidationContext,
    Verdict,
    validate,
    waived,
)

# The zero-upload property, a refused graph makes no attachment upload and no
# CreateJob call, is a property of the cook, which owns both calls, so it is
# tested with the cook rather than here.

WORKING_DIR: str = os.path.normpath("/root/wd")


# -- builders -----------------------------------------------------------------


def _item(name: str, index: int, command: str = "run", **kwargs: Any) -> RawItem:
    """A work item whose ``item_id`` is derived from its index, so tests can predict it."""
    return RawItem(name=name, index=index, item_id=index + 100, command=command, **kwargs)


def _node(
    name: str,
    upstream: Sequence[str] = (),
    count: int = 1,
    *,
    schedulable: Optional[bool] = True,
    items: Optional[Tuple[RawItem, ...]] = None,
    top_type: str = "Processor",
    **kwargs: Any,
) -> NodeInfo:
    """A node with ``count`` default items, unless ``items`` is given in full."""
    return NodeInfo(
        name=name,
        is_dynamic=kwargs.pop("is_dynamic", False),
        top_type=top_type,
        upstream=tuple(upstream),
        items=items if items is not None else tuple(_item(f"{name}_{i}", i) for i in range(count)),
        schedulable=schedulable,
        **kwargs,
    )


def _ctx(nodes: Sequence[NodeInfo] = (), **kwargs: Any) -> ValidationContext:
    """A context over ``nodes``, with the snapshot synthesized from them."""
    kwargs.setdefault("snapshot", synthesize(nodes))
    kwargs.setdefault("exists", lambda path: True)
    return ValidationContext(nodes=tuple(nodes), **kwargs)


def _subjects(verdict: Verdict, scenario: RefusalScenario) -> Set[str]:
    """The refusal subjects for one scenario, so a test can compare sets."""
    return {r.subject for r in verdict.refusals if r.scenario is scenario}


def _advised(verdict: Verdict, scenario: RefusalScenario) -> Set[str]:
    """The advisory subjects for one scenario, so a test can compare sets."""
    return {a.subject for a in verdict.advisories if a.scenario is scenario}


# -- strategies ---------------------------------------------------------------


@st.composite
def node_dags(draw: Any, max_nodes: int = 6, dynamic: bool = False) -> List[NodeInfo]:
    """Unique names, upstream drawn from earlier names, any schedulability, 0..3 items."""
    # Upstream names come from earlier indices only, so the graph is a DAG.
    count: int = draw(st.integers(1, max_nodes))
    names: List[str] = [f"n{i}" for i in range(count)]
    nodes: List[NodeInfo] = []
    for i, name in enumerate(names):
        upstream: List[str] = draw(st.lists(st.sampled_from(names[:i]), unique=True)) if i else []
        nodes.append(
            _node(
                name,
                upstream,
                schedulable=draw(st.sampled_from([True, False, None])),
                count=draw(st.integers(0, 3)),
                top_type=draw(st.sampled_from(["Processor", "Partitioner", "Mapper"])),
                is_dynamic=draw(st.booleans()) if dynamic else False,
                in_cook_set=draw(st.sampled_from([True, False, None])),
                cook_type=draw(
                    st.sampled_from([None, "Generate", "InProcess", "OutOfProcess", "Service"])
                ),
            )
        )
    return nodes


@st.composite
def step_dags(draw: Any, max_nodes: int = 6) -> List[NodeInfo]:
    """A DAG of nodes that all become steps, so every node is in the snapshot."""
    count: int = draw(st.integers(1, max_nodes))
    names: List[str] = [f"s{i}" for i in range(count)]
    return [
        _node(
            name,
            draw(st.lists(st.sampled_from(names[:i]), unique=True)) if i else [],
            count=draw(st.integers(1, 3)),
        )
        for i, name in enumerate(names)
    ]


quotas: Any = st.builds(
    Quotas,
    max_tasks_per_job=st.integers(0, 6),
    max_tasks_per_step=st.integers(0, 3),
    max_steps_per_job=st.integers(0, 4),
    max_step_dependencies=st.integers(0, 2),
    max_step_consumers=st.integers(0, 2),
)


# -- the verdict is total ------------------------------------------------------


@given(nodes=node_dags(dynamic=True))
def test_the_verdict_is_the_union_of_every_check(nodes: List[NodeInfo]) -> None:
    """validate returns exactly the union of the checks, with every field filled and accepted() in step."""
    ctx: ValidationContext = _ctx(nodes)
    verdict: Verdict = validate(ctx)

    # Compare the whole-run verdict against the concatenation of each check alone.
    # validate sorts the nodes once before running the checks, so bypass it with
    # the same sorted context rather than the raw one.
    sorted_ctx: ValidationContext = replace(
        ctx, nodes=tuple(sorted(ctx.nodes, key=lambda node: node.name))
    )
    from_each: List[Refusal] = [
        r for _scenario, check in validation._CHECKS for r in check(sorted_ctx).refusals
    ]
    assert sorted(verdict.refusals, key=repr) == sorted(from_each, key=repr)
    assert verdict.accepted() == (not verdict.refusals)
    assert all(r.subject and r.detail and r.remedy for r in verdict.refusals)
    assert all(a.subject and a.detail for a in verdict.advisories)


@given(nodes=node_dags(dynamic=True), seed=st.integers(0, 2**32))
@settings(max_examples=25)
def test_the_verdict_does_not_depend_on_the_order_of_the_checks(
    nodes: List[NodeInfo], seed: int
) -> None:
    """The set of refusals is the same whatever order the checks run in."""
    ctx: ValidationContext = _ctx(nodes)
    expected: Set[Refusal] = set(validate(ctx).refusals)

    # Rotate the registry by a drawn offset, so the same checks run in a new order.
    checks: List[Tuple[RefusalScenario, Any]] = list(validation._CHECKS)
    rotated: Tuple[Tuple[RefusalScenario, Any], ...] = tuple(
        checks[seed % len(checks) :] + checks[: seed % len(checks)]
    )
    with patch.object(validation, "_CHECKS", rotated):
        assert set(validate(ctx).refusals) == expected


def test_a_check_that_fails_leaves_an_advisory_and_the_others_run() -> None:
    """A check that raises becomes a named advisory, and the other checks still run."""

    def explode(ctx: ValidationContext) -> Verdict:
        """A stand-in check that always raises, to prove validate keeps going."""
        raise RuntimeError("no")

    nodes: List[NodeInfo] = [_node("dyn", is_dynamic=True)]
    # Put the exploding check first, so the dynamic check runs only if the loop
    # survived the exception.
    rotated: Tuple[Tuple[RefusalScenario, Any], ...] = (
        (RefusalScenario.SERVICE_NODE, explode),
    ) + validation._CHECKS
    with patch.object(validation, "_CHECKS", rotated):
        verdict: Verdict = validate(_ctx(nodes))

    # The dynamic refusal still lands, and the failed check leaves a named advisory.
    assert _subjects(verdict, RefusalScenario.DYNAMIC_NODE) == {"dyn"}
    (failed,) = [a for a in verdict.advisories if a.scenario is RefusalScenario.SERVICE_NODE]
    assert failed.subject == "service_node"
    assert "could not run" in failed.detail


def test_an_empty_graph_is_accepted() -> None:
    """No nodes means no work and no fault, so the verdict accepts."""
    assert validate(_ctx()).accepted()


# -- a cook with nothing for a worker to do -----------------------------------


def test_a_generated_graph_with_no_step_is_refused() -> None:
    """The job template needs at least one step, so a generated graph with none is refused here."""
    # Two in-process nodes with items but no schedulable node, so the snapshot has
    # a generated item and zero steps.
    nodes: List[NodeInfo] = [
        _node("genericgenerator1", schedulable=False, count=10),
        _node("inprocess_print", ["genericgenerator1"], schedulable=False, count=10),
    ]
    verdict: Verdict = validate(_ctx(nodes))
    assert _subjects(verdict, RefusalScenario.NO_FARM_WORK)
    (refusal,) = [r for r in verdict.refusals if r.scenario is RefusalScenario.NO_FARM_WORK]
    assert "no steps" in refusal.detail


def test_no_farm_work_names_only_the_in_cook_in_process_nodes_and_truncates_the_list() -> None:
    """The refusal counts and names only the in-session nodes it read, abbreviating a long list."""
    # Four in-session nodes the cook kept, plus one it excluded, and no farm work.
    nodes: List[NodeInfo] = [
        _node("ip_a", schedulable=False, count=1, in_cook_set=True),
        _node("ip_b", schedulable=False, count=1, in_cook_set=True),
        _node("ip_c", schedulable=False, count=1, in_cook_set=None),
        _node("ip_d", schedulable=False, count=1, in_cook_set=True),
        _node("ip_excluded", schedulable=False, count=1, in_cook_set=False),
    ]
    (refusal,) = [
        r for r in validate(_ctx(nodes)).refusals if r.scenario is RefusalScenario.NO_FARM_WORK
    ]
    # Four nodes are counted, the excluded one is absent, and the subject shows
    # three names followed by "and 1 more".
    assert "4 node(s) were read" in refusal.detail
    assert "ip_excluded" not in refusal.subject
    assert refusal.subject == "ip_a, ip_b, ip_c, and 1 more"


def test_a_graph_with_one_step_is_not_refused_for_having_no_farm_work() -> None:
    """One schedulable node is farm work, so the no-farm-work check stays silent."""
    nodes: List[NodeInfo] = [
        _node("generator", schedulable=False, count=4),
        _node("rop", ["generator"], count=4),
    ]
    assert not _subjects(validate(_ctx(nodes)), RefusalScenario.NO_FARM_WORK)


def test_no_farm_work_stands_down_before_generation() -> None:
    """Pre-generation no node has items, which is ordinary, so the check stands down rather than refuse."""
    nodes: List[NodeInfo] = [
        _node("generator", schedulable=False, items=()),
        _node("inprocess", ["generator"], schedulable=False, items=()),
    ]
    assert not _subjects(validate(_ctx(nodes)), RefusalScenario.NO_FARM_WORK)


def test_a_requested_empty_step_is_left_to_step_without_items_not_no_farm_work() -> None:
    """A worker step the cook asked for but that has no items yet is the snapshot race.

    The cook does have farm work, so it is STEP_WITHOUT_ITEMS, not NO_FARM_WORK.
    Both firing would give the artist two messages for one early read.
    """
    # An in-process generator that already produced items, plus a ROP Fetch the
    # cook asked for that has generated none yet.
    nodes: List[NodeInfo] = [
        _node("generator", schedulable=False, count=3),
        _node("rop_fetch", ["generator"], count=0, in_cook_set=True),
    ]
    verdict: Verdict = validate(_ctx(nodes))
    assert _subjects(verdict, RefusalScenario.STEP_WITHOUT_ITEMS) == {"rop_fetch"}
    assert not _subjects(verdict, RefusalScenario.NO_FARM_WORK)
    # The same stand-down when the caller could not place the empty step: a snapshot
    # whose only empty step names a node not in the context, so the lookup misses and
    # the unknown branch reads it as asked for, like the steps-without-items check.
    # The check must stand down cleanly: no refusal and no advisory from it, which a
    # read of the missing node would turn into (validate advises a check that raises).
    ghost: GraphSnapshot = GraphSnapshot(steps=(), steps_without_items=("ghost_step",))
    ghost_verdict: Verdict = validate(
        _ctx([_node("inprocess", schedulable=False, count=2)], snapshot=ghost)
    )
    assert not _subjects(ghost_verdict, RefusalScenario.NO_FARM_WORK)
    assert not _advised(ghost_verdict, RefusalScenario.NO_FARM_WORK)


# -- dynamic nodes -------------------------------------------------------------


@given(nodes=node_dags(dynamic=True))
def test_every_dynamic_node_is_refused_once_and_not_as_a_step_without_items(
    nodes: List[NodeInfo],
) -> None:
    """Dynamic subjects equal the dynamic nodes, once each, and never doubled with step-without-items."""
    verdict: Verdict = validate(_ctx(nodes))
    dynamic: Set[str] = {n.name for n in nodes if n.is_dynamic}

    # Exactly the dynamic nodes are refused, once each, and never doubled as a
    # step-without-items finding for the same node.
    assert _subjects(verdict, RefusalScenario.DYNAMIC_NODE) == dynamic
    assert len([r for r in verdict.refusals if r.scenario is RefusalScenario.DYNAMIC_NODE]) == len(
        dynamic
    )
    assert not dynamic & _subjects(verdict, RefusalScenario.STEP_WITHOUT_ITEMS)
    assert not dynamic & _advised(verdict, RefusalScenario.STEP_WITHOUT_ITEMS)


# -- ROP batching --------------------------------------------------------------


@st.composite
def batched_nodes(draw: Any) -> List[NodeInfo]:
    """Nodes whose items carry a drawn batch parent, so some become batched and some not."""
    nodes: List[NodeInfo] = []
    # Each node draws its item count and each item draws a batch parent, so the
    # strategy covers nodes with no batch, one batch, and several. is_batch is
    # drawn too, so a check that counts it instead of the parent fails.
    for i in range(draw(st.integers(1, 4))):
        name: str = f"b{i}"
        items: List[RawItem] = []
        for k in range(draw(st.integers(0, 3))):
            parent: str = draw(st.sampled_from(["", "p0", "p1"]))
            flag: bool = draw(st.booleans())
            items.append(_item(f"{name}_{k}", k, batch_parent=parent, is_batch=flag))
        nodes.append(
            _node(name, items=tuple(items), schedulable=draw(st.sampled_from([True, False])))
        )
    return nodes


@given(nodes=batched_nodes())
def test_a_batched_node_is_refused_with_its_item_and_batch_counts(
    nodes: List[NodeInfo],
) -> None:
    """Batch subjects are the schedulable nodes with a batched item, and the detail carries both counts."""
    verdict: Verdict = validate(_ctx(nodes))

    expected: Set[str] = {
        n.name for n in nodes if n.is_schedulable and any(i.batch_parent for i in n.items)
    }
    assert _subjects(verdict, RefusalScenario.ROP_BATCH) == expected

    by_name: Dict[str, NodeInfo] = {n.name: n for n in nodes}
    # Each batch refusal must report the batched-item count and batch count the
    # artist reads, recomputed here from the node rather than from the message.
    for refusal in verdict.refusals:
        if refusal.scenario is not RefusalScenario.ROP_BATCH:
            continue
        node: NodeInfo = by_name[refusal.subject]
        # Recount the batched items and batches from the node, independent of the
        # message, then check the detail and remedy carry them.
        children: int = sum(1 for i in node.items if i.batch_parent)
        batches: int = len({i.batch_parent for i in node.items if i.batch_parent})
        assert f"{children} of this node's work items are batched, in {batches} batch" in (
            refusal.detail
        )
        assert "batchall" in refusal.remedy and "framesperbatch" in refusal.remedy


def test_the_batch_remedy_asks_for_both_parms() -> None:
    """The remedy names both parms and asks for both, because either one alone still batches.

    Measured in Houdini 21.0.559 on a 720-item ROP Fetch across 6 wedges: batchall
    on gave 6 batches whatever framesperbatch held, and batchall off with
    framesperbatch at 10 gave 72. Only both together generated no batch at all.
    """
    nodes: List[NodeInfo] = [_node("rop", items=(_item("rop_0", 0, batch_parent="parent0"),))]
    # One batch child, so the node is refused, and the remedy must demand both
    # parms, not offer a choice.
    (refusal,) = [
        r for r in validate(_ctx(nodes)).refusals if r.scenario is RefusalScenario.ROP_BATCH
    ]
    assert refusal.subject == "rop"
    assert "batchall" in refusal.remedy and "framesperbatch" in refusal.remedy
    assert "and set" in refusal.remedy and "Both are needed" in refusal.remedy


# -- quotas --------------------------------------------------------------------

_LIMITS: Tuple[str, ...] = (
    validation._TASKS_PER_JOB,
    validation._STEPS_PER_JOB,
    validation._TASKS_PER_STEP,
    validation._DEPENDENCIES_PER_STEP,
    validation._CONSUMERS_PER_STEP,
)


def _quota_model(snapshot: GraphSnapshot, quota: Quotas) -> Dict[Tuple[str, str], Tuple[int, int]]:
    """Every exceeded (subject, quota) pair a reference count finds, with its count and limit.

    This is an independent oracle: it recounts the graph's shape rather than
    calling the check, so it can catch the check miscounting.
    """
    crossed: Dict[Tuple[str, str], Tuple[int, int]] = {}
    # Job-wide ceilings first: total tasks, then step count.
    if snapshot.total_tasks > quota.max_tasks_per_job:
        crossed[(validation._WHOLE_COOK, validation._TASKS_PER_JOB)] = (
            snapshot.total_tasks,
            quota.max_tasks_per_job,
        )
    if len(snapshot.steps) > quota.max_steps_per_job:
        crossed[(validation._WHOLE_COOK, validation._STEPS_PER_JOB)] = (
            len(snapshot.steps),
            quota.max_steps_per_job,
        )
    # Then per-step ceilings, counting consumers as we go.
    consumers: Dict[str, int] = {}
    for step in snapshot.steps:
        if step.task_count > quota.max_tasks_per_step:
            crossed[(step.name, validation._TASKS_PER_STEP)] = (
                step.task_count,
                quota.max_tasks_per_step,
            )
        if len(step.dependencies) > quota.max_step_dependencies:
            crossed[(step.name, validation._DEPENDENCIES_PER_STEP)] = (
                len(step.dependencies),
                quota.max_step_dependencies,
            )
        for dependency in step.dependencies:
            consumers[dependency] = consumers.get(dependency, 0) + 1
    for name, count in consumers.items():
        if count > quota.max_step_consumers:
            crossed[(name, validation._CONSUMERS_PER_STEP)] = (count, quota.max_step_consumers)
    return crossed


def _quota_refusals(verdict: Verdict) -> Set[Tuple[str, str]]:
    """The (subject, limit) pairs the verdict's quota refusals report."""
    return {
        (r.subject, limit)
        for r in verdict.refusals
        if r.scenario is RefusalScenario.QUOTA_EXCEEDED
        for limit in _LIMITS
        if limit in r.detail
    }


def _exact_quotas(snapshot: GraphSnapshot) -> Quotas:
    """Quotas set to this snapshot's exact counts, which must refuse nothing."""
    # Count consumers per step, then set each quota to the measured maximum, so no
    # count is strictly over its limit.
    consumers: Dict[str, int] = {}
    for step in snapshot.steps:
        for dependency in step.dependencies:
            consumers[dependency] = consumers.get(dependency, 0) + 1
    return Quotas(
        max_tasks_per_job=snapshot.total_tasks,
        max_tasks_per_step=max((s.task_count for s in snapshot.steps), default=0),
        max_steps_per_job=len(snapshot.steps),
        max_step_dependencies=max((len(s.dependencies) for s in snapshot.steps), default=0),
        max_step_consumers=max(consumers.values(), default=0),
    )


@given(nodes=step_dags(), quota=quotas)
def test_every_crossed_quota_is_refused_with_its_count_and_limit(
    nodes: List[NodeInfo], quota: Quotas
) -> None:
    """The quota refusals equal the reference count, and each names its count, limit, and remedy."""
    snapshot: GraphSnapshot = synthesize(nodes)
    verdict: Verdict = validate(_ctx(nodes, snapshot=snapshot, quotas=quota))
    expected: Dict[Tuple[str, str], Tuple[int, int]] = _quota_model(snapshot, quota)

    # The reported refusals match the independent count.
    assert _quota_refusals(verdict) == set(expected)
    # Each refusal states the count and the limit the reference count found, since
    # those are what the artist acts on, and carries the Service Quotas remedy.
    for refusal in verdict.refusals:
        if refusal.scenario is not RefusalScenario.QUOTA_EXCEEDED:
            continue
        # Find which quota this refusal names, then look up what the model counted.
        (quota_name,) = [name for name in _LIMITS if name in refusal.detail]
        count, limit = expected[(refusal.subject, quota_name)]
        assert f"{count:,} {quota_name}" in refusal.detail
        assert f"limit of {limit:,}." in refusal.detail
        assert "Service Quotas" in refusal.remedy


@given(nodes=step_dags(), quota=quotas, field=st.sampled_from(sorted(Quotas().__dict__)))
def test_raising_one_quota_can_only_remove_refusals(
    nodes: List[NodeInfo], quota: Quotas, field: str
) -> None:
    """Raising a single quota field by one is monotone: it never adds a refusal."""
    snapshot: GraphSnapshot = synthesize(nodes)
    # Count refusals, raise one field by one, and confirm the set only shrinks.
    before: Set[Tuple[str, str]] = _quota_refusals(
        validate(_ctx(nodes, snapshot=snapshot, quotas=quota))
    )
    raised: Quotas = replace(quota, **{field: getattr(quota, field) + 1})
    after: Set[Tuple[str, str]] = _quota_refusals(
        validate(_ctx(nodes, snapshot=snapshot, quotas=raised))
    )
    assert after <= before


@given(nodes=step_dags())
def test_quotas_set_to_the_exact_counts_refuse_nothing(nodes: List[NodeInfo]) -> None:
    """At the exact counts nothing is strictly over the limit, so no quota refuses."""
    snapshot: GraphSnapshot = synthesize(nodes)
    verdict: Verdict = validate(_ctx(nodes, snapshot=snapshot, quotas=_exact_quotas(snapshot)))
    assert not _quota_refusals(verdict)


def test_a_dependency_quota_refusal_says_how_many_edges_carry_data() -> None:
    """A dependency refusal reports how many of its edges carry data, since a data edge cannot be pruned."""
    nodes: List[NodeInfo] = [_node("a"), _node("b"), _node("c", ["a", "b"])]
    # One of c's two dependencies carries data, so the detail must say "1".
    snapshot: GraphSnapshot = synthesize(nodes, data_edges={"c": {"a"}})
    verdict: Verdict = validate(
        _ctx(nodes, snapshot=snapshot, quotas=Quotas(max_step_dependencies=1))
    )
    (refusal,) = [r for r in verdict.refusals if r.scenario is RefusalScenario.QUOTA_EXCEEDED]
    assert "1 of them carry data" in refusal.detail


# -- steps without items -------------------------------------------------------


@given(nodes=node_dags(dynamic=True))
def test_a_step_without_items_is_refused_when_it_was_asked_for_or_is_needed(
    nodes: List[NodeInfo],
) -> None:
    """A dropped step refuses iff the cook asked for it or a survivor needs it, leaving dynamic nodes out."""
    snapshot: GraphSnapshot = synthesize(nodes)
    verdict: Verdict = validate(_ctx(nodes, snapshot=snapshot))
    by_name: Dict[str, NodeInfo] = {n.name: n for n in nodes}
    dynamic: Set[str] = {n.name for n in nodes if n.is_dynamic}

    # A refusal is expected exactly when a dropped step that is not dynamic was
    # requested, or a surviving step depends on a dropped one that is not dynamic.
    # A dynamic node already has its own refusal, so it never adds this one, and a
    # dynamic survivor is never also refused here.
    requested: bool = any(
        name not in dynamic and by_name[name].in_cook_set is not False
        for name in snapshot.steps_without_items
    )
    orphaned: bool = any(
        name not in dynamic
        for step, missing in snapshot.dependencies_on_dropped.items()
        if step not in dynamic
        for name in missing
    )
    refused: bool = bool(_subjects(verdict, RefusalScenario.STEP_WITHOUT_ITEMS))
    assert refused == (requested or orphaned)


@given(nodes=node_dags())
def test_an_unknown_cook_set_makes_every_dropped_step_a_refusal(
    nodes: List[NodeInfo],
) -> None:
    """With cook_set unknown, every dropped step is refused, the strict reading."""
    # Force every node's cook-set flag to unknown, so no drop can be excused.
    unknown: List[NodeInfo] = [replace(n, in_cook_set=None) for n in nodes]
    snapshot: GraphSnapshot = synthesize(unknown)
    verdict: Verdict = validate(_ctx(unknown, snapshot=snapshot))
    assert _subjects(verdict, RefusalScenario.STEP_WITHOUT_ITEMS) >= set(
        snapshot.steps_without_items
    )


def test_a_step_left_out_of_the_cook_only_gets_an_advisory() -> None:
    """A dropped step the cook did not ask for is an advisory, not a refusal."""
    # b generated nothing and the cook did not ask for it, so it is advised, not refused.
    nodes: List[NodeInfo] = [_node("a"), _node("b", count=0, in_cook_set=False)]
    verdict: Verdict = validate(_ctx(nodes))
    assert not _subjects(verdict, RefusalScenario.STEP_WITHOUT_ITEMS)
    assert _advised(verdict, RefusalScenario.STEP_WITHOUT_ITEMS) == {"b"}


def test_a_surviving_step_that_needs_a_dropped_one_is_refused() -> None:
    """A survivor that depends on a dropped step is refused even if the drop was not asked for."""
    # a is dropped and not requested, but b depends on it, so b is still refused.
    nodes: List[NodeInfo] = [_node("a", count=0, in_cook_set=False), _node("b", ["a"])]
    verdict: Verdict = validate(_ctx(nodes))
    assert _subjects(verdict, RefusalScenario.STEP_WITHOUT_ITEMS) == {"b"}


def test_a_step_whose_only_dropped_producer_is_dynamic_is_not_refused_for_it() -> None:
    """A dynamic producer already has its own refusal, so its consumer gets no second one."""
    # dyn is dynamic and has no items, and b depends on it. The cause is dyn being
    # dynamic, so the artist gets that refusal only.
    nodes: List[NodeInfo] = [
        _node("dyn", count=0, is_dynamic=True, in_cook_set=True),
        _node("b", ["dyn"]),
    ]
    verdict: Verdict = validate(_ctx(nodes))
    assert _subjects(verdict, RefusalScenario.DYNAMIC_NODE) == {"dyn"}
    assert not _subjects(verdict, RefusalScenario.STEP_WITHOUT_ITEMS)


def test_a_step_with_a_dynamic_and_a_plain_dropped_producer_names_only_the_plain_one() -> None:
    """The consumer is still refused for the producer that is not dynamic, and names only that one."""
    # c depends on a dynamic producer and on one that was left out of the cook. The
    # second one still leaves c without input files.
    nodes: List[NodeInfo] = [
        _node("dyn", count=0, is_dynamic=True),
        _node("plain", count=0, in_cook_set=False),
        _node("c", ["dyn", "plain"]),
    ]
    verdict: Verdict = validate(_ctx(nodes))
    (refusal,) = [r for r in verdict.refusals if r.scenario is RefusalScenario.STEP_WITHOUT_ITEMS]
    assert refusal.subject == "c"
    assert "plain" in refusal.detail
    assert "dyn" not in refusal.detail


# -- the scenario keys ---------------------------------------------------------


def test_the_scenario_keys_are_stable_snake_case_identifiers() -> None:
    """Every scenario value is a unique lower snake-case key, and HOST_REQUIREMENTS is gone."""
    # Keys are unique snake_case identifiers, and the removed member is absent.
    values: List[str] = [s.value for s in RefusalScenario]
    assert len(set(values)) == len(values)
    assert all(v.islower() and v.replace("_", "").isalnum() for v in values)
    assert not hasattr(RefusalScenario, "HOST_REQUIREMENTS")


# -- validation off -----------------------------------------------------------


def test_waived_keeps_every_refusal_as_an_advisory_with_its_remedy() -> None:
    """waived drops each refusal to an advisory after the existing ones, folding the remedy into the detail."""
    kept: Advisory = Advisory(RefusalScenario.LOCAL_FILE_UNREACHABLE, "step", "big file.")
    # One pre-existing advisory and two refusals, so the test can check the order
    # and the folded-in remedy after waiving.
    verdict: Verdict = Verdict(
        refusals=(
            Refusal(RefusalScenario.IN_PROCESS_NODE, "merge2", "cooks here.", "Change it."),
            Refusal(RefusalScenario.QUOTA_EXCEEDED, "this cook", "too many.", "Ask."),
        ),
        advisories=(kept,),
    )
    result: Verdict = waived(verdict)
    # The verdict accepts, the original advisory stays first, each refusal becomes
    # an advisory, and the first one's detail carries its old detail and remedy.
    assert result.accepted()
    assert result.advisories[0] is kept
    assert [(a.scenario, a.subject) for a in result.advisories[1:]] == [
        (RefusalScenario.IN_PROCESS_NODE, "merge2"),
        (RefusalScenario.QUOTA_EXCEEDED, "this cook"),
    ]
    assert result.advisories[1].detail == (
        "Not refused, because graph validation is off. cooks here. Change it."
    )


# -- service and command-server nodes ------------------------------------------


@pytest.mark.parametrize(
    "node_type",
    [
        "houdiniserver",
        "mayaserver",
        "nukeserver",
        "shotgunserver",
        "commandserverbegin",
        "commandserverend",
    ],
)
@pytest.mark.parametrize("cook_type", ["OutOfProcess", "Service", None])
def test_a_command_server_is_refused_whatever_its_cook_type_says(
    node_type: str, cook_type: Optional[str]
) -> None:
    """Each command-server type is refused by its node type, whatever its cook type says."""
    nodes: List[NodeInfo] = [_node("server", node_type_name=node_type, cook_type=cook_type)]
    assert _subjects(validate(_ctx(nodes)), RefusalScenario.SERVICE_NODE) == {"server"}


def test_a_command_server_remedy_does_not_advise_out_of_process() -> None:
    """A command server's remedy names the hang, not the Out-of-Process cook type that causes it."""
    # A command server left on Out-of-Process is refused, and the remedy must name
    # the hang rather than offer the cook type that causes it.
    nodes: List[NodeInfo] = [_node("srv", node_type_name="houdiniserver", cook_type="OutOfProcess")]
    (refusal,) = [
        r for r in validate(_ctx(nodes)).refusals if r.scenario is RefusalScenario.SERVICE_NODE
    ]
    assert "command server" in refusal.detail
    assert "does not help" in refusal.remedy


def test_a_plain_service_node_is_refused_with_the_cook_type_remedy() -> None:
    """A plain Service node is refused and told to switch cook type, and an Out-of-Process one is not."""
    # One Service node and one Out-of-Process node: only the Service one is refused,
    # and its remedy offers the cook-type fix a command server must not get.
    nodes: List[NodeInfo] = [
        _node("py_service", cook_type="Service"),
        _node("ok", cook_type="OutOfProcess"),
    ]
    verdict: Verdict = validate(_ctx(nodes))
    assert _subjects(verdict, RefusalScenario.SERVICE_NODE) == {"py_service"}
    (refusal,) = [r for r in verdict.refusals if r.scenario is RefusalScenario.SERVICE_NODE]
    assert "Out Of Process" in refusal.remedy


def test_a_node_that_overrides_the_scheduler_is_refused() -> None:
    """A node whose topscheduler is set sends part of the cook to another job, so it is refused."""
    # Any override left here points at another scheduler, because the host clears a
    # self-naming override before validation.
    nodes: List[NodeInfo] = [_node("a", scheduler_override="/obj/topnet1/localscheduler")]
    verdict: Verdict = validate(_ctx(nodes))
    (refusal,) = [r for r in verdict.refusals if r.scenario is RefusalScenario.MULTIPLE_SCHEDULERS]
    assert refusal.subject == "a"
    assert "topscheduler" in refusal.detail and "topscheduler" in refusal.remedy


def test_an_unused_extra_scheduler_node_causes_no_refusal() -> None:
    """A scheduler node in the scene that no node overrides to is not itself a refusal."""
    # The scheduler is just another node with no override set, so the scheduler
    # check must leave the cook accepted.
    nodes: List[NodeInfo] = [
        _node("rop"),
        _node("localscheduler", schedulable=False, items=(), node_type_name="localscheduler"),
    ]
    verdict: Verdict = validate(_ctx(nodes))
    assert not _subjects(verdict, RefusalScenario.MULTIPLE_SCHEDULERS)


# -- in-process nodes -----------------------------------------------------------


def _in_process_script() -> List[NodeInfo]:
    """A file pattern, a step, a wait-for-all, an in-process Python Script, and a step."""
    return [
        _node("filepattern1", schedulable=False, items=()),
        _node("subdivide", ["filepattern1"], count=3),
        _node("waitforall1", ["subdivide"], schedulable=False, items=(), top_type="Partitioner"),
        _node("python1", ["waitforall1"], schedulable=False, items=(), cook_type="InProcess"),
        _node("distort", ["python1"], count=1),
    ]


def _routed_network() -> List[NodeInfo]:
    """Stock routing processors between two steps: a merge, a switch, an attribute create."""
    return [
        _node("subdivide", count=3),
        _node("merge", ["subdivide"], schedulable=False, items=(), cook_type="Generate"),
        _node("switch", ["merge"], schedulable=False, items=(), cook_type="Generate"),
        # An attribute set from the frame number touches no file.
        _node(
            "attribcreate",
            ["switch"],
            schedulable=False,
            items=(),
            cook_type=None,
            expressions=(("intvalue11", "@Frame+120"),),
        ),
        _node("distort", ["attribcreate"], count=1),
    ]


def _wedged_network() -> List[NodeInfo]:
    """Wedges at the roots and partitioners between the steps."""
    return [
        _node("wedge1", schedulable=False, items=()),
        _node("wedge2", schedulable=False, items=()),
        _node("sparks", ["wedge1", "wedge2"], count=2),
        _node("partition1", ["sparks"], schedulable=False, items=(), top_type="Partitioner"),
        _node("karma", ["partition1"], count=2),
        _node("mapper1", ["karma"], schedulable=False, items=(), top_type="Mapper"),
        _node("mosaic", ["mapper1"], count=1),
    ]


def _between_steps(**middle: Any) -> List[NodeInfo]:
    """A step that declares one output, a node ``middle`` that is not a step, and a step below it."""
    # The middle node has no items and the Generate cook type unless a test says otherwise.
    declared: Tuple[DeclaredFile, ...] = (DeclaredFile("__PDG_DIR__/geo/sub.1.bgeo.sc", ""),)
    middle.setdefault("items", ())
    middle.setdefault("cook_type", "Generate")
    return [
        _node("subdivide", items=(_item("subdivide_0", 0, declared_outputs=declared),)),
        _node("middle", ["subdivide"], schedulable=False, **middle),
        _node("distort", ["middle"]),
    ]


def _judged(nodes: List[NodeInfo]) -> Verdict:
    """The verdict on ``nodes`` with no read report and nothing on disk."""
    # Nothing is on disk, so the cached-output check stays out of these tests.
    return validate(_ctx(nodes, working_dir=WORKING_DIR, env={}, exists=lambda path: False))


def _in_process(verdict: Verdict) -> Refusal:
    """The one in-process refusal in ``verdict``."""
    (refusal,) = [r for r in verdict.refusals if r.scenario is RefusalScenario.IN_PROCESS_NODE]
    return refusal


def test_an_in_process_processor_between_two_steps_is_refused() -> None:
    """An In-Process node with a step above and below is the only refusal, naming each side."""
    # python1 is In-Process, below subdivide and above distort.
    verdict: Verdict = validate(_ctx(_in_process_script()))
    refusal: Refusal = _in_process(verdict)
    assert refusal.subject == "python1"
    assert "below subdivide" in refusal.detail and "above distort" in refusal.detail
    assert verdict.refusals == (refusal,)


def test_the_cooking_example_geometry_import_is_refused_from_its_own_parms() -> None:
    """The Geometry Import below a step is refused with no read report, by its authored file parm.

    Measured in Houdini 21: its ``filepath`` is authored as `` `@pdg_input` `` and
    evaluates to nothing outside a work item, so the scene walk drops it, and its
    cook type is Generate, which the reader reports as unknown.
    """
    # The scene's shape: a step, a wait-for-all, the Geometry Import, and a step.
    nodes: List[NodeInfo] = [
        _node("filepattern1", schedulable=False, items=()),
        _node("subdivide_ropfetch1", ["filepattern1"], count=3),
        _node(
            "waitforall1",
            ["subdivide_ropfetch1"],
            schedulable=False,
            items=(),
            top_type="Partitioner",
        ),
        _node(
            "merge",
            ["waitforall1"],
            schedulable=False,
            items=(),
            cook_type=None,
            node_type_name="geometryimport",
            expressions=(("filepath", "`@pdg_input`"),),
            file_parms=(("filepath", "`@pdg_input`"),),
        ),
        _node("distort_ropfetch1", ["merge"], count=1),
    ]
    verdict: Verdict = _judged(nodes)
    refusal: Refusal = _in_process(verdict)
    assert refusal.subject == "merge"
    assert "'filepath' parm reads an upstream work item's file" in refusal.detail
    assert verdict.refusals == (refusal,)


@pytest.mark.parametrize(
    "text", ["`@pdg_input`", '`pdginput(0, "", 0)`', "@pdg_output", '`pdgoutput(0, "", 0)`']
)
def test_a_file_parm_that_names_an_upstream_items_file_is_refused(text: str) -> None:
    """A file parm whose authored text names an upstream item's file reads a step's output."""
    # The parm is the node's only file use, so the token alone must refuse it.
    nodes: List[NodeInfo] = _between_steps(file_parms=(("filepath", text),))
    refusal: Refusal = _in_process(_judged(nodes))
    assert refusal.subject == "middle"
    assert "'filepath' parm reads an upstream work item's file" in refusal.detail


@pytest.mark.parametrize(
    "expression",
    [
        'npoints(pdginput(0, "", 0))',
        'detail(opinputpath(".", 0), "n", 0)',
        'point(pdginput(0, "", 0), 0, "P", 0)',
        'prim(opinputpath(".", 0), 0, "name", 0)',
    ],
)
def test_an_expression_that_reads_an_upstream_file_is_refused(expression: str) -> None:
    """An expression that opens an upstream file through any of the four functions is refused."""
    nodes: List[NodeInfo] = _between_steps(expressions=(("intvalue11", expression),))
    refusal: Refusal = _in_process(_judged(nodes))
    assert "'intvalue11' expression reads an upstream work item's file" in refusal.detail


def test_a_node_whose_own_parms_touch_no_step_file_is_allowed() -> None:
    """An outside file, a constant expression, or an item's path copied into a plain parm is allowed.

    Each is the near miss of a refused case. Copying `` `@pdg_input` `` into a
    parm that is not a file parm stores the path and opens no file.
    """
    # One node carries all three near misses, so any one of them refusing fails.
    nodes: List[NodeInfo] = _between_steps(
        file_parms=(("file", "/library/tex/wood.jpg"),),
        expressions=(("intvalue11", "@Frame+120"), ("stringvalue1", "`@pdg_input`")),
    )
    verdict: Verdict = _judged(nodes)
    assert not _subjects(verdict, RefusalScenario.IN_PROCESS_NODE)
    assert not _advised(verdict, RefusalScenario.IN_PROCESS_NODE)


@pytest.mark.parametrize(
    ("code", "advised"),
    [
        ("for up in upstream_items:\n    path = up.outputFiles[0].path\n", True),
        ("import os\nok = os.path.exists(path)\n", True),
        ("for up in upstream_items:\n    item_holder.addWorkItem(parent=up)\n", False),
    ],
)
def test_python_code_that_names_item_files_is_only_advised(code: str, advised: bool) -> None:
    """Python code that names item files or opens a file gets an advisory, never a refusal."""
    # The code is the node's only file use, so it is at most an advisory.
    nodes: List[NodeInfo] = _between_steps(python_code=(("generate", code),))
    verdict: Verdict = _judged(nodes)
    assert not _subjects(verdict, RefusalScenario.IN_PROCESS_NODE)
    assert _advised(verdict, RefusalScenario.IN_PROCESS_NODE) == ({"middle"} if advised else set())


def test_a_locked_assets_inner_node_is_not_judged() -> None:
    """A node inside a locked asset is neither refused nor advised, because the artist cannot change it."""
    # The node would be refused three ways and advised once if it were not locked.
    nodes: List[NodeInfo] = _between_steps(
        in_locked_asset=True,
        cook_type="InProcess",
        file_parms=(("filepath", "`@pdg_input`"),),
        expressions=(("intvalue11", 'npoints(pdginput(0, "", 0))'),),
        python_code=(("generate", "open(path)"),),
    )
    verdict: Verdict = _judged(nodes)
    assert not _subjects(verdict, RefusalScenario.IN_PROCESS_NODE)
    assert not _advised(verdict, RefusalScenario.IN_PROCESS_NODE)


def test_routing_processors_between_two_steps_are_allowed() -> None:
    """A Merge, Switch and Attribute Create with a constant touch no file, so none is refused."""
    verdict: Verdict = validate(_ctx(_routed_network()))
    assert verdict.accepted()


def test_service_nodes_between_two_steps_are_not_refused_as_in_process() -> None:
    """A Service node and a command server are reported once, under the service scenario."""
    # The command server is also In-Process, so the in-process check could fire on it too.
    nodes: List[NodeInfo] = [
        _node("subdivide", count=2),
        _node("serve", ["subdivide"], schedulable=False, items=(), cook_type="Service"),
        _node(
            "server",
            ["serve"],
            schedulable=False,
            items=(),
            cook_type="InProcess",
            node_type_name="commandserverbegin",
        ),
        _node("distort", ["server"], count=1),
    ]
    verdict: Verdict = validate(_ctx(nodes))
    assert not _subjects(verdict, RefusalScenario.IN_PROCESS_NODE)
    assert _subjects(verdict, RefusalScenario.SERVICE_NODE) == {"serve", "server"}


def test_root_wedges_and_partitioners_between_steps_are_allowed() -> None:
    """Root wedges run before the job and partitioners move no files, so none is refused."""
    verdict: Verdict = validate(_ctx(_wedged_network()))
    assert verdict.accepted()


def test_a_processor_above_every_step_is_allowed() -> None:
    """An in-process node with no step above it runs before the job, so it is allowed."""
    nodes: List[NodeInfo] = [
        _node("filepattern1", schedulable=False, items=(), cook_type="InProcess"),
        _node("rop", ["filepattern1"]),
    ]
    assert not _subjects(validate(_ctx(nodes)), RefusalScenario.IN_PROCESS_NODE)


def test_an_upstream_name_with_no_node_does_not_stop_the_walk() -> None:
    """A dangling upstream name must not stop the ancestor walk from finding the step above."""
    nodes: List[NodeInfo] = [
        _node("subdivide", count=2),
        _node(
            "python1", ["ghost", "subdivide"], schedulable=False, items=(), cook_type="InProcess"
        ),
        _node("distort", ["python1"]),
    ]
    assert _subjects(validate(_ctx(nodes)), RefusalScenario.IN_PROCESS_NODE) == {"python1"}


@given(nodes=node_dags())
def test_an_in_process_node_is_refused_exactly_when_a_step_is_above_it(
    nodes: List[NodeInfo],
) -> None:
    """With no file reads, the subjects are exactly the In-Process non-steps with a step ancestor."""
    by_name: Dict[str, NodeInfo] = {n.name: n for n in nodes}

    def ancestors(name: str) -> Set[str]:
        """Every name reachable upstream of ``name``, grown one wire per round.

        A path in the graph has fewer wires than there are nodes, so that many
        rounds reach every ancestor.
        """
        found: Set[str] = set(by_name[name].upstream)
        for _ in nodes:
            found |= {u for a in list(found) if a in by_name for u in by_name[a].upstream}
        return found

    # The model: an In-Process node that is not a step and has any step upstream.
    expected: Set[str] = {
        n.name
        for n in nodes
        if not n.is_schedulable
        and n.cook_type == "InProcess"
        and any(a in by_name and by_name[a].is_schedulable for a in ancestors(n.name))
    }
    assert _subjects(validate(_ctx(nodes)), RefusalScenario.IN_PROCESS_NODE) == expected


# -- the path helpers ----------------------------------------------------------

# Path prefixes that exercise every branch of the deliverability model: a PDG
# token, a path inside the working directory, one outside it, and three variable
# forms whose value depends on the environment.
_PREFIXES: List[str] = [
    "__PDG_DIR__",
    WORKING_DIR,
    os.path.normpath("/elsewhere"),
    "$VAR",
    "${VAR}",
    "$GONE",
]
_ENVS: List[Dict[str, str]] = [
    {"VAR": WORKING_DIR},
    {"VAR": os.path.normpath("/elsewhere")},
    {"VAR": "$OTHER"},
]


def _reason_model(path: str, working_dir: str, env: Mapping[str, str]) -> str:
    """An independent model of _undeliverable_reason's four branches, re-derived from path and env."""
    # Expand each variable once, the same single pass Houdini and the helper do.
    expanded: str = reduce(
        lambda text, pair: text.replace("${" + pair[0] + "}", pair[1]).replace(
            "$" + pair[0], pair[1]
        ),
        env.items(),
        path,
    )
    resolved: str = os.path.normpath(expanded)
    if "__PDG_" in path:
        return ""
    if "$" in resolved:
        return "unexpanded variable"
    if not working_dir:
        return ""
    root: str = os.path.normpath(working_dir)
    if resolved == root or resolved.startswith(root + os.sep):
        return ""
    return "outside the working directory"


@given(
    prefix=st.sampled_from(_PREFIXES),
    tail=st.sampled_from(["/geo/a.bgeo.sc", "/images/img.0001.exr"]),
    working_dir=st.sampled_from([WORKING_DIR, ""]),
    env=st.sampled_from(_ENVS),
)
def test_the_undeliverable_reason_follows_its_four_branches(
    prefix: str, tail: str, working_dir: str, env: Dict[str, str]
) -> None:
    """_undeliverable_reason matches the model on every branch, and a leading PDG token always wins."""
    path: str = prefix + tail
    assert validation._undeliverable_reason(path, working_dir, env) == _reason_model(
        path, working_dir, env
    )
    # A PDG token in front makes the path session-relative, so the reason is always
    # empty whatever the rest of the path said.
    assert validation._undeliverable_reason("__PDG_DIR__" + path, working_dir, env) == ""


def test_the_working_directory_itself_is_inside_the_working_directory() -> None:
    """A path equal to the working directory is deliverable, so the equal case is pinned."""
    assert validation._undeliverable_reason(WORKING_DIR, WORKING_DIR, {}) == ""


def test_is_below_is_false_for_an_empty_path_or_ancestor() -> None:
    """An empty path or ancestor is never below anything, so the guard refuses both."""
    assert validation._is_below("", WORKING_DIR) is False
    assert validation._is_below(WORKING_DIR, "") is False
    assert validation._is_below("", "") is False


def test_is_below_requires_a_separator_boundary() -> None:
    """A sibling whose name merely starts with the ancestor's is not below it; a true child is."""
    # A sibling sharing a prefix is not below, but the ancestor itself and a true
    # child are, so the test needs a separator boundary, not a bare prefix.
    parent: str = os.path.normpath("/a/b")
    assert validation._is_below(os.path.normpath("/a/bc"), parent) is False
    assert validation._is_below(parent, parent) is True
    assert validation._is_below(os.path.normpath("/a/b/c"), parent) is True


# -- outputs already on disk ----------------------------------------------------


def _with_outputs(nodes: Sequence[NodeInfo], prefix: str) -> List[NodeInfo]:
    """Every item declares one output, named after the item, under ``prefix``."""
    return [
        replace(
            node,
            items=tuple(
                replace(
                    item,
                    declared_outputs=(
                        DeclaredFile(f"{prefix}/{node.name}/{item.name}.bgeo.sc", ""),
                    ),
                )
                for item in node.items
            ),
        )
        for node in nodes
    ]


def _all_outputs(snapshot: GraphSnapshot) -> List[str]:
    """Every declared output path, resolved against the working directory."""
    return sorted(
        os.path.normpath(f.path.replace("__PDG_DIR__", WORKING_DIR))
        for step in snapshot.steps
        for item in step.items
        for f in item.declared_outputs
    )


@given(nodes=step_dags(max_nodes=4), data=st.data())
def test_more_files_on_disk_can_only_add_cached_output_refusals(
    nodes: List[NodeInfo], data: Any
) -> None:
    """Growing the on-disk set only adds refusals, and a full set refuses exactly the files' owners."""
    # Give every item one declared output, then test subsets of those paths as the
    # on-disk set.
    with_outputs: List[NodeInfo] = _with_outputs(nodes, "__PDG_DIR__/geo")
    snapshot: GraphSnapshot = synthesize(with_outputs)
    paths: List[str] = _all_outputs(snapshot)
    smaller: Set[str] = data.draw(st.sets(st.sampled_from(paths))) if paths else set()
    larger: Set[str] = smaller | (data.draw(st.sets(st.sampled_from(paths))) if paths else set())

    def refused(on_disk: Set[str]) -> Set[str]:
        """The cached-output subjects when exactly ``on_disk`` is present."""
        verdict: Verdict = validate(
            _ctx(
                with_outputs,
                snapshot=snapshot,
                working_dir=WORKING_DIR,
                env={},
                exists=lambda path: path in on_disk,
            )
        )
        return _subjects(verdict, RefusalScenario.CACHED_OUTPUTS)

    assert refused(set()) == set()
    assert refused(smaller) <= refused(larger)
    owners: Set[str] = {
        step.name
        for step in snapshot.steps
        for item in step.items
        for f in item.declared_outputs
        if os.path.normpath(f.path.replace("__PDG_DIR__", WORKING_DIR)) in larger
    }
    assert refused(larger) == owners


def test_one_cached_item_of_three_refuses_its_step() -> None:
    """A single cached item refuses the step, and the detail carries the count and example path."""
    # One of three items has its output on disk, so the step is refused and the
    # detail carries the "1 of 3" count and that example path.
    nodes: List[NodeInfo] = _with_outputs([_node("cache", count=3)], "__PDG_DIR__/geo")
    present: str = os.path.join(WORKING_DIR, "geo", "cache", "cache_1.bgeo.sc")
    verdict: Verdict = validate(
        _ctx(nodes, working_dir=WORKING_DIR, env={}, exists=lambda path: path == present)
    )
    (refusal,) = [r for r in verdict.refusals if r.scenario is RefusalScenario.CACHED_OUTPUTS]
    assert refusal.subject == "cache"
    assert "1 of this step's 3 work item(s)" in refusal.detail and present in refusal.detail
    assert "Write Files" in refusal.remedy and "Delete" in refusal.remedy


@pytest.mark.parametrize("path", ["__PDG_TEMP__/a.bgeo.sc", "$UNSET/a.bgeo.sc"])
def test_an_output_that_cannot_be_resolved_here_is_not_tested(path: str) -> None:
    """A path that still holds a token or a variable is neither tested on disk nor refused."""
    seen: List[str] = []

    def record(candidate: str) -> bool:
        """An exists that records every path it is asked about, to prove none was tested."""
        seen.append(candidate)
        return True

    nodes: List[NodeInfo] = [
        _node("a", items=(_item("a_0", 0, declared_outputs=(DeclaredFile(path, ""),)),))
    ]
    verdict: Verdict = validate(_ctx(nodes, working_dir=WORKING_DIR, env={}, exists=record))
    assert not _subjects(verdict, RefusalScenario.CACHED_OUTPUTS)
    assert not seen


def test_a_cached_output_check_that_cannot_test_a_path_refuses_nothing() -> None:
    """An ``exists`` that raises is not evidence a result is cached, so nothing refuses."""

    def explode(path: str) -> bool:
        """An exists that always raises, to prove the check swallows the error."""
        raise OSError("name too long")

    nodes: List[NodeInfo] = _with_outputs([_node("a")], "__PDG_DIR__/geo")
    verdict: Verdict = validate(_ctx(nodes, working_dir=WORKING_DIR, env={}, exists=explode))
    assert not _subjects(verdict, RefusalScenario.CACHED_OUTPUTS)


def test_a_node_set_to_write_files_is_not_refused_for_existing_outputs() -> None:
    """Write Files ignores existing results, so a node set to it is never cached."""
    nodes: List[NodeInfo] = _with_outputs(
        [_node("cache", count=3, cache_mode="Write Files")], "__PDG_DIR__/geo"
    )
    verdict: Verdict = validate(
        _ctx(nodes, working_dir=WORKING_DIR, env={}, exists=lambda path: True)
    )
    assert not _subjects(verdict, RefusalScenario.CACHED_OUTPUTS)


def test_cached_output_returns_an_empty_string_when_nothing_is_on_disk() -> None:
    """The helper returns "", the value the caller treats as "not cached", when no output exists."""
    # The caller keeps a found path only when it is truthy, so the empty value must
    # be the empty string, not another falsy value.
    item: Any = _item("a_0", 0, declared_outputs=(DeclaredFile("__PDG_DIR__/geo/a.bgeo.sc", ""),))
    ctx: ValidationContext = _ctx(
        [_node("a")], working_dir=WORKING_DIR, env={}, exists=lambda path: False
    )
    assert validation._cached_output(item, ctx) == ""
