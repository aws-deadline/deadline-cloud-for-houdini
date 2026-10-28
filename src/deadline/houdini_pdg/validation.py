# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Decide whether a graph can be cooked, before anything is uploaded.

A cook that fails after ``CreateJob`` has already cost the artist an upload, so
:func:`validate` names every unsupported scenario at once.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .graph import GraphSnapshot, NodeInfo
from .scene_reads import ReadReport

_logger: logging.Logger = logging.getLogger(__name__)


class RefusalScenario(Enum):
    """A scenario the scheduler refuses. The value is a stable key for logs and telemetry."""

    DYNAMIC_NODE = "dynamic_node"
    #: An in-process node that writes files a step reads, or reads files a step
    #: writes. Neither reaches the other side without a shared filesystem.
    IN_PROCESS_NODE = "in_process_node"
    RUNTIME_FILE_UNPRODUCED = "runtime_file_unproduced"
    ROP_BATCH = "rop_batch"
    MULTIPLE_SCHEDULERS = "multiple_schedulers"
    SERVICE_NODE = "service_node"
    QUOTA_EXCEEDED = "quota_exceeded"
    OUTPUT_OUTSIDE_COOK = "output_outside_cook"
    LOCAL_FILE_UNREACHABLE = "local_file_unreachable"
    ATTRIBUTE_AS_VARIABLE = "attribute_as_variable"
    #: A step PDG should have generated has no items yet, so the graph was read
    #: too early.
    STEP_WITHOUT_ITEMS = "step_without_items"
    #: The ``.hip`` was never saved, or has unsaved changes. Workers load the saved
    #: file, so they would cook a different scene than the one the job describes.
    UNSAVED_SCENE = "unsaved_scene"
    #: A step item whose expected outputs already exist here. PDG marks it cooked
    #: from cache and never schedules it, so its task never runs and the job waits.
    CACHED_OUTPUTS = "cached_outputs"
    #: Nothing in the cook runs on a worker, so the job would describe no steps.
    NO_FARM_WORK = "no_farm_work"


@dataclass(frozen=True)
class Refusal:
    """One reason the graph cannot be cooked, and what the artist should do."""

    scenario: RefusalScenario
    #: The node, step, parm, or file at fault.
    subject: str
    detail: str
    remedy: str


@dataclass(frozen=True)
class Advisory:
    """Something the artist should know that does not stop the cook."""

    scenario: RefusalScenario
    subject: str
    detail: str


@dataclass(frozen=True)
class Quotas:
    """The job-structure quotas a cook must fit. The defaults are the service's; a farm may have raised them."""

    max_tasks_per_job: int = 10_000
    max_tasks_per_step: int = 10_000
    max_steps_per_job: int = 200
    max_step_dependencies: int = 128
    max_step_consumers: int = 32


@dataclass(frozen=True)
class ValidationContext:
    """Everything validation reads. ``exists`` is its only access to the disk."""

    nodes: Tuple[NodeInfo, ...]
    snapshot: GraphSnapshot
    #: None when the scene walk did not run. The path checks then advise that
    #: they were skipped, instead of passing silently. A walk that reported
    #: itself unavailable is passed as None for the same reason.
    reads: Optional[ReadReport] = None
    working_dir: str = ""
    env: Optional[Mapping[str, str]] = None
    quotas: Quotas = Quotas()
    exists: Callable[[str], bool] = os.path.exists
    #: The schedulers the cook's nodes use, as node paths, the same form as
    #: :attr:`NodeInfo.scheduler_override`.
    scheduler_names: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    """Every refusal and advisory for one graph."""

    refusals: Tuple[Refusal, ...] = ()
    advisories: Tuple[Advisory, ...] = ()

    def accepted(self) -> bool:
        """Whether the graph has no refusals."""
        return not self.refusals


def validate(ctx: ValidationContext) -> Verdict:
    """Run every check and return all their findings. Never raises."""
    # Sort the nodes once by name, so a verdict never depends on the order PDG
    # listed them and no check has to sort again.
    ctx_with_ordered_nodes: ValidationContext = dataclasses.replace(
        ctx, nodes=tuple(sorted(ctx.nodes, key=lambda node: node.name))
    )
    refusals: List[Refusal] = []
    advisories: List[Advisory] = []
    for scenario, check in _CHECKS:
        try:
            verdict: Verdict = check(ctx_with_ordered_nodes)
        except Exception:
            # A check that fails becomes an advisory that names it. The cook can
            # still go ahead, and the other checks still run.
            _logger.warning("The '%s' check failed.", scenario.value, exc_info=True)
            advisories.append(
                Advisory(
                    scenario=scenario,
                    subject=scenario.value,
                    detail=(
                        "This check could not run, so the graph was not examined for it. "
                        "The reason is in the Houdini console."
                    ),
                )
            )
            continue
        refusals.extend(verdict.refusals)
        advisories.extend(verdict.advisories)
    return Verdict(refusals=tuple(refusals), advisories=tuple(advisories))


def waived(verdict: Verdict) -> Verdict:
    """Keep every refusal as an advisory, for a cook the artist chose not to validate.

    The remedy moves into the detail, so the warning still says what to change.
    """
    return Verdict(
        advisories=verdict.advisories
        + tuple(
            Advisory(
                scenario=refusal.scenario,
                subject=refusal.subject,
                detail=(
                    f"Not refused, because graph validation is off. {refusal.detail} "
                    f"{refusal.remedy}"
                ),
            )
            for refusal in verdict.refusals
        )
    )


# -- shared helpers -----------------------------------------------------------


def _examples(values: Sequence[str], limit: int = 3) -> str:
    """Up to ``limit`` values, then how many were left out, so one refusal stays readable."""
    shown: str = ", ".join(values[:limit])
    if len(values) > limit:
        shown += f", and {len(values) - limit} more"
    return shown


# -- the graph's shape --------------------------------------------------------


def _check_dynamic_nodes(ctx: ValidationContext) -> Verdict:
    """A node that generates its items while the cook runs has no tasks to describe."""
    return Verdict(
        refusals=tuple(
            Refusal(
                scenario=RefusalScenario.DYNAMIC_NODE,
                subject=node.name,
                detail=(
                    "This node generates its work items while the cook runs, so the job "
                    "cannot describe its tasks before it is submitted."
                ),
                remedy=(
                    "Replace it with a node that generates its items up front, or cook this "
                    "network with another scheduler."
                ),
            )
            for node in ctx.nodes
            if node.is_dynamic
        )
    )


def _check_rop_batch(ctx: ValidationContext) -> Verdict:
    """Batched items dispatch through a batch parent that is in no step.

    The parent never appears in the item list, so no task covers it and every
    parent fails in ``onSchedule``. Until the scheduler maps a parent to a task,
    the only way to cook such a node here is to stop batching.
    """
    refusals: List[Refusal] = []
    for node in ctx.nodes:
        # Only a schedulable node becomes a step, so only its items can be batched
        # into a parent that no task covers.
        if not node.is_schedulable:
            continue
        # Count batched items by the parent they name. The items are batch
        # children, and PDG's isBatch is False on every one of them.
        batched: List = [item for item in node.items if item.batch_parent]
        if not batched:
            continue
        batches: int = len({item.batch_parent for item in batched})
        refusals.append(
            Refusal(
                scenario=RefusalScenario.ROP_BATCH,
                subject=node.name,
                detail=(
                    f"{len(batched)} of this node's work items are batched, in {batches} "
                    "batch(es). PDG dispatches one batch parent per batch, and a parent is "
                    "in no step, so no task exists for it. This scheduler cannot batch yet."
                ),
                remedy=(
                    "Turn off 'All Frames in One Batch' (batchall) on this node and set "
                    "'Frames Per Batch' (framesperbatch) to 1. Both are needed, because "
                    "batchall ignores framesperbatch, and framesperbatch batches on its own. "
                    f"That trades {batches} task(s) for {len(batched)}, each paying its own "
                    "scene load, so prefer another scheduler when the per-item work is short."
                ),
            )
        )
    return Verdict(refusals=tuple(refusals))


#: Each quota's name, as the refusal says it. The count and the limit are what an
#: artist acts on, so both are in every detail.
_TASKS_PER_JOB: str = "tasks per job"
_STEPS_PER_JOB: str = "steps per job"
_TASKS_PER_STEP: str = "tasks per step"
_DEPENDENCIES_PER_STEP: str = "dependencies per step"
_CONSUMERS_PER_STEP: str = "consumers per step"

_QUOTA_REMEDY: str = (
    "Reduce the cook's scope, or request a quota increase in AWS Service Quotas for "
    "AWS Deadline Cloud."
)

#: What a job-wide quota refusal names, since no single node is at fault.
_WHOLE_COOK: str = "this cook"


def _quota_refusal(subject: str, count: int, limit: int, quota: str, extra: str = "") -> Refusal:
    """One quota refusal, with the count and the limit the artist acts on."""
    return Refusal(
        scenario=RefusalScenario.QUOTA_EXCEEDED,
        subject=subject,
        detail=f"{count:,} {quota}, more than the limit of {limit:,}.{extra}",
        remedy=_QUOTA_REMEDY,
    )


def _check_quotas(ctx: ValidationContext) -> Verdict:
    """Refuse every quota the job's shape crosses, naming the count and the limit.

    The service validates the same ceilings, but only after the scene is uploaded,
    and names neither the node nor a number.
    """
    quotas: Quotas = ctx.quotas
    steps: Tuple = ctx.snapshot.steps
    refusals: List[Refusal] = []

    # Job-wide ceilings: the task total across every step, and the step count.
    total: int = ctx.snapshot.total_tasks
    if total > quotas.max_tasks_per_job:
        refusals.append(
            _quota_refusal(_WHOLE_COOK, total, quotas.max_tasks_per_job, _TASKS_PER_JOB)
        )
    if len(steps) > quotas.max_steps_per_job:
        refusals.append(
            _quota_refusal(_WHOLE_COOK, len(steps), quotas.max_steps_per_job, _STEPS_PER_JOB)
        )

    # Per-step ceilings: a step's own task count, and its dependency count.
    for step in steps:
        if step.task_count > quotas.max_tasks_per_step:
            refusals.append(
                _quota_refusal(
                    step.name, step.task_count, quotas.max_tasks_per_step, _TASKS_PER_STEP
                )
            )
        if len(step.dependencies) > quotas.max_step_dependencies:
            # How many carry data decides whether the graph is untidy or
            # inexpressible at step granularity: an ordering edge can be pruned,
            # while dropping a data edge removes files from a session.
            carrying: int = len(step.data_dependencies)
            refusals.append(
                _quota_refusal(
                    step.name,
                    len(step.dependencies),
                    quotas.max_step_dependencies,
                    _DEPENDENCIES_PER_STEP,
                    extra=(
                        f" {carrying} of them carry data a task reads, so they cannot be " "pruned."
                        if carrying
                        else ""
                    ),
                )
            )

    # The consumer ceiling counts, for each step, how many other steps depend on
    # it, by inverting the dependency edges each step lists upstream.
    consumers: Counter = Counter(dependency for step in steps for dependency in step.dependencies)
    for name, count in sorted(consumers.items()):
        if count > quotas.max_step_consumers:
            refusals.append(
                _quota_refusal(name, count, quotas.max_step_consumers, _CONSUMERS_PER_STEP)
            )
    return Verdict(refusals=tuple(refusals))


def _check_no_farm_work(ctx: ValidationContext) -> Verdict:
    """Refuse a cook that generated items but no step for a worker to run.

    The job template requires at least one step, so a snapshot with none is
    rejected by the service with a schema error after the upload. The empty graph
    is not this case: with no items generated, no cook reaches the check, so it
    stands down until something has generated.
    """
    if ctx.snapshot.steps or not any(node.items for node in ctx.nodes):
        return Verdict()
    # A step the cook asked for but that has no items yet is the snapshot race,
    # which STEP_WITHOUT_ITEMS already names. Stand down so one early read does not
    # also read as "no farm work": the cook does have a worker step, it just has
    # not generated. Unknown placement (None) counts as asked for, like that check.
    by_name: Dict[str, NodeInfo] = {node.name: node for node in ctx.nodes}
    requested_empty_step: bool = any(
        by_name.get(name) is None or by_name[name].in_cook_set is not False
        for name in ctx.snapshot.steps_without_items
    )
    if requested_empty_step:
        return Verdict()
    # The nodes that were read but cook in this session, named so the artist can
    # see what the cook contained. A node that the caller could not place in the
    # cook set (None) is included, because excluding it would under-report.
    in_process: List[str] = sorted(
        node.name for node in ctx.nodes if not node.is_schedulable and node.in_cook_set is not False
    )
    return Verdict(
        refusals=(
            Refusal(
                scenario=RefusalScenario.NO_FARM_WORK,
                subject=_examples(in_process) if in_process else "this cook",
                detail=(
                    "No node in this cook runs a command on a worker, so the job would "
                    f"describe no steps and the service would reject it. {len(in_process)} "
                    "node(s) were read and every one of them cooks in this Houdini session."
                ),
                remedy=(
                    "Cook a node that runs on a worker, such as a ROP Fetch, or a Python "
                    "Script TOP set to Cook (Out-of-Process). A network of only in-process "
                    "nodes has nothing to send to a farm, so cook it with another scheduler."
                ),
            ),
        )
    )


def _check_steps_without_items(ctx: ValidationContext) -> Verdict:
    """Tell the snapshot race from a deliberate partial cook.

    A step that generated nothing is left out either because PDG had not generated
    it yet or because the artist cooked a different node. The cook set answers the
    first; a surviving step with a missing producer is the backstop when the cook
    set is incomplete.
    """
    by_name: Dict[str, NodeInfo] = {node.name: node for node in ctx.nodes}
    dynamic: Set[str] = {node.name for node in ctx.nodes if node.is_dynamic}
    dropped: List[str] = [name for name in ctx.snapshot.steps_without_items if name not in dynamic]

    def requested(name: str) -> bool:
        # A dropped node is a race only when the cook asked for it. Unknown means
        # the caller could not tell, which keeps the strict reading and refuses.
        node: Optional[NodeInfo] = by_name.get(name)
        return node is None or node.in_cook_set is not False

    # A dropped step the cook asked for is the snapshot race: PDG was still
    # generating, so the job would be missing most of its tasks.
    refusals: List[Refusal] = [
        Refusal(
            scenario=RefusalScenario.STEP_WITHOUT_ITEMS,
            subject=name,
            detail=(
                "This node runs on the farm and has generated no work items yet, and PDG "
                "asked for it in this cook. The graph was read while PDG was still "
                "generating, so the job would be missing most of its tasks."
            ),
            remedy="Press Generate before Cook, or cook again.",
        )
        for name in dropped
        if requested(name)
    ]
    # A surviving step that depends on a dropped one has no producer for its input
    # files, so it is refused even when the dropped node itself was not requested.
    for step in ctx.snapshot.steps:
        # A dynamic dependency is already refused for being dynamic, so only the
        # other missing producers are named here. A dynamic step is skipped for
        # the same reason: one message about one node is enough.
        missing: Tuple[str, ...] = tuple(
            name
            for name in ctx.snapshot.dependencies_on_dropped.get(step.name, ())
            if name not in dynamic
        )
        if not missing or step.name in dynamic:
            continue
        refusals.append(
            Refusal(
                scenario=RefusalScenario.STEP_WITHOUT_ITEMS,
                subject=step.name,
                detail=(
                    f"This step depends on {_examples(list(missing))}, which has no work "
                    "items in this cook, so nothing in this job produces its input files."
                ),
                remedy=(
                    "Cook the upstream nodes as well, dirty them so they cook again, or "
                    "cook again after generating."
                ),
            )
        )

    # Every other dropped step is a deliberate omission, so it is at most an
    # advisory, and never doubled with a refusal for the same node.
    refused: Set[str] = {refusal.subject for refusal in refusals}
    advisories: Tuple[Advisory, ...] = tuple(
        Advisory(
            scenario=RefusalScenario.STEP_WITHOUT_ITEMS,
            subject=name,
            detail=(
                "This node runs on the farm but has no work items in this cook, and PDG "
                "did not ask for it, so the job leaves it out."
            ),
        )
        for name in dropped
        if name not in refused
    )
    return Verdict(refusals=tuple(refusals), advisories=advisories)


# Every check, with the scenario it reports. A new refusal is a new function and a
# new row: no other check and no part of ``validate`` changes.
_CHECKS: Tuple[Tuple[RefusalScenario, Callable[[ValidationContext], Verdict]], ...] = (
    (RefusalScenario.DYNAMIC_NODE, _check_dynamic_nodes),
    (RefusalScenario.NO_FARM_WORK, _check_no_farm_work),
    (RefusalScenario.ROP_BATCH, _check_rop_batch),
    (RefusalScenario.QUOTA_EXCEEDED, _check_quotas),
    (RefusalScenario.STEP_WITHOUT_ITEMS, _check_steps_without_items),
)
