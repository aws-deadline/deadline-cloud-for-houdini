# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Decide whether a graph can be cooked, before anything is uploaded.

A cook that fails after ``CreateJob`` has already cost the artist an upload, so
:func:`validate` names every unsupported scenario at once.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from types import ModuleType
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .graph import PDG_DIR_TOKEN, PDG_TOKEN_PREFIX, GraphSnapshot, NodeInfo, WorkItemSpec
from .scene_reads import (
    AttributeVariableUse,
    ReadReport,
    ReadVerdict,
    SceneRead,
    _depends_on_work_item,
)

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
            _logger.warning(
                "The '%s' check, %s, failed.", scenario.value, check.__name__, exc_info=True
            )
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
        # An advisory two checks share, such as the skipped scene walk, is shown once.
        for advisory in verdict.advisories:
            if advisory not in advisories:
                advisories.append(advisory)
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


def _parm_subject(step: str, node_path: str, parm: str) -> str:
    """How a parm finding names its subject: the node path and parm, or the step if the node is unknown."""
    return f"{node_path or step} parm '{parm}'"


#: What every path check returns when the scene walk did not run. It is one shared
#: advisory, so ``validate`` shows it once rather than once per check.
_SCENE_WALK_SKIPPED: Advisory = Advisory(
    scenario=RefusalScenario.OUTPUT_OUTSIDE_COOK,
    subject="the scene's file references",
    detail=(
        "The scene's file references could not be read, so nothing checked where nodes "
        "read and write files, or how write parms spell work item attributes. A fault of "
        "that kind would not be reported before the cook."
    ),
)


def _skipped() -> Verdict:
    """Advise that a path check could not run, instead of returning what reads as a clean result."""
    return Verdict(advisories=(_SCENE_WALK_SKIPPED,))


# -- paths --------------------------------------------------------------------

#: ``$NAME`` or ``${NAME}``.
_VARIABLE: "re.Pattern[str]" = re.compile(r"\$\{(\w+)\}|\$(\w+)")

_OUTSIDE_WORKING_DIR: str = "outside the working directory"
_UNEXPANDED_VARIABLE: str = "unexpanded variable"

# How the artist's message describes each reason, so it reads after "are".
_UNDELIVERABLE_PHRASES: Dict[str, str] = {
    _OUTSIDE_WORKING_DIR: _OUTSIDE_WORKING_DIR,
    _UNEXPANDED_VARIABLE: "left with an unexpanded variable",
}

# The remedy for each reason an output cannot be delivered. An unexpanded
# variable needs a different fix than a path outside the delivered tree.
_UNDELIVERABLE_REMEDIES: Dict[str, str] = {
    _OUTSIDE_WORKING_DIR: (
        "Point the working directory at the tree these nodes write to, which is usually "
        "$HIP, or change the nodes to write under it."
    ),
    _UNEXPANDED_VARIABLE: (
        "Give the variable a real path instead of another variable, or use $HIP."
    ),
}

# Variables that follow the session or that a worker sets up, such as $HIP and
# $HFS, so a read under one of them resolves on a worker too.
_SESSION_VARIABLES: Tuple[str, ...] = ("HIP", "PDG_DIR", "PDG_TEMP")
_WORKER_VARIABLES: Tuple[str, ...] = ("HFS", "HH", "HHP", "HOUDINI_TEMP_DIR", "TEMP", "TMP")

# Matches a value beginning with one of those variables as a whole token, or with a
# PDG token. ``$HIP`` and ``${HIP}`` count; ``$HIPNAME`` does not.
_REACHABLE_PREFIX: "re.Pattern[str]" = re.compile(
    r"|".join(
        [rf"\$(?:{name})(?!\w)|\$\{{{name}\}}" for name in _SESSION_VARIABLES + _WORKER_VARIABLES]
        + [re.escape(PDG_TOKEN_PREFIX)]
    )
)


def _expand(path: str, env: Optional[Mapping[str, str]]) -> str:
    """One pass of ``$NAME`` and ``${NAME}`` substitution against ``env``, like Houdini."""
    # One pass only: Houdini does not re-expand a value that is itself a variable,
    # and an unknown name is left as it was, so the surviving ``$`` reaches the
    # checks that report it.
    source: Mapping[str, str] = os.environ if env is None else env

    def replace(match: "re.Match[str]") -> str:
        name: str = match.group(1) or match.group(2)
        return source.get(name, match.group(0))

    return _VARIABLE.sub(replace, path)


def _resolve(path: str, working_dir: str, env: Optional[Mapping[str, str]]) -> str:
    """A declared or authored path as it reads on this workstation."""
    # Substitute the PDG directory token for the session's working directory, then
    # Houdini variables, then normalise.
    return os.path.normpath(_expand(path.replace(PDG_DIR_TOKEN, working_dir), env))


def _is_below(path: str, ancestor: str, paths: ModuleType = os.path) -> bool:
    """Whether ``path`` is ``ancestor`` or under it. ``/a/bc`` is not under ``/a/b``.

    ``paths`` is ``os.path``, or ``ntpath`` in a test of Windows rules.
    """
    if not path or not ancestor:
        return False
    # Fold case where the platform ignores it, so C:\Work is under c:\work on Windows.
    child: str = paths.normcase(path)
    parent: str = paths.normcase(ancestor)
    if child == parent:
        return True
    # Compare against the ancestor plus a separator, so a sibling whose name only
    # starts with the ancestor's is not counted as under it.
    return child.startswith(parent.rstrip(paths.sep) + paths.sep)


def _undeliverable_reason(
    path: str, working_dir: str, env: Optional[Mapping[str, str]] = None
) -> str:
    """Why this declared output cannot reach a dependent step, or ``""``."""
    # A PDG token makes the path session-relative, and an absolute path inside the
    # working directory is in the attachment tree, so both are deliverable. A
    # surviving ``$`` or a path outside the working directory is not.
    if PDG_TOKEN_PREFIX in path:
        return ""
    resolved: str = os.path.normpath(_expand(path, env))
    if "$" in resolved:
        return _UNEXPANDED_VARIABLE
    if not working_dir:
        # Nothing to compare against, so refusing on a guess is worse than skipping.
        return ""
    if _is_below(resolved, os.path.normpath(working_dir)):
        return ""
    return _OUTSIDE_WORKING_DIR


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


# -- the node's kind ----------------------------------------------------------


def _nearest_steps(start: str, edges: Mapping[str, Set[str]], targets: Set[str]) -> List[str]:
    """Traverse from ``start``, following ``edges``, and return the closest ``targets`` on each path."""
    # Iterative with a visited set, so a cycle or a dangling name terminates. A
    # target is a boundary: it is recorded and not walked past.
    found: Set[str] = set()
    seen: Set[str] = {start}
    stack: List[str] = sorted(edges.get(start, set()))
    while stack:
        # Walk one node at a time, skipping seen names, recording a target without
        # walking past it, and following every other node's edges.
        current: str = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in targets:
            found.add(current)
            continue
        stack.extend(sorted(edges.get(current, set())))
    return sorted(found)


#: The ``cook_type`` values this module acts on. A node reports ``None`` when PDG
#: has not resolved its cook type.
_IN_PROCESS: str = "InProcess"
_SERVICE: str = "Service"

#: Authored text that names an upstream work item's file.
_ITEM_FILE_TOKENS: Tuple[str, ...] = ("@pdg_input", "pdginput(", "@pdg_output", "pdgoutput(")

#: An expression that opens an upstream file: ``pdginput`` or ``opinputpath`` as
#: the file argument of ``detail``, ``point``, ``prim`` or ``npoints``.
_UPSTREAM_FILE_EXPRESSION: "re.Pattern[str]" = re.compile(
    r"\b(?:detail|point|prim|npoints)\s*\(\s*(?:pdginput|opinputpath)\s*\("
)

#: Python code that names a work item's files or opens a file.
_FILE_CODE: "re.Pattern[str]" = re.compile(
    r"\b(?:inputFiles|outputFiles|expectedInputFiles|expectedOutputFiles|inputResultData"
    r"|resultData|pdginput|pdgoutput|loadFromFile|glob)\b|\bopen\s*\(|\bos\.path\b"
)


def _node_file_uses(node: NodeInfo) -> List[str]:
    """Each way ``node`` touches a step's files, in words.

    These are judged from the node alone: its cook type, its file parms' authored
    text, and its expressions.
    """
    uses: List[str] = []
    if node.cook_type == _IN_PROCESS:
        uses.append("its cook type is In-Process, so it can write files during the cook")
    # A file parm whose authored text names an upstream item's file reads a step's
    # output, even though it evaluates to nothing outside a work item.
    uses.extend(
        f"its '{parm}' parm reads an upstream work item's file"
        for parm, text in node.file_parms
        if any(token in text for token in _ITEM_FILE_TOKENS)
    )
    uses.extend(
        f"its '{parm}' expression reads an upstream work item's file"
        for parm, text in node.expressions
        if _UPSTREAM_FILE_EXPRESSION.search(text)
    )
    return uses


def _check_non_step_file_use(ctx: ValidationContext) -> Verdict:
    """Refuse a node that is not a step, has a step above it, and touches a step's files.

    Such a node runs on this machine, not on a worker, so a file it reads from a
    step is missing or old, and a file it writes never reaches a step. A node that
    only routes work items is allowed, and a locked asset's inner nodes are not judged.
    """
    steps: Set[str] = {node.name for node in ctx.nodes if node.is_schedulable}
    # Build the upstream and downstream edge maps, so a node's step ancestors and
    # step descendants can both be reached from its name.
    upstream: Dict[str, Set[str]] = {node.name: set(node.upstream) for node in ctx.nodes}
    downstream: Dict[str, Set[str]] = {node.name: set() for node in ctx.nodes}
    for node in ctx.nodes:
        for parent in node.upstream:
            if parent in downstream:
                downstream[parent].add(node.name)

    refusals: List[Refusal] = []
    advisories: List[Advisory] = []
    for node in ctx.nodes:
        # A step and a service node are judged by other checks, and the artist
        # cannot change a locked asset's inner nodes.
        if node.is_schedulable or node.in_locked_asset:
            continue
        if node.cook_type == _SERVICE or _is_command_server(node):
            continue
        above: List[str] = _nearest_steps(node.name, upstream, steps)
        # With no step above it, the node runs before the job exists, so it is allowed.
        if not above:
            continue
        below: List[str] = _nearest_steps(node.name, downstream, steps)
        between: str = f"below {_examples(above)}"
        if below:
            between += f" and above {_examples(below)}"
        uses: List[str] = _node_file_uses(node)
        if uses:
            refusals.append(
                Refusal(
                    scenario=RefusalScenario.IN_PROCESS_NODE,
                    subject=node.name,
                    detail=(
                        f"This node is not a step and runs on this machine, {between}. "
                        f"It touches a step's files: {'; '.join(uses)}. A file it reads "
                        "from a step is missing or old here, and a file it writes never "
                        "reaches a step on the farm."
                    ),
                    remedy=(
                        "Move the file work into a node that runs on a worker, such as a "
                        "ROP Fetch or a Python Script set to Out-of-Process, or cook this "
                        "network with another scheduler."
                    ),
                )
            )
            continue
        # Python code is too open-ended to judge, so code that names item files or
        # opens a file is only an advisory.
        coded: List[str] = [parm for parm, code in node.python_code if _FILE_CODE.search(code)]
        if coded:
            advisories.append(
                Advisory(
                    scenario=RefusalScenario.IN_PROCESS_NODE,
                    subject=node.name,
                    detail=(
                        f"This node runs on this machine, {between}, and its Python code in "
                        f"{_examples(coded)} names work item files or opens a file. If it "
                        "reads a file a step writes, it reads a missing or old file."
                    ),
                )
            )
    return Verdict(refusals=tuple(refusals), advisories=tuple(advisories))


#: TOP node types that start a long-running command server. Their cook type is
#: whatever the scene author set, so the node type, not the cook type, recognises
#: them: measured in Houdini 21.0.559, mayaserver and nukeserver ship as Service
#: while houdiniserver and shotgunserver ship as Out-of-Process.
_COMMAND_SERVERS: "frozenset[str]" = frozenset(
    {
        "houdiniserver",
        "mayaserver",
        "nukeserver",
        "shotgunserver",
        "commandserverbegin",
        "commandserverend",
    }
)


def _is_command_server(node: NodeInfo) -> bool:
    """Whether the node is a command-server TOP type, by its node type name."""
    return node.node_type_name in _COMMAND_SERVERS


def _check_service_nodes(ctx: ValidationContext) -> Verdict:
    """Refuse a Service node, and a command server whatever its cook type says.

    A command server left on Out-of-Process still becomes a step whose task holds
    a worker until it times out, so the node type is a second signal and its
    remedy never offers that cook type.
    """
    refusals: List[Refusal] = []
    for node in ctx.nodes:
        server: bool = _is_command_server(node)
        # A node is a service node by either signal: its cook type is Service, or
        # its type is a command server whatever the cook type says.
        if not server and node.cook_type != _SERVICE:
            continue
        # A command server and a plain service node take different remedies, and a
        # command server must never be told to switch to Out-of-Process, because
        # that still launches a server nothing connects to.
        detail: str = (
            (
                "This node starts a long-running command server that later work items "
                "connect to. Each task runs in its own session on its own worker, so "
                "nothing can reach the server, and its own task would hold a worker "
                "until it timed out."
            )
            if server
            else (
                "This node's work items cook in a long-running service process, which "
                "this scheduler does not start."
            )
        )
        remedy: str = (
            (
                "Cook this network with another scheduler. Setting 'pdg_cooktype' to Out "
                "Of Process does not help: it only stops this refusal and leaves the task "
                "running a server nothing connects to."
            )
            if server
            else (
                "Set the node's 'pdg_cooktype' parm to Out Of Process, or cook this "
                "network with another scheduler."
            )
        )
        refusals.append(
            Refusal(
                scenario=RefusalScenario.SERVICE_NODE,
                subject=node.name,
                detail=detail,
                remedy=remedy,
            )
        )
    return Verdict(refusals=tuple(refusals))


def _check_schedulers(ctx: ValidationContext) -> Verdict:
    """One cook becomes one job, so a node that overrides the scheduler is refused.

    The host clears an override that names the cook's own scheduler before
    validation, so any override left here points elsewhere.
    """
    refusals: List[Refusal] = []
    for node in ctx.nodes:
        if not node.scheduler_override:
            continue
        refusals.append(
            Refusal(
                scenario=RefusalScenario.MULTIPLE_SCHEDULERS,
                subject=node.name,
                detail=(
                    f"This node's 'topscheduler' parm sends its work items to "
                    f"'{node.scheduler_override}', and the rest of the cook is one job "
                    "elsewhere."
                ),
                remedy="Clear the node's 'topscheduler' parm so it uses the network's scheduler.",
            )
        )
    return Verdict(refusals=tuple(refusals))


# -- outputs already on disk --------------------------------------------------

#: The Cache Mode menu label that writes results whether or not they already
#: exist, so a node set to it is never cached and is skipped by the check.
_WRITE_FILES: str = "Write Files"


def _cached_output(item: WorkItemSpec, ctx: ValidationContext) -> str:
    """The item's first declared output already on disk, which PDG would treat as cooked from cache, or ``""``."""
    # A path that still holds a PDG token or a variable has no definite location
    # here, and a path the disk cannot answer for is not evidence of a cached
    # result, so both are skipped.
    for declared in item.declared_outputs:
        resolved: str = _resolve(declared.path, ctx.working_dir, ctx.env)
        if PDG_TOKEN_PREFIX in resolved or "$" in resolved:
            continue
        try:
            present: bool = ctx.exists(resolved)
        except Exception:
            _logger.debug("Could not test whether '%s' exists.", resolved, exc_info=True)
            continue
        if present:
            return resolved
    return ""


def _check_cached_outputs(ctx: ValidationContext) -> Verdict:
    """Refuse a step with an item whose declared outputs already exist on this machine.

    PDG's default cache mode marks such an item cooked from cache and never
    schedules it, so its task never runs and the job waits forever. A node set to
    Write Files, or a path that still holds a token or a variable, is skipped.
    """
    refusals: List[Refusal] = []
    # Write Files ignores existing results, so a node set to it is never cached.
    writes_always: Set[str] = {n.name for n in ctx.nodes if n.cache_mode == _WRITE_FILES}
    for step in ctx.snapshot.steps:
        if step.name in writes_always:
            continue
        # Each item with a result on disk is one PDG would skip, so the step is
        # refused when any item does, and the first such file is the example.
        present_files: List[str] = [
            found for item in step.items if (found := _cached_output(item, ctx))
        ]
        if not present_files:
            continue
        cached: int = len(present_files)
        example: str = present_files[0]
        refusals.append(
            Refusal(
                scenario=RefusalScenario.CACHED_OUTPUTS,
                subject=step.name,
                detail=(
                    f"{cached} of this step's {step.task_count} work item(s) declare outputs "
                    f"that already exist, for example '{example}'. PDG would mark them cooked "
                    "from cache and never schedule them, so their tasks would never run and "
                    "the job would wait for them forever."
                ),
                remedy=(
                    "Set the node's Cache Mode (pdg_cachemode) to Write Files, or delete its "
                    "results before cooking (Delete Selected Node Results From Disk)."
                ),
            )
        )
    return Verdict(refusals=tuple(refusals))


# -- the scene's file references ----------------------------------------------


def _check_undeliverable_outputs(ctx: ValidationContext) -> Verdict:
    """Refuse a step whose outputs cannot reach the sessions that need them.

    A consumed step starves the steps below it, so it is refused; a leaf breaks
    nothing downstream but silently leaves no files, so it is advised.
    """
    leaves: Set[str] = set(ctx.snapshot.leaf_steps())
    refusals: List[Refusal] = []
    advisories: List[Advisory] = []
    for step in ctx.snapshot.steps:
        # Each declared output whose resolved path cannot reach a worker, with why.
        # An empty list means the step delivers everything it writes.
        offenders: List[Tuple[str, str]] = [
            (declared.path, reason)
            for item in step.items
            for declared in item.declared_outputs
            if (reason := _undeliverable_reason(declared.path, ctx.working_dir, ctx.env))
        ]
        if not offenders:
            continue
        # Every distinct reason, in the order found, so a step with both faults is
        # told about both and given both remedies.
        reasons: List[str] = list(dict.fromkeys(reason for _path, reason in offenders))
        example: str = offenders[0][0]
        resolved: str = _resolve(example, ctx.working_dir, ctx.env)
        root: str = ctx.working_dir or "the working directory, which is not set"
        detail: str = (
            f"{len(offenders)} of this step's declared output path(s) are "
            f"{' or '.join(_UNDELIVERABLE_PHRASES[reason] for reason in reasons)}, for "
            f"example '{example}', which resolves to '{resolved}'. The job's root is "
            f"'{root}'."
        )
        # A leaf's missing files break nothing downstream, so it is only advised.
        if step.name in leaves:
            advisories.append(
                Advisory(
                    scenario=RefusalScenario.OUTPUT_OUTSIDE_COOK,
                    subject=step.name,
                    detail=(
                        f"{detail} Nothing downstream needs them, so the cook will report "
                        "success and leave no files for this step."
                    ),
                )
            )
            continue
        # A consumed step is refused: the step below would fail on a missing input.
        refusals.append(
            Refusal(
                scenario=RefusalScenario.OUTPUT_OUTSIDE_COOK,
                subject=step.name,
                detail=(
                    f"{detail} Those tasks would write the files, report success, and lose "
                    "them with the session, and the steps below would fail on a missing input."
                ),
                remedy=" ".join(_UNDELIVERABLE_REMEDIES[reason] for reason in reasons),
            )
        )
    return Verdict(refusals=tuple(refusals), advisories=tuple(advisories))


def _check_unportable_paths(ctx: ValidationContext) -> Verdict:
    """Refuse a write parm that names this workstation, and advise about a read.

    The check reads the parm, not the declared output, because a literal path
    agrees with PDG on this machine but not on a worker.
    """
    # Without the scene walk there are no parms to read, so say so rather than
    # passing silently.
    if ctx.reads is None:
        return _skipped()

    refusals: List[Refusal] = []
    advisories: List[Advisory] = []
    for read in ctx.reads.unportable:
        # Only a work item can evaluate a backtick expression or an attribute, so
        # the path it gives here means nothing, as in producer matching.
        if _depends_on_work_item(read.raw or ""):
            continue
        subject: str = _parm_subject(read.step, read.node_path, read.parm)
        where: str = (
            f"The parm is '{read.raw}', which resolves to '{read.path}', inside the working "
            "directory on this machine. Path mapping rewrites paths, not Houdini variables, "
            "so the value does not follow the session."
        )
        # A write is refused, because the worker re-evaluates the parm. A read is
        # advised, because PDG usually supplies the upstream output.
        if read.is_output:
            refusals.append(
                Refusal(
                    scenario=RefusalScenario.OUTPUT_OUTSIDE_COOK,
                    subject=subject,
                    detail=f"{where} The renderer re-evaluates the parm on the worker, where "
                    "that directory does not exist.",
                    remedy="Author the path under $HIP, $PDG_DIR, or $PDG_TEMP.",
                )
            )
        else:
            advisories.append(
                Advisory(
                    scenario=RefusalScenario.OUTPUT_OUTSIDE_COOK,
                    subject=subject,
                    detail=(
                        f"{where} It is harmless only while PDG supplies this input from the "
                        "upstream work item. Prefer $HIP or $PDG_DIR."
                    ),
                )
            )
    return Verdict(refusals=tuple(refusals), advisories=tuple(advisories))


def _check_scene_directory(ctx: ValidationContext) -> Verdict:
    """Advise when the scene and the working directory are in different trees.

    The job carries only the working directory, so $HIP paths point outside it.
    The output check refuses those writes, and the advisory names the cause.
    """
    # No env means this process's environment, as in ``_expand``.
    env: Mapping[str, str] = os.environ if ctx.env is None else ctx.env
    scene_dir: str = env.get("HIP", "")
    # With no scene directory or no working directory there is nothing to compare.
    if not scene_dir or not ctx.working_dir:
        return Verdict()
    scene: str = os.path.normpath(scene_dir)
    root: str = os.path.normpath(ctx.working_dir)
    # When the scene sits under the working directory, $HIP paths are delivered.
    if _is_below(scene, root):
        return Verdict()
    return Verdict(
        advisories=(
            Advisory(
                scenario=RefusalScenario.OUTPUT_OUTSIDE_COOK,
                subject=scene,
                detail=(
                    f"The scene is in '{scene}' and the working directory is '{root}'. The "
                    "working directory is the only tree the job carries, so any node writing "
                    "a path relative to $HIP writes outside it and its files cannot reach a "
                    "step below. Set the working directory to $HIP unless every node already "
                    "declares session-relative outputs."
                ),
            ),
        )
    )


def _check_attribute_variables(ctx: ValidationContext) -> Verdict:
    """Refuse a write parm that spells a work item attribute as a Houdini variable.

    An exported attribute is an environment variable on the worker only, so ``$NAME``
    resolves there and to nothing here, where PDG records the declared output path.
    """
    # Without the scene walk there are no parms to read, so say so rather than
    # passing silently.
    if ctx.reads is None:
        return _skipped()

    # One refusal per parm and variable, so a parm that spells the same variable
    # twice is refused once.
    uses: Dict[Tuple[str, str], AttributeVariableUse] = {}
    for use in ctx.reads.attribute_variables:
        uses.setdefault((_parm_subject(use.step, use.node_path, use.parm), use.variable), use)
    return Verdict(
        refusals=tuple(
            Refusal(
                scenario=RefusalScenario.ATTRIBUTE_AS_VARIABLE,
                subject=subject,
                detail=(
                    f"The parm is '{use.raw}'. '${use.variable}' is the work item attribute "
                    f"'{use.attribute}', which this step exports to the environment, not a "
                    "Houdini variable: it resolves on the worker and to nothing here, where "
                    "PDG records the path it will report."
                ),
                remedy=(
                    f"Use `@{use.attribute}` instead of ${use.variable}, so the value is "
                    "substituted when the work items are generated and both sides agree."
                ),
            )
            for (subject, _variable), use in uses.items()
        )
    )


def _check_runtime_files(ctx: ValidationContext) -> Verdict:
    """Refuse an input file that no task produces and that is not on disk.

    Such a file would be written mid-cook by a node that runs here, and nothing
    carries it: it is no task's output and did not exist when the job was created.
    """
    snapshot: GraphSnapshot = ctx.snapshot
    # Every path some task declares as an output, so a matching input is carried
    # along a step dependency and is not a runtime file.
    produced: Set[str] = {
        declared.path
        for step in snapshot.steps
        for item in step.items
        for declared in item.declared_outputs
    }
    # Each unproduced input path, with the steps that read it, so one missing file
    # is reported once and names every reader.
    consumers: Dict[str, List[str]] = {}
    for step in snapshot.steps:
        for item in step.items:
            for declared in item.declared_inputs:
                if declared.path in produced:
                    continue
                readers: List[str] = consumers.setdefault(declared.path, [])
                if step.name not in readers:
                    readers.append(step.name)

    refusals: List[Refusal] = []
    for path, readers in sorted(consumers.items()):
        resolved: str = _resolve(path, ctx.working_dir, ctx.env)
        # A path that still holds a PDG token or a variable has no definite location
        # here, so a worker resolves it and it is not a missing runtime file.
        if PDG_TOKEN_PREFIX in resolved or "$" in resolved:
            continue
        try:
            # A file already on disk is carried in the job's attachments.
            if ctx.exists(resolved):
                continue
        except Exception:
            # An unanswerable path is not evidence of a fault.
            _logger.debug("Could not test whether '%s' exists.", resolved, exc_info=True)
            continue
        refusals.append(
            Refusal(
                scenario=RefusalScenario.RUNTIME_FILE_UNPRODUCED,
                subject=path,
                detail=(
                    f"Step(s) {_examples(readers)} read this file, no task in this job writes "
                    f"it, and it is not on disk at '{resolved}'. A node that cooks here would "
                    "write it mid-cook, where nothing can carry it to a worker."
                ),
                remedy=(
                    "Give the node that writes it a type that runs on a worker, or create the "
                    "file before cooking so the job carries it."
                ),
            )
        )
    return Verdict(refusals=tuple(refusals))


def _check_unreachable_local_files(ctx: ValidationContext) -> Verdict:
    """Refuse a read that no step produces, lies outside the cook, and is not on disk.

    What is left after producer matching is the scene's own assets. One that exists
    travels in the attachments; one that does not exists nowhere a worker can look.
    """
    # Without the scene walk there are no reads to judge, so say so rather than
    # passing silently.
    if ctx.reads is None:
        return _skipped()

    root: str = os.path.normpath(ctx.working_dir) if ctx.working_dir else ""
    missing: List[SceneRead] = []
    # Per step, the reachable assets outside the working directory, so a step that
    # carries several large files is advised once.
    attached: Dict[str, List[str]] = {}
    for match in ctx.reads.matches:
        read: SceneRead = match.read
        # Only a read no step produces, and not a write, is a candidate: a step's
        # own or a dependency's output reaches the worker along the job's edges.
        if match.verdict is not ReadVerdict.EXTERNAL or read.is_output:
            continue
        # A path that follows the session, or still holds a PDG token, resolves on
        # a worker, so it is not unreachable.
        if _REACHABLE_PREFIX.match((read.raw or "").strip()) or PDG_TOKEN_PREFIX in read.path:
            continue
        resolved: str = os.path.normpath(_expand(read.path, ctx.env))
        # A surviving variable, a relative path, or a path inside the working
        # directory is unanswerable or already carried, so it is skipped.
        if "$" in resolved or not os.path.isabs(resolved) or _is_below(resolved, root):
            continue
        try:
            reachable: bool = ctx.exists(resolved)
        except Exception:
            # An unanswerable path is not evidence of a fault.
            _logger.debug("Could not test whether '%s' exists.", resolved, exc_info=True)
            continue
        if reachable:
            attached.setdefault(read.step, []).append(resolved)
        else:
            missing.append(read)

    # A file nothing on disk holds and no step writes is refused: the task opens a
    # file that is not there.
    refusals: Tuple[Refusal, ...] = tuple(
        Refusal(
            scenario=RefusalScenario.LOCAL_FILE_UNREACHABLE,
            subject=_parm_subject(read.step, read.node_path, read.parm),
            detail=(
                f"The parm is '{read.raw}', which resolves to '{read.path}', outside the "
                "working directory and not on disk. No step in this job writes it, so the "
                "job has nothing to carry and the task opens a missing file."
            ),
            remedy=(
                "Author the path under $HIP or the working directory, or create the file "
                "before cooking so the job carries it."
            ),
        )
        for read in missing
    )
    # A file that does exist is carried in the attachments, which costs upload time
    # on every cook, so a step that reads several is advised.
    advisories: Tuple[Advisory, ...] = tuple(
        Advisory(
            scenario=RefusalScenario.LOCAL_FILE_UNREACHABLE,
            subject=step,
            detail=(
                f"This step reads {len(paths)} file(s) outside the working directory, for "
                f"example {_examples(paths)}. They reach the worker only in the job's "
                "attachments, so a large one costs upload time on every cook."
            ),
        )
        for step, paths in sorted(attached.items())
    )
    return Verdict(refusals=refusals, advisories=advisories)


# Every check, with the scenario it reports. A new refusal is a new function and a
# new row: no other check and no part of ``validate`` changes.
_CHECKS: Tuple[Tuple[RefusalScenario, Callable[[ValidationContext], Verdict]], ...] = (
    (RefusalScenario.DYNAMIC_NODE, _check_dynamic_nodes),
    (RefusalScenario.NO_FARM_WORK, _check_no_farm_work),
    (RefusalScenario.ROP_BATCH, _check_rop_batch),
    (RefusalScenario.QUOTA_EXCEEDED, _check_quotas),
    (RefusalScenario.STEP_WITHOUT_ITEMS, _check_steps_without_items),
    (RefusalScenario.IN_PROCESS_NODE, _check_non_step_file_use),
    (RefusalScenario.SERVICE_NODE, _check_service_nodes),
    (RefusalScenario.MULTIPLE_SCHEDULERS, _check_schedulers),
    (RefusalScenario.OUTPUT_OUTSIDE_COOK, _check_undeliverable_outputs),
    (RefusalScenario.OUTPUT_OUTSIDE_COOK, _check_unportable_paths),
    (RefusalScenario.OUTPUT_OUTSIDE_COOK, _check_scene_directory),
    (RefusalScenario.ATTRIBUTE_AS_VARIABLE, _check_attribute_variables),
    (RefusalScenario.RUNTIME_FILE_UNPRODUCED, _check_runtime_files),
    (RefusalScenario.CACHED_OUTPUTS, _check_cached_outputs),
    (RefusalScenario.LOCAL_FILE_UNREACHABLE, _check_unreachable_local_files),
)
