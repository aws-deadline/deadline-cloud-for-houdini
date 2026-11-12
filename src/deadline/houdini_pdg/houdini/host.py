# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""``PdgHost`` over live PDG and Houdini.

Only this module and the registration module import ``hou`` or ``pdg``. PDG calls the scheduler on its own threads, and ``hou`` is not safe
there while Houdini's UI runs, so every scene read runs on the main thread.
"""

from __future__ import annotations

import dataclasses
import glob
import logging
import os
import re
import threading
from types import ModuleType
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import hou  # type: ignore[import-not-found]
import pdg  # type: ignore[import-not-found]

from ..errors import (
    CookSetupError,
    GraphRefusedError,
    PayloadTransferError,
    format_advisories,
    format_refusals,
)
from ..graph import GraphSnapshot, NodeInfo, find_ancestors_among, synthesize
from ..ports import ItemOutcome, ItemState, SceneFiles, T, WorkItemHandle
from ..submitter import farm_problem, get_workstation_setting, read_local_storage_locations
from ..scene_reads import (
    ReadReport,
    SceneReads,
    VARIABLE_PATTERN,
    _depends_on_work_item,
    _has_root,
    _is_below,
    _path_key,
    match_reads,
)
from ..validation import (
    Refusal,
    RefusalScenario,
    ValidationContext,
    Verdict,
    check_cook_set_has_steps,
    environment_with_hip,
    check_generated_counts,
    check_service_nodes,
    check_shape_quotas,
    validate,
    waived,
)
from . import readers
from .settings import SchedulerSettings

_logger: logging.Logger = logging.getLogger(__name__)

#: The scheduler's node type, which the HDA defines.
_NODE_TYPE: str = "deadlinecloudscheduler"

_WORKING_DIR_PARM: str = "working_dir"
_WORKING_DIR_DEFAULT: str = "$HIP"
_DEBUG_LEVEL_PARM: str = "debug_level"
_VALIDATE_GRAPH_PARM: str = "validate_graph"
#: 0 is the standard record, and 1 adds the graph dump and the item details.
_DEBUG_LEVELS: Tuple[int, ...] = (0, 1)
#: Wins over the parm, so a scene saved before the parm existed can still be debugged.
_DEBUG_LEVEL_ENV: str = "DEADLINE_PDG_DEBUG_LEVEL"
#: A whole number as text, the only form a debug level may take.
_WHOLE_NUMBER: str = r"-?\d+"
_AUTO_SAVE_PARM: str = "auto_save_hip"
#: The title of every dialog the node shows.
_TITLE: str = "Deadline Cloud"
#: The save prompt's buttons, in the order ``hou.ui.displayMessage`` numbers them.
_SAVE_BUTTONS: Tuple[str, ...] = ("Save", "Always Save", "Cancel")

#: PDG's sub-index for an item that is not in a batch. The cook refuses batches.
_NOT_BATCHED: int = -1

#: PDG reads an output's checksum as its change stamp. The file was written on a
#: worker, so there is nothing to stamp here, and 0 is PDG's own default.
_NO_CHECKSUM: int = 0

#: The findings that need generated work items, so a verdict taken before
#: generation would report every step.
_NEEDS_ITEMS: FrozenSet[RefusalScenario] = frozenset({RefusalScenario.STEP_WITHOUT_ITEMS})

_SAVE_REMEDY: str = "Save the scene, then cook again."

#: The environment variables that can name the home directory. Windows sets
#: ``USERPROFILE``, and a shell on any system may set ``HOME``.
_HOME_VARIABLES: Tuple[str, ...] = ("HOME", "USERPROFILE")

# A PDG log line's leading "[HH:MM:SS.fff] " stamp.
_LOG_STAMP: "re.Pattern[str]" = re.compile(r"^\[[0-9:.]+\]\s*")

# ``pdg.workItemState`` names of an item PDG has queued in the current cook.
_QUEUED_STATES: FrozenSet[str] = frozenset({"Waiting", "Scheduled", "Cooking"})

# (node path, parm) pairs already reported missing, so an old node logs each once.
_reported_missing: Set[Tuple[str, str]] = set()
_reported_lock: threading.Lock = threading.Lock()


class HoudiniPdgHost:
    """Implements ``PdgHost`` for one scheduler node."""

    def __init__(self, scheduler: "pdg.scheduler.PyScheduler") -> None:
        self._scheduler: Any = scheduler
        self._lock: threading.Lock = threading.Lock()
        # One host serves one cook, and the cook and the submitter both ask.
        self._scene_files: Optional[SceneFiles] = None
        self._node: Any = None
        # The steps a cook waits for, and those PDG has finished generating.
        self._waiting_for: Set[str] = set()
        self._generated: Set[str] = set()
        self._generation_handlers: Tuple[Any, ...] = ()
        # Separate from ``_lock``, which is held across a main-thread call.
        self._generation_lock: threading.Lock = threading.Lock()

    def read_graph(self) -> Sequence[NodeInfo]:
        """The topology and the generated items, from one ``dependencyGraph`` read."""
        dependency_graph: Any = self._scheduler.dependencyGraph(True)
        return self.on_main_thread(lambda: self._read_graph(dependency_graph))

    def read_scene(self) -> SceneReads:
        """The scene reads of every TOP node, so a node that is not a step is judged too."""
        return self.on_main_thread(
            lambda: readers.collect_scene_reads(
                _top_nodes(_graph_nodes(self._scheduler.context)), expand=_expand_variables
            )
        )

    def scene_files(self) -> SceneFiles:
        """The saved ``.hip`` and the assets the ROP submitter's scan finds.

        Raises ``CookSetupError`` for a path that would attach a whole filesystem
        root or home directory.
        """
        with self._lock:
            if self._scene_files is None:
                node: Any = self.scheduler_node()
                self._scene_files = self.on_main_thread(lambda: _collect_scene_files(node))
            return self._scene_files

    def serialize(self, item: WorkItemHandle) -> str:
        """PDG's own JSON for the work item."""
        if item.native is None:
            raise PayloadTransferError(f"Work item '{item.name}' has no PDG object to serialize.")
        # What PDG's own job RPC returns for an item.
        return str(item.native.saveJSONString(global_attributes=True))

    def notify(self, item: WorkItemHandle, outcome: ItemOutcome) -> None:
        """Report the outcome through PDG's ``onWorkItem*`` callbacks."""
        scheduler: Any = self._scheduler
        item_id: int = item.item_id
        if outcome.state is ItemState.STARTED:
            # The start flag leaves an earlier cook's output records in place, and
            # PDG adds this cook's beside them, so the item is cleared first.
            _clear_outputs(item)
            scheduler.onWorkItemStartCook(item_id, _NOT_BATCHED, True)
        elif outcome.state is ItemState.SUCCEEDED:
            # Each output is reported unchanged, then the success with its duration.
            for output in outcome.outputs:
                scheduler.onWorkItemAddOutput(
                    item_id, _NOT_BATCHED, output.path, output.tag, _NO_CHECKSUM
                )
            scheduler.onWorkItemSucceeded(item_id, _NOT_BATCHED, float(outcome.duration_seconds))
        else:
            scheduler.onWorkItemFailed(item_id, _NOT_BATCHED)

    def set_working_dir(self, path: str) -> None:
        """Use ``path`` as both the local and the remote working directory."""
        self._scheduler.setWorkingDir(path, path)

    def watch_generation(
        self,
        cook_set: Optional[AbstractSet[str]],
        on_quota_exceeded: Optional[Callable[[Verdict], bool]] = None,
    ) -> None:
        """Follow ``NodeGenerated`` for the steps PDG asked for, and count their tasks.

        PDG offers a step's first item while it is still generating the steps
        below it, so a graph read at that moment is missing most of the job. When
        ``on_quota_exceeded`` returns True for a quota refusal, the cook is canceled.
        """
        # Wait for every step, or only those in the cook set, and listen for each to finish.
        context: Any = self._scheduler.context
        steps: Set[str] = {
            _name(node) for node in _graph_nodes(context) if _name(node) and _is_step(node)
        }
        with self._generation_lock:
            self._waiting_for = steps & set(cook_set) if cook_set else steps
            self._generated = set()
        handlers: Tuple[Any, ...] = (
            context.addEventHandler(self._on_node_generated, pdg.EventType.NodeGenerated, True),
        )
        if on_quota_exceeded is not None:
            handlers += (watch_task_counts(context, on_quota_exceeded),)
        with self._generation_lock:
            self._generation_handlers = handlers

    def pending_generation(self) -> Sequence[str]:
        """The watched steps that have neither generated nor queued an item."""
        with self._generation_lock:
            pending: Set[str] = self._waiting_for - self._generated
        if not pending:
            return ()
        # A node generated before this cook may not announce itself again. Once
        # PDG queues one of its items, its generation is done.
        # A dynamic node generates only once its upstream items cook, which the
        # wait holds back. Without PDG's flag this reads ``hou``, so on the main thread.
        nodes: Dict[str, Any] = {
            _name(node): node for node in _graph_nodes(self._scheduler.context)
        }
        dynamic: Set[str] = self.on_main_thread(lambda: readers.find_dynamic_nodes(nodes))
        started: Set[str] = {
            name
            for name, node in nodes.items()
            if name in pending and (_has_queued_item(node) or name in dynamic)
        }
        with self._generation_lock:
            self._generated |= started
            return tuple(sorted(self._waiting_for - self._generated))

    def stop_watching_generation(self) -> None:
        """Remove the event handlers, if there are any."""
        with self._generation_lock:
            handlers: Tuple[Any, ...] = self._generation_handlers
            self._generation_handlers = ()
        for handler in handlers:
            try:
                self._scheduler.context.removeEventHandler(handler)
            except Exception:
                _logger.debug("Could not remove the generation event handler", exc_info=True)

    def failure_reason(self, item_id: int) -> Tuple[str, str]:
        """The ERROR lines of the work item's PDG log, and its last line.

        The last line is kept apart, because without an ERROR line it may be an
        unrelated message.
        """
        try:
            graph: Any = _attr(self._scheduler.context, "graph")
            item: Any = _call(graph, "workItemById", item_id)
            # Drop the time stamps and blank lines, then keep the ERROR lines apart from the last line.
            log: str = str(_attr(item, "logMessages") or "")
            stripped: List[str] = [_LOG_STAMP.sub("", line).strip() for line in log.splitlines()]
            lines: List[str] = [line for line in stripped if line]
            errors: List[str] = [
                line.split("ERROR:", 1)[1].strip() for line in lines if "ERROR:" in line
            ]
            return " ".join(errors), (lines[-1] if lines else "")
        except Exception:
            _logger.debug("Could not read the log of work item %s.", item_id, exc_info=True)
            return "", ""

    def houdini_version(self) -> str:
        """The running Houdini's version, computed as the ROP computes it."""
        from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
            _get_houdini_version,
        )

        return self.on_main_thread(_get_houdini_version)

    def queue_parameter_values(self) -> List[Dict[str, Any]]:
        """The scheduler node's queue parameter values, read as the ROP reads them."""
        from deadline.houdini_submitter.python.deadline_cloud_for_houdini.queue_parameters import (
            get_queue_parameter_values_as_openjd,
        )

        node: Any = self.scheduler_node()
        return self.on_main_thread(lambda: get_queue_parameter_values_as_openjd(node))

    def on_main_thread(self, read: Callable[[], T]) -> T:
        """Run ``read`` on Houdini's main thread whenever a UI exists, and call it directly otherwise."""
        return on_main_thread(read)

    def scheduler_node(self) -> Any:
        """The scheduler's own TOP node, which holds the cook's parms."""
        if self._node is None:
            self._node = self.on_main_thread(lambda: _scheduler_node(self._scheduler))
        return self._node

    def host_requirements(self) -> Optional[Dict[str, Any]]:
        """The node's Host Requirements tab as the job's block, or None for any worker."""
        node: Any = self.scheduler_node()
        return self.on_main_thread(lambda: read_host_requirements(node))

    def show_warning(self, text: str) -> None:
        """Add ``text`` to the scheduler node's Warning tab with PDG's ``cookWarning``."""
        _call(self._scheduler, "cookWarning", text)

    def read_settings(self) -> SchedulerSettings:
        """The cook's settings, from the scheduler node's parms."""
        node: Any = self.scheduler_node()
        return self.on_main_thread(lambda: read_scheduler_settings(node))

    def _on_node_generated(self, _handler: Any, event: Any) -> None:
        """Record the node PDG says finished generating."""
        name: str = _name(_attr(event, "node"))
        if name:
            with self._generation_lock:
                self._generated.add(name)

    def _read_graph(self, dependency_graph: Any) -> Sequence[NodeInfo]:
        """The nodes and their items, with an override naming this scheduler cleared."""
        # Keep only the items in this cook, because the others are an earlier cook's.
        context: Any = self._scheduler.context
        graph_nodes: List[Any] = _graph_nodes(context)
        cook_set: Optional[FrozenSet[str]] = cook_set_names(_attr(context, "cookSet"))
        infos: Tuple[NodeInfo, ...] = readers.node_infos(
            graph_nodes, dependency_graph, cook_set, only_items_in_cook=True
        )
        # Every name an override may use for this scheduler, even when its node can't be found.
        names: Set[str] = {_name(self._scheduler)}
        try:
            names |= _names_of(self.scheduler_node())
        except CookSetupError:
            # With no node to read, the scheduler's own name is the only one an override can use.
            pass
        return _clear_own_overrides(infos, graph_nodes, frozenset(name for name in names if name))


def run_pre_cook_checks(
    node: Any,
    *,
    cook_set: Optional[AbstractSet[str]] = None,
    dependency_graph: Any = None,
) -> Tuple[Verdict, GraphSnapshot]:
    """The checks before a cook on the scheduler ``node`` starts, from the TOP Cook button or Submit.

    Returns the verdict, with a refusal for an unsaved ``.hip``, and the job's shape.
    ``cook_set`` names the nodes the cook runs. With no ``dependency_graph``, only the
    topology is judged. With one, every check runs on its generated items.
    """

    # One trip to the main thread for both checks, since each waits on the UI.
    def check() -> Tuple[Verdict, GraphSnapshot]:
        found: Verdict
        snapshot: GraphSnapshot
        # A settings problem is reported by the cook when it starts, so the topology
        # is still judged without it.
        found, snapshot = _check_network(
            node,
            _get_or_create_graph_context(node),
            _read_settings_or_none(node),
            cook_set,
            dependency_graph,
        )
        verdict: Verdict = Verdict(
            refusals=found.refusals + _check_saved_scene(), advisories=found.advisories
        )
        return verdict, snapshot

    return on_main_thread(check)


def on_main_thread(read: Callable[[], T]) -> T:
    """Call ``read`` on Houdini's main thread, and return its result or raise its error.

    With no UI, as in ``hython``, nothing races, so it is called directly.
    """
    if not hou.isUIAvailable() or threading.current_thread() is threading.main_thread():
        return read()
    import hdefereval  # type: ignore[import-not-found]

    result: T = hdefereval.executeInMainThreadWithResult(read)
    return result


def watch_task_counts(context: Any, stop: Callable[[Verdict], bool]) -> Any:
    """Count each step's tasks as PDG generates it, and cancel once ``stop`` accepts a quota refusal.

    Returns PDG's handler. The counts live only in this handler, and it reports once.
    """
    counts: Dict[str, int] = {}
    stopped: List[bool] = [False]
    lock: threading.Lock = threading.Lock()

    def on_generated(_handler: Any, event: Any) -> None:
        # Only a step's items are tasks. PDG may send events from several threads.
        node: Any = _attr(event, "node")
        name: str = _name(node)
        if not name or not _is_step(node):
            return
        with lock:
            # Record this step's count, and ask the cook to stop once the counts pass a quota.
            if stopped[0]:
                return
            counts[name] = len(_attr(node, "workItems") or ())
            verdict: Verdict = check_generated_counts(counts)
            # Report a quota once per cook, even when the cook doesn't stop for it.
            stopped[0] = bool(verdict.refusals)
            canceling: bool = stopped[0] and stop(verdict)
        # PDG is inside its own generation here, so another thread cancels it.
        if canceling:
            threading.Thread(target=lambda: _call(context, "cancelCook"), daemon=True).start()

    return context.addEventHandler(on_generated, pdg.EventType.NodeGenerated, True)


def cook_set_names(cook_set: Any) -> Optional[FrozenSet[str]]:
    """PDG's cook set, a list of ``pdg.Node``, as node names. None when unknown."""
    try:
        names: FrozenSet[str] = frozenset(_name(node) or str(node) for node in (cook_set or ()))
    except Exception:
        return None
    # An empty set would leave every node out, which is not what PDG asked.
    return names or None


def _clear_outputs(item: WorkItemHandle) -> None:
    """Drop the output records an earlier cook left on the work item.

    An item with no PDG object, or a PDG build whose item refuses the call, keeps
    its records, because raising here would lose the task.
    """
    native: Any = item.native
    if native is None:
        return
    try:
        native.clearOutputFiles()
    except Exception:
        _logger.warning("Could not clear the earlier outputs of '%s'.", item.name, exc_info=True)


# -- settings -----------------------------------------------------------------


def read_scheduler_settings(
    node: Any,
    *,
    read_local_locations: Callable[[], Sequence[str]] = read_local_storage_locations,
) -> SchedulerSettings:
    """The cook's settings from the scheduler node's parms. An absent parm takes its default.

    ``read_local_locations`` gives the queue's LOCAL storage profile paths, and is called only
    for an output folder that shares only "/" with the working directory. Call it on the
    main thread.
    """
    # The working directory must be a real, absolute path.
    working_dir: str = _make_absolute(
        _parm(node, _WORKING_DIR_PARM, _WORKING_DIR_DEFAULT, as_text=True)
    )
    if not working_dir:
        raise CookSetupError(
            f"The '{_WORKING_DIR_PARM}' parm is empty. Set it to the directory the cook "
            "writes to, usually $HIP."
        )
    if "$" in working_dir:
        raise CookSetupError(
            f"The '{_WORKING_DIR_PARM}' parm names a variable that is not set: "
            f"'{working_dir}'. Use a real path, or $HIP."
        )
    return SchedulerSettings(
        working_dir=working_dir,
        debug_level=_read_debug_level(node),
        validate_graph=bool(_parm(node, _VALIDATE_GRAPH_PARM, 1, as_text=False)),
        output_directories=_read_output_directories(node, working_dir, read_local_locations),
    )


def _read_output_directories(
    node: Any, working_dir: str, read_local_locations: Callable[[], Sequence[str]]
) -> Tuple[str, ...]:
    """The Job Attachments tab's output folders, absolute, each checked against the working directory.

    A node from before the tab has none. Raises ``CookSetupError`` naming the parm of a
    folder with an unset variable, one that overlaps the working directory or another
    folder, one that is a drive or home directory, or one that shares only "/" with the
    working directory or another folder that has the same longest LOCAL storage profile
    location, or none.
    """
    folders: Dict[str, Set[str]] = {}
    for index in range(1, int(_parm(node, "output_directories", 0, as_text=False)) + 1):
        # Skip an empty entry, and refuse an unset variable, which no worker could resolve.
        name: str = f"output_dir{index}"
        value: str = str(_parm(node, name, "", as_text=True)).strip()
        if not value:
            continue
        parm_path: str = _path_of(node.parm(name)) or name
        if "$" in value:
            raise CookSetupError(
                f"The '{parm_path}' parm names a variable that is not set: '{value}'."
            )
        folders.setdefault(_make_absolute(value), set()).add(parm_path)
    # A task's scan walks the working directory and each folder, so an overlap records files twice.
    working: str = _absolute_key(working_dir)
    overlapping: List[str] = sorted(
        parm
        for folder, parms in folders.items()
        if _is_below(_absolute_key(folder), working) or _is_below(working, _absolute_key(folder))
        for parm in parms
    )
    if overlapping:
        raise CookSetupError(
            f"The output folder in {', '.join(overlapping)} overlaps the working directory "
            f"'{working_dir}', which every task already uploads. Remove it, or point it at a "
            "folder outside the working directory."
        )
    # The same goes for two folders where one holds the other.
    nested: List[str] = sorted(
        {
            parm
            for folder in folders
            for other in folders
            if folder != other and _is_below(_absolute_key(folder), _absolute_key(other))
            for parm in folders[folder] | folders[other]
        }
    )
    if nested:
        raise CookSetupError(
            f"The output folders in {', '.join(nested)} hold one another, so a task would "
            "record each file in the inner one twice. Keep only the outer folder."
        )
    _refuse_unbounded(folders)
    # Job attachments root each LOCAL location's group at its common path, and a "/" root maps node
    # paths such as /obj/pyro too. Only a folder apart from the working directory can give one.
    any_apart: bool = any(_shares_only_the_posix_root(folder, working_dir) for folder in folders)
    locations: Sequence[str] = read_local_locations() if any_apart else ()
    groups: Dict[str, str] = {
        path: _find_local_location(path, locations) for path in (working_dir, *folders)
    }
    # A folder's group is rooted at "/" when it shares only "/" with any path in the group.
    refused: List[str] = sorted(
        f"{parm} ('{folder}')"
        for folder, parms in folders.items()
        if any(
            groups[other] == groups[folder] and _shares_only_the_posix_root(folder, other)
            for other in groups
        )
        for parm in parms
    )
    if refused:
        raise CookSetupError(
            f"The output folder in {', '.join(refused)} shares only '/' with the working "
            f"directory '{working_dir}' or another output folder, and the queue's storage profile "
            "gives them the same LOCAL location or none. So the job's attachments would be rooted "
            "at '/', and the workers couldn't find the scene's nodes. Put each folder under the "
            "same top-level folder as the working directory, or ask your farm admin to give them "
            "different LOCAL locations in the queue's storage profile."
        )
    return tuple(sorted(folders))


def _shares_only_the_posix_root(path: str, other: str, paths: ModuleType = os.path) -> bool:
    """Whether two absolute POSIX paths have nothing in common but "/".

    A Windows drive or share root never counts, because a worker's rule for it can't match a
    node path. ``paths`` is ``os.path``, or ``ntpath`` in a test of Windows rules.
    """
    try:
        common: str = paths.commonpath([paths.normcase(path), paths.normcase(other)])
    except ValueError:
        # Windows raises this for paths on different drives, which job attachments root apart.
        return False
    return common == "/"


def _find_local_location(path: str, locations: Sequence[str]) -> str:
    """The longest of ``locations`` that holds ``path``, as job attachments pick it, or ``""``."""
    holding: List[str] = [
        location for location in locations if _is_below(_path_key(path), _path_key(location))
    ]
    return max(holding, key=len, default="")


#: Each hardware field of the tab: its parm stem, the library's field stem, and the
#: factor from what the artist types to what the service counts (GiB to MiB).
_HARDWARE_PARMS: Tuple[Tuple[str, str, int], ...] = (
    ("vcpu", "cpu", 1),
    ("memory", "memory", 1024),
    ("gpu", "acceleration", 1),
    ("gpu_memory", "acceleration_memory", 1024),
    ("scratch", "scratch_space", 1),
)
#: What a number parm holds when the artist set nothing.
_NOT_SET: int = -1
#: The prefixes the ROP's dialog puts on custom amount and attribute names.
_CUSTOM_AMOUNT_PREFIX: str = "amount."
_CUSTOM_ATTRIBUTE_PREFIX: str = "attr."


def read_host_requirements(node: Any) -> Optional[Dict[str, Any]]:
    """The Host Requirements tab as the block the ROP's dialog gives, or None for any worker.

    Built with the client library's classes. A value they refuse raises ``CookSetupError``
    naming the parm. Call it on the main thread.
    """
    from deadline.client.ui.dataclasses import (
        CustomAmountRequirement,
        CustomAttributeRequirement,
        CustomRequirements,
        HardwareRequirements,
        HostRequirements,
        OsRequirements,
    )

    if _parm(node, "hr_mode", "all", as_text=True) != "custom":
        return None
    # Operating systems and CPU architectures, each checked on its own to name its parm.
    systems: List[str] = str(_parm(node, "hr_os", "", as_text=True)).split()
    archs: List[str] = str(_parm(node, "hr_cpu_arch", "", as_text=True)).split()
    for parm, chosen in (
        ("hr_os", {"operating_systems": systems}),
        ("hr_cpu_arch", {"cpu_archs": archs}),
    ):
        _build_requirement(lambda: OsRequirements(**chosen), parm)
    # Each min and max pair, converted to the units the service counts.
    hardware: Dict[str, int] = {}
    for stem, field, factor in _HARDWARE_PARMS:
        pair: Dict[str, int] = {}
        for end in ("min", "max"):
            typed: int = int(_parm(node, f"hr_{stem}_{end}", _NOT_SET, as_text=False))
            pair[f"{field}_{end}"] = typed * factor if typed != _NOT_SET else _NOT_SET
        _build_requirement(lambda: HardwareRequirements(**pair), f"hr_{stem}_min and hr_{stem}_max")
        hardware.update(pair)
    # The custom amounts and attributes, one multiparm entry each, numbered from 1.
    amounts: List[Any] = []
    for index in range(1, int(_parm(node, "hr_amounts", 0, as_text=False)) + 1):
        bounds: List[int] = [
            int(_parm(node, f"hr_amount_{end}{index}", _NOT_SET, as_text=False))
            for end in ("min", "max")
        ]
        amount: Any = _build_requirement(
            lambda: CustomAmountRequirement(
                name=str(_parm(node, f"hr_amount_name{index}", "", as_text=True)).strip(),
                min=bounds[0] if bounds[0] != _NOT_SET else CustomAmountRequirement.DEFAULT_VALUE,
                max=bounds[1] if bounds[1] != _NOT_SET else CustomAmountRequirement.DEFAULT_VALUE,
            ),
            f"hr_amount_name{index}, hr_amount_min{index} and hr_amount_max{index}",
        )
        amounts.append(amount)
    attributes: List[Any] = []
    for index in range(1, int(_parm(node, "hr_attributes", 0, as_text=False)) + 1):
        values: List[str] = [
            value.strip()
            for value in str(_parm(node, f"hr_attr_values{index}", "", as_text=True)).split(",")
            if value.strip()
        ]
        # The library checks the values only when it serializes the entry.
        attribute: Any = _build_requirement(
            lambda: CustomAttributeRequirement(
                name=str(_parm(node, f"hr_attr_name{index}", "", as_text=True)).strip(),
                option=str(_parm(node, f"hr_attr_option{index}", "anyOf", as_text=True)),
                values=values,
            ),
            f"hr_attr_name{index}, hr_attr_option{index} and hr_attr_values{index}",
        )
        _build_requirement(attribute.serialize, f"hr_attr_values{index}")
        attributes.append(attribute)
    # The library writes an empty list for a group with nothing set, and the service
    # refuses an empty block, so both are dropped.
    serialized: Dict[str, Any] = HostRequirements(
        os_requirements=OsRequirements(operating_systems=systems, cpu_archs=archs),  # type: ignore[arg-type]
        hardware_requirements=HardwareRequirements(**hardware),
        custom_requirements=CustomRequirements(),
    ).serialize()
    # The library names custom entries amount.worker.<name> and attr.worker.<name>, a
    # namespace Open Job Description keeps for itself, so they are renamed as the ROP names them.
    for group, prefix, entries in (
        ("amounts", _CUSTOM_AMOUNT_PREFIX, amounts),
        ("attributes", _CUSTOM_ATTRIBUTE_PREFIX, attributes),
    ):
        serialized.setdefault(group, []).extend(
            {**entry.serialize(), "name": prefix + entry.name} for entry in entries
        )
    block: Dict[str, Any] = {group: entries for group, entries in serialized.items() if entries}
    return block or None


def _build_requirement(build: Callable[[], T], parms: str) -> T:
    """``build()``, with a ``ValueError`` from the library raised as ``CookSetupError`` naming ``parms``."""
    try:
        return build()
    except ValueError as e:
        raise CookSetupError(
            f"The Host Requirements tab's {parms} parm(s) are not valid: {e}"
        ) from e


def _read_debug_level(node: Any) -> int:
    """The debug level: the environment variable when it is valid, else the parm."""
    override: str = os.environ.get(_DEBUG_LEVEL_ENV, "").strip()
    valid: bool = bool(re.fullmatch(_WHOLE_NUMBER, override)) and int(override) in _DEBUG_LEVELS
    if override and not valid:
        _logger.warning(
            "Ignoring %s=%r: it must be 0 or 1.",
            _DEBUG_LEVEL_ENV,
            override,
        )
    # The parm holds a menu index, so anything else is a damaged scene.
    value: Any = override if valid else _parm(node, _DEBUG_LEVEL_PARM, 0, as_text=False)
    text: str = str(value).strip()
    level: int = int(text) if re.fullmatch(_WHOLE_NUMBER, text) else -1
    if level not in _DEBUG_LEVELS:
        raise CookSetupError(f"The '{_DEBUG_LEVEL_PARM}' parm must be 0 or 1, not {value!r}.")
    return level


def _parm(node: Any, name: str, default: Any, *, as_text: bool) -> Any:
    """The parm's value, or ``default`` when the node lacks it. An unreadable parm raises ``CookSetupError``."""
    parm: Any = node.parm(name)
    if parm is None:
        # Log once per node and parm that the parm is missing, and which default is used.
        key: Tuple[str, str] = (_path_of(node), name)
        with _reported_lock:
            first: bool = key not in _reported_missing
            _reported_missing.add(key)
        if first:
            _logger.warning(
                "Node %s has no '%s' parm, so it uses the default, %r. Recreate the node to get it.",
                key[0] or "?",
                name,
                default,
            )
        return hou.text.expandString(default) if as_text else default
    try:
        return parm.evalAsString() if as_text else parm.eval()
    except Exception as e:
        raise CookSetupError(f"The '{name}' parm cannot be read: {e}") from e


def _make_absolute(path: str) -> str:
    """``path`` made absolute and normalized, or ``""`` for an empty path."""
    stripped: str = (path or "").strip()
    return os.path.normpath(os.path.abspath(os.path.expanduser(stripped))) if stripped else ""


def _absolute_key(path: str, paths: ModuleType = os.path) -> str:
    """``path`` made absolute and spelled by ``_path_key``, for comparing it with another path."""
    return _path_key(paths.abspath(paths.expanduser(path.strip())), paths)


def _drop_listed_paths(
    found: Iterable[str], drop: Iterable[str], paths: ModuleType = os.path
) -> Set[str]:
    """``found`` less each path that names a file in ``drop``, however each is spelled."""
    dropped: Set[str] = {_absolute_key(path, paths) for path in drop if path.strip()}
    return {path for path in found if _absolute_key(path, paths) not in dropped}


# -- the graph ----------------------------------------------------------------


def _scheduler_node(scheduler: Any) -> Any:
    """The scheduler's TOP node: PDG's own link first, then the only node of this type by name."""
    linked: Any = _call(scheduler, "topNode")
    # With no link, the node is matched by the scheduler's name among this type's nodes.
    name: str = _name(scheduler)
    node_type: Any = (
        None
        if linked is not None
        else _call(hou, "nodeType", _call(hou, "topNodeTypeCategory"), _NODE_TYPE)
    )
    instances: List[Any] = list(_call(node_type, "instances") or ())
    named: List[Any] = [n for n in instances if _call(n, "name") == name]
    # The only node of the type is used even when its name differs.
    matches: List[Any] = (
        [linked] if linked is not None else named or (instances if len(instances) == 1 else [])
    )
    if len(matches) != 1:
        raise CookSetupError(
            f"Cannot find the node of scheduler '{name}' in the scene, so its settings "
            "cannot be read. Recreate the scheduler node."
        )
    return matches[0]


def _graph_nodes(context: Any) -> List[Any]:
    """The PDG nodes of a graph context, or none."""
    graph: Any = _attr(context, "graph")
    nodes: Any = _call(graph, "nodes") if graph is not None else None
    return list(nodes or ())


def _expand_variables(text: str) -> str:
    """``text`` with Houdini's variables expanded. Raises for a variable Houdini does not define.

    The test is the session's own expansion, not the environment, so ``$F4`` and ``$T``
    count as defined. An unknown variable gives nothing or stays as typed, which breaks a path.
    """
    for braced, bare in VARIABLE_PATTERN.findall(text):
        name: str = braced or bare
        if str(hou.text.expandString("$" + name)) in ("", "$" + name):
            raise KeyError(name)
    return str(hou.text.expandString(text))


def _has_queued_item(node: Any) -> bool:
    """Whether PDG has queued one of the node's items in this cook."""
    return any(
        readers._enum_name(_attr(item, "state")) in _QUEUED_STATES
        for item in _attr(node, "workItems") or ()
    )


def _names_of(node: Any) -> Set[str]:
    """The node's name, path, and PDG node name, any of which an override may hold."""
    # An override may name the node, its path, or its PDG name.
    names: Set[str] = {str(_call(node, "name") or ""), _path_of(node)}
    pdg_name: Any = _call(node, "getPDGNodeName")
    if pdg_name:
        names.add(str(pdg_name))
    return {name for name in names if name}


def _clear_own_overrides(
    infos: Sequence[NodeInfo], graph_nodes: Iterable[Any], own_names: AbstractSet[str]
) -> Tuple[NodeInfo, ...]:
    """Clear an override that names this scheduler, because it changes nothing.

    The override is resolved on the node itself, because PDG keeps a node's old
    scheduler until the next cook. PDG's answer is used only when that fails.
    """
    # Per node name: the node the override names, by path, and PDG's resolved scheduler.
    authored: Dict[str, str] = {}
    resolved: Dict[str, str] = {}
    for node in graph_nodes:
        name: str = _name(node)
        if not name:
            continue
        # PDG resolves the override's path from the step, so a bare sibling name names no node.
        hom: Any = _call(node, "topNode")
        target: str = _path_of(_call(hom, "node", readers._scheduler_override(hom)))
        if target:
            authored[name] = target
        scheduler: str = _name(_attr(node, "scheduler"))
        if scheduler:
            resolved[name] = scheduler
    return tuple(
        (
            dataclasses.replace(info, scheduler_override="")
            if info.scheduler_override
            and _names_own(info, authored.get(info.name), resolved.get(info.name), own_names)
            else info
        )
        for info in infos
    )


def _names_own(
    info: NodeInfo, authored: Optional[str], resolved: Optional[str], own_names: AbstractSet[str]
) -> bool:
    """Whether the node's override names this scheduler."""
    # A node the override resolves to decides. Otherwise its text or PDG's answer does.
    if authored:
        return authored in own_names
    return info.scheduler_override in own_names or resolved in own_names


def _is_step(node: Any) -> bool:
    """Whether a PDG node runs a job script, which makes it a step."""
    try:
        return bool(node.scriptInfo.command)
    except Exception:
        return False


def _top_nodes(graph_nodes: Iterable[Any]) -> Dict[str, Any]:
    """Every PDG node's ``hou.TopNode``, by PDG node name, steps or not."""
    return {_name(node): _call(node, "topNode") for node in graph_nodes if _name(node)}


# -- scene files --------------------------------------------------------------


def _collect_scene_files(node: Any) -> SceneFiles:
    """The saved ``.hip``, the scene's assets, and the node's listed inputs.

    Raises ``CookSetupError`` for an unbounded path, or a listed one that doesn't exist.
    """
    hip_file: str = _saved_hip_file()
    found: Dict[str, Set[str]] = _scan_scene_assets(node)
    scanned: Set[str] = set(found)
    # The artist's own files and folders join the scan, each owned by its parm.
    for path, parm in read_listed_inputs(node):
        found.setdefault(path, set()).add(parm)
    paths: Set[str] = _drop_listed_paths(found, {hip_file} | _integration_libraries())
    # The listed inputs were checked when they were read, so only the scan's are checked here.
    _refuse_unbounded({path: found[path] for path in paths if path in scanned})
    # Job attachments rejects a directory listed as a file. An asset library can
    # be stored expanded, as a directory.
    directories: Set[str] = {path for path in paths if os.path.isdir(path)}
    return SceneFiles(
        hip_file=hip_file,
        assets=tuple(sorted(paths - directories)),
        directories=tuple(sorted(directories)),
    )


def read_listed_inputs(node: Any) -> List[Tuple[str, str]]:
    """The Job Attachments tab's input files and folders, as (absolute path, parm path).

    A time variable such as ``$F4`` matches every frame, as on the ROP. Raises
    ``CookSetupError`` naming the parm of a path that doesn't exist, or that is a
    drive or home directory. Call it on the main thread.
    """
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets import (
        _houdini_time_vars_to_glob,
    )

    found: List[Tuple[str, str]] = []
    for list_parm, entry, kind in (
        ("input_filenames", "input_filenames", os.path.isfile),
        ("input_directories", "input_dir", os.path.isdir),
    ):
        for index in range(1, int(_parm(node, list_parm, 0, as_text=False)) + 1):
            # The text as typed, with each time variable made a glob, then expanded.
            parm: Any = node.parm(f"{entry}{index}")
            raw: str = str(_call(parm, "unexpandedString") or "").strip()
            if not raw:
                continue
            pattern: str = _houdini_time_vars_to_glob(raw)
            expanded: str = _make_absolute(str(hou.text.expandString(pattern)))
            matched: List[str] = sorted(glob.glob(expanded)) if pattern != raw else [expanded]
            owner: str = _path_of(parm) or f"{entry}{index}"
            existing: List[str] = [path for path in matched if kind(path)]
            if not existing:
                raise CookSetupError(
                    f"The '{owner}' parm names '{raw}', which is not on disk as a "
                    f"{'file' if kind is os.path.isfile else 'folder'}. Fix the path, or "
                    "remove the entry."
                )
            found.extend((_make_absolute(path), owner) for path in existing)
    # A listed home folder would hash all of it, the same as a scene parm naming it.
    owners: Dict[str, Set[str]] = {}
    for path, owner in found:
        owners.setdefault(path, set()).add(owner)
    _refuse_unbounded(owners)
    return found


# The node types of this integration. A worker never cooks them, and their
# libraries are installed with the submitter, not with the scene.
_INTEGRATION_NODE_TYPES: Tuple[Tuple[str, str], ...] = (
    ("topNodeTypeCategory", "deadlinecloudscheduler"),
    ("ropNodeTypeCategory", "deadline_cloud"),
)


def _integration_libraries() -> Set[str]:
    """The asset libraries that define this integration's own node types."""
    libraries: Set[str] = set()
    for category, name in _INTEGRATION_NODE_TYPES:
        try:
            node_type: Any = hou.nodeType(getattr(hou, category)(), name)
            definition: Any = node_type.definition() if node_type is not None else None
            if definition is not None:
                libraries.add(_make_absolute(str(definition.libraryFilePath())))
        except Exception:
            _logger.debug("Could not find the library of node type '%s'.", name, exc_info=True)
    return libraries


def _scan_scene_assets(node: Any) -> Dict[str, Set[str]]:
    """The ROP's scene scan, as absolute paths that exist, each with the parms that name it.

    The scan returns references as authored, with ``$HIP`` and ``$F`` intact. A
    reference that holds an expression or a work item attribute, such as
    `` `@pdg_input` ``, is left out, because the step that writes it delivers it.
    """
    try:
        from deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets import (
            _IGNORE_REF_VALUES,
            _get_scene_asset_references,
            _houdini_time_vars_to_glob,
        )

        refs: Any = _get_scene_asset_references(node)
        candidates: Set[str] = set(refs.input_filenames) | set(refs.input_directories)
        # The parms that hold each reference. An installed asset library is a
        # reference that no parm holds, and the shared scan skips those.
        owners: Dict[str, Set[str]] = {}
        for parm, ref in hou.fileReferences():
            if not ref or str(ref).startswith(_IGNORE_REF_VALUES):
                continue
            if parm is None:
                candidates.add(str(ref))
            owners.setdefault(str(ref), set()).add(
                _path_of(parm) if parm is not None else "an asset library"
            )
    except Exception:
        _logger.warning(
            "The scene's assets could not be scanned, so files the scene references by "
            "path may be missing on the workers.",
            exc_info=True,
        )
        return {}

    assets: Dict[str, Set[str]] = {}
    for ref in sorted(candidates):
        if not ref:
            continue
        if _depends_on_work_item(ref):
            _logger.info(
                "Not attaching '%s': it depends on a work item, so the step that writes "
                "the file delivers it.",
                ref,
            )
            continue
        # Expand the variables, and keep only a path with a root and none left. A
        # relative path would resolve against Houdini's current directory.
        pattern: str = _houdini_time_vars_to_glob(ref)
        evaluated: str = os.path.expanduser(hou.text.expandString(pattern).strip())
        if not _has_root(evaluated) or "$" in evaluated:
            continue
        expanded: str = _make_absolute(evaluated)
        matched: List[str] = (
            [_make_absolute(path) for path in glob.glob(expanded)]
            if pattern != ref
            else ([expanded] if os.path.exists(expanded) else [])
        )
        for path in matched:
            assets.setdefault(path, set()).update(owners.get(ref, {ref}))
    return assets


def _refuse_unbounded(owners: Mapping[str, Set[str]]) -> None:
    """Raise ``CookSetupError`` naming each path in ``owners`` that is a filesystem root, a home directory, or above one.

    Attaching such a path would hash every file below it before anything is
    submitted, so the cook would hang with no message.
    """
    # Each home directory that is set. ``_is_unbounded`` spells it as it spells the path.
    homes: List[str] = [os.environ[name] for name in _HOME_VARIABLES if os.environ.get(name)]
    unbounded: List[str] = sorted(path for path in owners if _is_unbounded(path, homes))
    if not unbounded:
        return
    named: str = "; ".join(f"'{path}' from {', '.join(sorted(owners[path]))}" for path in unbounded)
    raise CookSetupError(
        f"The scene names a folder that would attach a whole drive or home directory: {named}. "
        "Point each parm at the folder that holds only what the scene needs, or save the "
        ".hip in its own project folder."
    )


def _is_unbounded(path: str, homes: Sequence[str], paths: ModuleType = os.path) -> bool:
    """Whether ``path`` is a filesystem root, one of ``homes``, or a directory that holds one.

    Both sides are compared by ``_absolute_key``, so separators and case don't matter
    where the filesystem ignores them.
    """
    # A root is its own parent. Above home means home starts with this path.
    folded: str = _absolute_key(path, paths)
    if paths.dirname(folded) == folded:
        return True
    prefix: str = folded.rstrip(paths.sep) + paths.sep
    keys: List[str] = [_absolute_key(home, paths) for home in homes if home.strip()]
    return any(home == folded or home.startswith(prefix) for home in keys)


# -- pre-cook checks ----------------------------------------------------------


def prompt_to_save(node: Any) -> None:
    """Save unsaved changes before a submit: at once when the node's auto-save is on, else after asking.

    The prompt offers Save, Always Save, which also turns auto-save on, and Cancel,
    which raises ``UserInitiatedCancel``.
    """
    if not hou.isUIAvailable() or hou.hipFile.isNewFile() or not hou.hipFile.hasUnsavedChanges():
        # A scene that was never saved needs a file name, which this prompt cannot
        # ask for, so the refusal tells the artist to save it.
        return
    parm: Any = node.parm(_AUTO_SAVE_PARM)
    if not (parm is not None and parm.eval()):
        choice: int = hou.ui.displayMessage(
            "The scene has unsaved changes. The farm cooks the saved scene, so save it first.",
            title=_TITLE,
            buttons=_SAVE_BUTTONS,
            default_choice=0,
            close_choice=_SAVE_BUTTONS.index("Cancel"),
        )
        if choice not in (0, 1):
            from deadline.client.exceptions import UserInitiatedCancel

            raise UserInitiatedCancel("Submission canceled.")
        # Always Save turns auto-save on, so the next submit saves without asking.
        if choice == 1 and parm is not None:
            parm.set(1)
    hou.hipFile.save()


def _check_saved_scene() -> Tuple[Refusal, ...]:
    """A refusal when the ``.hip`` was never saved, or, with a UI, has unsaved changes."""
    path: str = str(hou.hipFile.path() or "")
    # hython reports unsaved changes even right after a save, and a session with no
    # UI has no artist editing the scene, so only a graphical session is checked.
    detail: str = (
        "The scene has never been saved, so there is no .hip for the workers to load."
        if hou.hipFile.isNewFile()
        else (
            "The scene has unsaved changes. The workers load the saved .hip, so they would "
            "cook the scene as it was last saved, not as it is now."
            if hou.isUIAvailable() and hou.hipFile.hasUnsavedChanges()
            else ""
        )
    )
    if not detail:
        return ()
    return (
        Refusal(
            scenario=RefusalScenario.UNSAVED_SCENE,
            subject=path or "the scene",
            detail=detail,
            remedy=_SAVE_REMEDY,
        ),
    )


def _check_network(
    node: Any,
    context: Any,
    settings: Optional[SchedulerSettings],
    cook_set: Optional[AbstractSet[str]],
    dependency_graph: Any,
) -> Tuple[Verdict, GraphSnapshot]:
    """The verdict and the job's shape for the TOP network, before a cook.

    With no ``dependency_graph``, nothing is generated yet, so only the checks the
    topology answers are kept. With one, every check runs on the generated items.
    With no ``settings``, the checks run without a working directory and are not waived.
    """
    if context is None:
        _logger.warning(
            "The TOP network has no PDG graph yet, so the checks that need it run when the "
            "cook starts."
        )
        return Verdict(), GraphSnapshot(steps=())
    graph_nodes: List[Any] = _graph_nodes(context)
    infos: Tuple[NodeInfo, ...] = _clear_own_overrides(
        readers.node_infos(graph_nodes, dependency_graph, cook_set), graph_nodes, _names_of(node)
    )
    snapshot: GraphSnapshot = synthesize(infos)
    working_dir: str = settings.working_dir if settings is not None else ""
    folders: Tuple[str, ...] = settings.output_directories if settings is not None else ()
    scene: SceneReads = readers.collect_scene_reads(
        _top_nodes(graph_nodes), expand=_expand_variables
    )
    # The reads are matched as the cook matches them. Before generation there are
    # no declared outputs, so every read would look external and the matches are dropped.
    reads: Optional[ReadReport] = (
        match_reads(snapshot, scene.reads, working_dir, output_directories=folders)
        if scene.available
        else None
    )
    generated: bool = dependency_graph is not None
    shape: GraphSnapshot = (
        synthesize(infos, data_edges=reads.data_edges)
        if reads is not None and generated
        else snapshot
    )
    judged: Optional[ReadReport] = (
        dataclasses.replace(reads, matches=(), data_edges={}, attribute_variables=())
        if reads is not None and not generated
        else reads
    )
    full: Verdict = validate(
        ValidationContext(
            nodes=infos,
            snapshot=shape,
            reads=judged,
            working_dir=working_dir,
            output_directories=folders,
            env=environment_with_hip(_saved_hip_file()),
        )
    )
    # Before generation, the checks that need items would report every step, so
    # they wait. A cook set with no step, and the job's shape, are known now.
    waiting: FrozenSet[RefusalScenario] = frozenset() if generated else _NEEDS_ITEMS
    topology: Tuple[Refusal, ...] = () if generated else check_shape_quotas(infos).refusals
    # PDG's cook type is the last generate's, so a node just set to Service is found from its
    # parm too, as Submit finds it. A refusal both reads find is kept once.
    by_parm: Tuple[Refusal, ...] = (
        ()
        if generated
        else tuple(
            r
            for r in _check_service_nodes_by_parm(infos, graph_nodes).refusals
            if r not in full.refusals
        )
    )
    verdict: Verdict = Verdict(
        refusals=tuple(r for r in full.refusals if r.scenario not in waiting)
        + topology
        + by_parm
        + check_cook_set_has_steps(infos, cook_set).refusals,
        advisories=tuple(a for a in full.advisories if a.scenario not in waiting),
    )
    return (verdict if settings is None or settings.validate_graph else waived(verdict)), shape


def _check_service_nodes_by_parm(infos: Tuple[NodeInfo, ...], graph_nodes: List[Any]) -> Verdict:
    """The Service node refusals, with each node's cook type read from its own Cook Type parm.

    PDG keeps the last generate's cook type, so a node the artist just set to Service reads stale.
    """
    homs: Dict[str, Any] = _top_nodes(graph_nodes)
    return check_service_nodes(
        ValidationContext(
            nodes=tuple(
                dataclasses.replace(
                    info, cook_type=readers.read_cook_type(None, homs.get(info.name))
                )
                for info in infos
            ),
            snapshot=GraphSnapshot(steps=()),
        )
    )


def _read_settings_or_none(node: Any) -> Optional[SchedulerSettings]:
    """The node's settings, or None when a parm is wrong."""
    try:
        return read_scheduler_settings(node)
    except CookSetupError:
        return None


def _get_or_create_graph_context(node: Any) -> Any:
    """The node's PDG graph context, creating the PDG nodes first when there is none."""
    context: Any = _call(node, "getPDGGraphContext")
    if context is not None:
        return context
    # Creates the PDG nodes without generating or cooking anything.
    _call(node, "cookWorkItems", block=True, tops_only=True)
    return _call(node, "getPDGGraphContext")


def _saved_hip_file() -> str:
    """The saved ``.hip``, absolute, or ``""`` for a scene that has no file yet."""
    hip: str = str(hou.hipFile.path() or "")
    return _make_absolute(hip) if hip else ""


# -- duck typing --------------------------------------------------------------


def _attr(obj: Any, name: str) -> Any:
    """``obj.name``, or None when it is absent or raises."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _call(obj: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """``obj.name(*args, **kwargs)``, or None when it is absent or raises, which is logged."""
    try:
        return getattr(obj, name)(*args, **kwargs)
    except Exception:
        _logger.debug("%s failed", name, exc_info=True)
        return None


def _name(obj: Any) -> str:
    """A PDG object's ``name`` attribute, or ``""``."""
    return str(_attr(obj, "name") or "")


def _path_of(node: Any) -> str:
    """A HOM node's or parm's path, or ``""``."""
    return str(_call(node, "path") or "")


# -- the node's layout -------------------------------------------------------

#: The tab the ROP puts its queue parameter folders in, by its label.
_QUEUE_PARAMETERS_TAB: str = "Shared Job Settings"


def _find_queue_folders(node: Any) -> List[Any]:
    """The ROP's queue parameter folders in the tab, which are the ones holding its prefixed fields."""
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini.queue_parameters import (
        _QUEUE_ENVIRONMENT_NAME_PREFIX,
    )

    tab: Any = node.parmTemplateGroup().findFolder(_QUEUE_PARAMETERS_TAB)
    folders: List[Any] = [
        template
        for template in (tab.parmTemplates() if tab is not None else ())
        if template.type() == hou.parmTemplateType.Folder
        and any(
            field.name().startswith(_QUEUE_ENVIRONMENT_NAME_PREFIX)
            for field in template.parmTemplates()
        )
    ]
    return folders


def move_queue_folders_above_submit(node: Any) -> None:
    """Put the queue parameter folders above Submit, so Submit stays last in its tab.

    The ROP's queue update adds each folder at the end of the tab, so call this after it.
    """
    # With no folder, nothing moves, so the node's layout isn't stored on it for nothing.
    folders: List[Any] = _find_queue_folders(node)
    if not folders:
        return
    # Each folder is taken out and put back just before Submit, keeping their order.
    group: Any = node.parmTemplateGroup()
    for folder in folders:
        group.remove(folder.name())
        group.insertBefore("submit", folder)
    node.setParmTemplateGroup(group)


def rebuild_layout_from_definition(node: Any) -> None:
    """Lay the node's parms out as its current definition does, keeping the queue parameter fields.

    The ROP's queue parameter update stores the node's whole layout on the node, so a node
    saved before the definition changed keeps its old tabs. Call this before that update.
    """
    # Each field's value is kept, because the folders come back with their defaults.
    folders: List[Any] = _find_queue_folders(node)
    values: Dict[str, Any] = {
        field.name(): node.parmTuple(field.name()).eval()
        for folder in folders
        for field in folder.parmTemplates()
    }
    # Removing every spare parm brings back the definition's tabs, and the definition's own
    # parms keep their values. The folders then go back above Submit, as after a queue update.
    node.removeSpareParms()
    for folder in folders:
        node.addSpareParmTuple(folder, (_QUEUE_PARAMETERS_TAB,))
    move_queue_folders_above_submit(node)
    for name, value in values.items():
        node.parmTuple(name).set(value)


# -- the node's Submit button ------------------------------------------------

_CONFIRM_BUTTONS: Tuple[str, ...] = ("Submit", "Cancel")

#: Submit's stop for each scheduler node by path, as (error, advisories), until its cook start
#: returns. A module dict doesn't mark the scene unsaved.
submit_stops: Dict[str, Tuple[str, str]] = {}


def submit(node: Any, *, get_setting: Optional[Callable[[str], str]] = None) -> bool:
    """Run the node's Submit. Return True when the cook started.

    First the unsaved-scene check, then the network's default scheduler, the farm,
    the node's tabs and any Service node, then a generate and every check on its items,
    then one confirmation, then a cook of the display node. Each step raises the problem
    it finds, and one place shows it in a popup and, once the network has a PDG graph, on the
    node's error tab. A task quota stop during the generate is shown in the popup only.
    """
    from deadline.client.exceptions import UserInitiatedCancel

    read: Callable[[str], str] = get_setting or get_workstation_setting
    # Set once the network has a PDG graph, and from then on a stop goes on the node's tabs
    # through a cook start. Before that, a cook could run on another scheduler or build the graph.
    display: Any = None
    context: Any = None
    # The task quota refusals found while the display node generates. That stop is popup only,
    # because the cook start after its canceled generate fails before onStartCook.
    over_quota: List[Verdict] = []
    try:
        # The save prompt comes first, so the artist answers it before anything else.
        prompt_to_save(node)
        network: Any = node.parent()
        _check_default_scheduler(node, network)
        display = network.displayNode()
        if display is None:
            raise CookSetupError(
                "The TOP network has no display node, so there is nothing to cook. Set one."
            )
        # The graph context and the cook set for the quota check and the generate. The final
        # check reads the context and settings again, the same unless the generate changed them.
        context = _get_or_create_graph_context(node)
        # Every check below reads the graph, so a network without one can't be judged.
        if context is None:
            raise CookSetupError(
                "The TOP network has no PDG graph yet, so Submit can't check it. "
                "Cook the network's TOP nodes once, then submit again."
            )
        farm: str = farm_problem(read)
        if farm:
            raise CookSetupError(farm)
        # The node's tabs are read next, so a bad value is shown before anything is generated.
        settings: SchedulerSettings = read_scheduler_settings(
            node, read_local_locations=lambda: read_local_storage_locations(read)
        )
        requirements: Optional[Dict[str, Any]] = read_host_requirements(node)
        inputs: List[Tuple[str, str]] = read_listed_inputs(node)
        cook_set: Optional[FrozenSet[str]] = _find_cook_set(context, display)
        graph_nodes: List[Any] = _graph_nodes(context)
        infos: Tuple[NodeInfo, ...] = readers.node_infos(graph_nodes, None, cook_set)
        # A Service node's generate starts PDG's service processes, so it's refused first.
        services: Verdict = _check_service_nodes_by_parm(infos, graph_nodes)
        # A job shape over a quota is known from the wires, so nothing is generated for
        # it either. Both stops wait unless the artist turned validation off.
        before_generate: Verdict = Verdict(
            refusals=check_shape_quotas(infos).refusals + services.refusals
        )
        if before_generate.refusals and settings.validate_graph:
            # The dirty clears an earlier Submit's text from the tabs, and unlike a generate
            # it starts no PDG process.
            if services.refusals:
                display.dirtyAllWorkItems(False)
            raise GraphRefusedError(before_generate)

        # Generate the display node's items, so every check sees the job the cook would
        # submit, and stop at the first step that passes a task quota.
        def stop(verdict: Verdict) -> bool:
            if settings.validate_graph:
                over_quota.append(verdict)
            return settings.validate_graph

        # PDG keeps old items after a parm change on a SOP outside the network, so dirty them
        # first. False keeps the artist's files on disk.
        display.dirtyAllWorkItems(False)
        handler: Any = watch_task_counts(context, stop)
        try:
            display.generateStaticWorkItems(block=True)
        finally:
            _call(context, "removeEventHandler", handler)
        if over_quota:
            raise GraphRefusedError(over_quota[0])
        # A canceled or failed generate still returns. Part of a graph can't be judged, so Submit stops.
        unfinished: str = _explain_unfinished_generate(graph_nodes, infos, cook_set)
        if unfinished:
            raise CookSetupError(unfinished)
        # Read the items from this scheduler, as the cook does, and check them all.
        scheduler: Any = _call(
            context, "schedulerForName", _call(node, "getPDGNodeName") or node.name()
        )
        dependency_graph: Any = _call(scheduler, "dependencyGraph", True)
        verdict: Verdict
        snapshot: GraphSnapshot
        verdict, snapshot = run_pre_cook_checks(
            node, cook_set=cook_set, dependency_graph=dependency_graph
        )
        if verdict.refusals:
            raise GraphRefusedError(verdict)
    except UserInitiatedCancel:
        return False
    except (GraphRefusedError, CookSetupError) as stopped:
        # A refusal's text lists each refusal. Only the final check's verdict has advisories,
        # and they go on the Warning tab.
        text, advisories = (
            (
                "The cook can't start.\n\n" + format_refusals(stopped.verdict.refusals),
                format_advisories(stopped.verdict.advisories),
            )
            if isinstance(stopped, GraphRefusedError)
            else (str(stopped), "")
        )
        # Once there's a PDG graph, the tabs keep the text after the popup closes.
        if context is not None and not over_quota:
            _show_on_node_tabs(node, display, text, advisories)
        else:
            _show(text)
        return False
    counts: str = (
        f"Steps: {len(snapshot.steps)}, tasks: {snapshot.total_tasks}"
        if dependency_graph is not None
        else "Steps and tasks: counted when the cook starts"
    )
    # The listed inputs by kind, from the folders' own test, and the extra output folders.
    folders: int = sum(1 for path, _parm_path in inputs if os.path.isdir(path))
    attached: str = (
        f"Extra attachments: {len(inputs) - folders} input file(s), {folders} input folder(s), "
        f"{len(settings.output_directories)} output folder(s)"
        if inputs or settings.output_directories
        else "Extra attachments: none"
    )
    lines: List[str] = [counts, _describe_host_requirements(requirements), attached]
    details: str = format_advisories(verdict.advisories)
    confirmed: bool = _ask_to_confirm(node, settings, details, read, lines)
    if not confirmed:
        # The cook sends its own advisories to the Warning tab, so only a cancel sends them here.
        if details:
            _show_on_node_tabs(node, display, "", details)
        return False
    # The display node is what the network's own Cook button cooks. A stop still stored
    # from an earlier Submit would refuse this cook, so it goes first.
    submit_stops.pop(node.path(), None)
    display.cookWorkItems(block=False)
    return True


def _explain_unfinished_generate(
    graph_nodes: List[Any], infos: Tuple[NodeInfo, ...], cook_set: Optional[FrozenSet[str]]
) -> str:
    """Why the display node's generate may have stopped early, or ``""`` when it finished.

    PDG can set the errors after the generate returns, so empty steps stop Submit too. A step
    can also make no items with its settings, so that text names the steps and both causes.
    """
    # The nodes in this cook, by PDG name.
    nodes: Dict[str, Any] = {
        _name(node): node
        for node in graph_nodes
        if _name(node) and (cook_set is None or _name(node) in cook_set)
    }
    # A dynamic node has no items until it cooks, so its count says nothing here.
    dynamic: Set[str] = {info.name for info in infos if info.is_dynamic}
    empty: Set[str] = {
        name for name, node in nodes.items() if name not in dynamic and not _attr(node, "workItems")
    }
    steps: Set[str] = {
        name for name, node in nodes.items() if name not in dynamic and _is_step(node)
    }
    # Each node's empty ancestors, so an empty step below an empty node is found.
    empty_above: Dict[str, Set[str]] = find_ancestors_among(infos, empty)
    # Every step empty, or an empty step below an empty node, is a stop or steps that make nothing.
    stopped: bool = bool(steps) and (
        steps <= empty or any(empty_above.get(step) for step in steps & empty)
    )
    listed: str = ", ".join(name for name in nodes if name in steps & empty)
    # The node that stopped and each node below it keep an error, once PDG sets it.
    errored: bool = any(_call(_call(node, "topNode"), "errors") for node in nodes.values())
    text: str = (
        "The generate didn't finish, so Submit can't check the graph. "
        "Fix the node errors, then submit again."
        if errored
        else (
            f"These steps have no work items: {listed}. Either the generate didn't finish "
            "(check the node errors), or they make no items with these settings."
            if stopped
            else ""
        )
    )
    return text


def _find_cook_set(context: Any, display: Any) -> Optional[FrozenSet[str]]:
    """The PDG names of the display node and every node wired above it, or None when unknown.

    The wires are read as the cook reads them. A display node with no PDG node of its
    own, such as a subnet, starts from what it cooks, its inner display node, or else
    the nodes wired into it. None when none of those has a PDG node either, so no step
    is left out of the checks.
    """
    # PDG's name for each TOP node by path, and the wires as the readers find them.
    graph_nodes: List[Any] = _graph_nodes(context)
    by_path: Dict[str, str] = {
        _path_of(hom): name for name, hom in _top_nodes(graph_nodes).items() if hom
    }
    infos: Tuple[NodeInfo, ...] = readers.node_infos(graph_nodes, None)
    # The nearest PDG nodes the display node cooks, by the same rule as the readers' wires.
    starts: Set[str] = readers._upstream_of(display, by_path, set())
    # Each start and everything wired above it.
    ancestors: Dict[str, Set[str]] = find_ancestors_among(infos, {info.name for info in infos})
    return frozenset(starts.union(*(ancestors.get(start, set()) for start in starts))) or None


def _check_default_scheduler(node: Any, network: Any) -> None:
    """Raise ``CookSetupError`` unless the network's Default TOP Scheduler is ``node``."""
    # The network parm names its default scheduler, relative to the network.
    parm: Any = network.parm("topscheduler") if network is not None else None
    target: Any = network.node(parm.eval()) if parm is not None and parm.eval() else None
    if target is None or target.path() != node.path():
        raise CookSetupError(
            f"The TOP network's Default TOP Scheduler is not {node.path()}, so the network would "
            "cook on another scheduler. Set the network's Default TOP Scheduler to this node."
        )


def _show_on_node_tabs(node: Any, display: Any, error: str, advisories: str) -> None:
    """Show Submit's stop in a popup, when it has an error, and on the scheduler node's tabs.

    PDG keeps a message sent inside a cook, so after Submit's button callback returns, a cook
    start runs, and the scheduler's ``onStartCook`` sends the stop and refuses the cook.
    """
    import hdefereval  # type: ignore[import-not-found]

    # The node Houdini's Cook Output Node cooks: the network's Output node, else its display node.
    network: Any = node.parent()
    output: Any = (network.outputNode() if hasattr(network, "outputNode") else None) or display
    path: str = node.path()
    submit_stops[path] = (error, advisories)
    _logger.info("Routing Submit's stop through a cook start of %s", output.path())
    if error:
        _show(error)
    # A cook started inside the callback never reached onStartCook in the GUI, so it waits.
    hdefereval.executeDeferred(lambda: _start_cook_for_node_tabs(path, output))


def _start_cook_for_node_tabs(path: str, output: Any) -> None:
    """Start a cook of ``output`` the way Cook Output Node does, for the stop stored at ``path``."""
    failure: str = ""
    # Submit already handled saving, so a save prompt here would be a second dialog.
    try:
        output.cookWorkItems(block=False, generate_only=False, save_prompt=False, nodes=[output])
    except hou.OperationFailed as error:
        # PDG raises this when onStartCook refuses the cook, and for any other failed start.
        failure = str(error)
    finally:
        # onStartCook runs inside this call, so a stop still stored was never taken. Drop it, or
        # the artist's next real cook would be refused with it.
        if submit_stops.pop(path, None) is not None:
            _logger.warning(
                "The cook start of %s didn't reach %s's onStartCook (%s), so Submit's stop was "
                "dropped.",
                output.path(),
                path,
                failure or "no error",
            )


def _ask_to_confirm(
    node: Any,
    settings: SchedulerSettings,
    details: str,
    read: Callable[[str], str],
    extra: List[str],
) -> bool:
    """Show what will be submitted, with the advisories in ``details``, and return whether the artist chose Submit."""
    # The job as it will be submitted, then any warnings in the details.
    lines: List[str] = [
        f"Farm: {read('defaults.farm_id')}",
        f"Queue: {read('defaults.queue_id')}",
        f"Job name: {_parm(node, 'name', '', as_text=True) or os.path.basename(str(hou.hipFile.path()))}",
        f"Priority: {_parm(node, 'priority', '', as_text=True) or '50'}",
        f"Working directory: {settings.working_dir}",
        *extra,
    ]
    choice: int = hou.ui.displayMessage(
        "Submit this TOP network to Deadline Cloud?\n\n" + "\n".join(lines),
        title=_TITLE,
        buttons=_CONFIRM_BUTTONS,
        default_choice=0,
        close_choice=1,
        details=details or None,
    )
    return choice == 0


def _describe_host_requirements(requirements: Optional[Dict[str, Any]]) -> str:
    """The host requirements as one confirmation line, such as ``amount.worker.gpu min 1``."""
    if not requirements:
        return "Host requirements: any worker"
    # Each attribute with its values, then each amount with its bounds.
    parts: List[str] = [
        f"{entry['name']} {'any of' if 'anyOf' in entry else 'all of'} "
        + ", ".join(entry.get("anyOf") or entry.get("allOf") or ())
        for entry in requirements.get("attributes", ())
    ] + [
        " ".join(
            [entry["name"]] + [f"{end} {entry[end]}" for end in ("min", "max") if end in entry]
        )
        for entry in requirements.get("amounts", ())
    ]
    return "Host requirements: " + ", ".join(parts)


def show_error_popup(text: str) -> None:
    """Queue an error popup on the main thread, when a UI exists, and return at once.

    PDG's thread must never wait on a dialog, and with no UI nobody would read it.
    """
    if not hou.isUIAvailable():
        return
    import hdefereval  # type: ignore[import-not-found]

    hdefereval.executeDeferred(lambda: _show(text))


def _show(text: str) -> None:
    """Tell the artist, in an error dialog, why nothing was submitted."""
    hou.ui.displayMessage(text, title=_TITLE, severity=hou.severityType.Error)
