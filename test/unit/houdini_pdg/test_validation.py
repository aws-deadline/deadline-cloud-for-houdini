# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the validation module's public surface and its graph-shape checks.

The module is pure, so every test builds its input value types directly, and the
property tests check against independent models rather than a copy of the code.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from deadline.houdini_pdg import validation
from deadline.houdini_pdg.graph import GraphSnapshot, NodeInfo, RawItem, synthesize
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
