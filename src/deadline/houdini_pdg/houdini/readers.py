# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Read live PDG and Houdini objects into plain values, without importing either.

The readers take the objects by duck typing and tolerate any attribute that is
missing or raises, so unit tests drive them with small fakes.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from ..graph import DeclaredFile, NodeInfo, RawItem
from ..scene_reads import IGNORE_PARMS, IGNORE_VALUE_PREFIXES, OUTPUT_PARMS, SceneRead, SceneReads

_logger: logging.Logger = logging.getLogger(__name__)

# ``pdg.workItemCookType`` values, and the ``pdg_cooktype`` parm's, which use the
# same numbers. Generate (0) and Automatic (4) say nothing about where items cook.
_COOK_TYPES: Dict[int, str] = {1: "InProcess", 2: "OutOfProcess", 3: "Service"}

# The TOP parm that sends a node's items to a scheduler other than the network's.
# On the network itself it names the default scheduler.
_SCHEDULER_OVERRIDE_PARM: str = "topscheduler"

# ``pdg.workItemState`` names of items a cook will not schedule. By the cook's first
# ``onSchedule``, PDG has marked every item it will cook Waiting or Scheduled; the rest
# stay Uncooked, or keep an earlier cook's result. Any other state keeps the item.
_OUTSIDE_COOK_STATES: FrozenSet[str] = frozenset(
    {"Uncooked", "Dirty", "CookedSuccess", "CookedFail", "CookedCancel", "CookedCache"}
)

# The authored text of a parm that only copies another parm's value.
_CHANNEL_REFERENCES: Tuple[str, ...] = ("chs(", "chsraw(", "ch(")


def node_infos(
    graph_nodes: Iterable[Any],
    dependency_graph: Any,
    cook_set: Optional[Iterable[str]] = None,
    *,
    only_items_in_cook: bool = False,
) -> Tuple[NodeInfo, ...]:
    """Every TOP node, with its generated work items.

    Membership, wiring, and whether a node is a step come from the topology,
    because generation may not have reached every node yet. A node is a step when
    its ``scriptInfo.command`` is non-empty. ``dependency_graph`` is PDG's
    ``dependencyGraph(expand=True)``, and contributes only the items.

    With ``only_items_in_cook``, read during a cook, an item PDG will not schedule
    is left out, and a node whose every generated item is left out is outside the
    cook: cooking one work item cooks it and its upstream items, not their nodes.
    Outside a cook the item states are an earlier cook's, so every item is kept.
    """
    # The PDG nodes by name. The first of two nodes with one name wins, and a
    # nameless or unreadable node is skipped.
    pdg_nodes: Dict[str, Any] = {}
    for node in _iterate(graph_nodes):
        name: str = _str_attr(node, "name")
        if name and name not in pdg_nodes:
            pdg_nodes[name] = node

    # During a cook, drop the items PDG will not schedule. A node left with none
    # is outside the cook.
    items, item_upstream, attributes = _generated(dependency_graph)
    outside: Set[str] = set()
    if only_items_in_cook:
        for name in list(items):
            kept: List[RawItem] = [item for item in items[name] if item.in_cook is not False]
            if not kept:
                outside.add(name)
            items[name] = kept
    wiring: Optional[Dict[str, Set[str]]] = _hom_wiring(pdg_nodes)
    requested: Optional[Set[str]] = (
        None if cook_set is None else {str(name) for name in _iterate(cook_set)}
    )

    def in_cook(name: str) -> Optional[bool]:
        """Whether the node is in the cook, or None when the cook set is unknown."""
        if name in outside:
            return False
        # A node with items is in the cook whatever the set says: the set is taken
        # at the cook's start, before an HDA's inner nodes exist.
        return None if requested is None else (name in requested or bool(items.get(name)))

    # One NodeInfo per topology node. Its wiring comes from the TOP network, and
    # from the items' dependencies only when no network could be read.
    infos: List[NodeInfo] = []
    for name in sorted(pdg_nodes):
        pdg_node: Any = pdg_nodes[name]
        hom: Any = _call(pdg_node, "topNode")
        upstream: Set[str] = (
            wiring.get(name, set()) if wiring is not None else item_upstream.get(name, set())
        )
        infos.append(
            NodeInfo(
                name=name,
                is_dynamic=bool(_attr(pdg_node, "isDynamic", False)),
                top_type=_enum_name(_attr(pdg_node, "nodeType", "")),
                upstream=tuple(sorted(upstream - {name})),
                items=tuple(items.get(name, ())),
                exported_attribute_names=tuple(sorted(attributes.get(name, ()))),
                schedulable=_has_job_script(pdg_node),
                in_cook_set=in_cook(name),
                cook_type=_cook_type(pdg_node, hom),
                scheduler_override=_scheduler_override(hom),
                cache_mode=_cache_mode(hom),
                node_type_name=_node_type_name(hom),
            )
        )

    # Items whose node the topology does not list. It should not happen; keeping
    # them lets validation and the graph dump show it. Nothing is known of the
    # node itself, so whether it is a step is left to its items' commands.
    for name in sorted(set(items) - set(pdg_nodes)):
        infos.append(
            NodeInfo(
                name=name,
                is_dynamic=False,
                top_type="",
                upstream=tuple(sorted(item_upstream.get(name, set()) - {name})),
                items=tuple(items[name]),
                exported_attribute_names=tuple(sorted(attributes.get(name, ()))),
                schedulable=None,
                in_cook_set=in_cook(name),
            )
        )
    return tuple(infos)


def collect_scene_reads(
    top_nodes: Mapping[str, Any],
    max_nodes: int = 5_000,
    *,
    expand: Optional[Callable[[str], str]] = None,
) -> SceneReads:
    """The file-reference parms each step's scene reads, by step name.

    Follows node-reference parms and input wires. It enters children only inside
    the step's own subtree, never enters another TOP node, and stops after
    ``max_nodes`` nodes per step. ``expand`` evaluates Houdini variables in a
    read's static prefix, and raises for one it does not know. Without it, every
    static prefix is empty.
    """
    reads: List[SceneRead] = []
    walked: Dict[str, int] = {}
    failed: List[str] = []
    try:
        names: List[str] = sorted(top_nodes)
    except Exception:
        _logger.warning("Could not list the steps' TOP nodes for the scene walk.", exc_info=True)
        return SceneReads(available=False)
    # Walk each step on its own, so one that fails costs only its own reads.
    for step in names:
        try:
            top_node: Any = top_nodes[step]
            if top_node is None:
                continue
            # One read per file parm on every node the walk reached.
            reached: List[Any] = _walk(top_node, max_nodes)
            for node in reached:
                node_path: str = _path_of(node)
                node_name: str = _name_of(node)
                for parm, raw, value, owner in _file_parms(node, node_name):
                    reads.append(
                        SceneRead(
                            step=step,
                            node_path=node_path,
                            parm=parm,
                            raw=raw,
                            path=value,
                            is_output=parm in OUTPUT_PARMS,
                            static_prefix=_static_prefix(raw, owner, expand),
                        )
                    )
            walked[step] = len(reached)
        except Exception:
            failed.append(step)
            _logger.warning("The scene walk failed for step '%s'.", step, exc_info=True)
    return SceneReads(reads=tuple(reads), walked=walked, available=not failed)


def describe_work_item(work_item: Any, attribute_limit: int = 40) -> Dict[str, Any]:
    """A JSON-ready description of a work item, for the debug directory."""
    described: Dict[str, Any] = {}
    try:
        # Scalars, skipping any that are missing or are methods.
        for pdg_name, key in _SCALARS:
            value: Any = _attr(work_item, pdg_name, None)
            if value is None or callable(value):
                continue
            described[key] = value if isinstance(value, (bool, int, float)) else str(value)
        node_name: str = _str_attr(_attr(work_item, "node", None), "name")
        if node_name:
            described["node_name"] = node_name
        state: Any = _attr(work_item, "state", None)
        if state is not None:
            described["state"] = str(state)
        for pdg_name, key in _FILE_LISTS:
            described[key] = _file_entries(_attr(work_item, pdg_name, None))
        # Attribute names, capped so a large item cannot flood the debug directory.
        try:
            attribute_names: List[str] = sorted(str(name) for name in work_item.attribNames())
            described["attribute_names"] = attribute_names[: max(0, int(attribute_limit))]
            described["attribute_count"] = len(attribute_names)
        except Exception:
            # An item whose attribute names cannot be read is described without them.
            pass
        try:
            environment: Any = work_item.environment
            if environment is not None:
                described["environment"] = {str(k): str(v) for k, v in dict(environment).items()}
        except Exception:
            # An item whose environment cannot be read or converted is described without it.
            pass
        # Neighbouring items by name. PDG exposes them as lists; anything that
        # cannot be iterated, such as a method, is skipped.
        for pdg_name in ("dependencies", "dependents"):
            neighbours: Any = _attr(work_item, pdg_name, None)
            try:
                described[pdg_name] = sorted(str(_attr(dep, "name", dep)) for dep in neighbours)
            except Exception:
                continue
    except Exception:
        _logger.debug("Could not describe a work item fully.", exc_info=True)
    return described


# -- work items ---------------------------------------------------------------

# ``(WorkItem attribute, key in the description)`` for the scalar fields.
_SCALARS: Tuple[Tuple[str, str], ...] = (
    ("id", "id"),
    ("index", "index"),
    ("name", "name"),
    ("command", "command"),
    ("frame", "frame"),
    ("priority", "priority"),
    ("isBatch", "is_batch"),
    ("isInProcess", "is_in_process"),
    ("isOutOfProcess", "is_out_of_process"),
    ("cookType", "cook_type"),
    ("outputDir", "output_dir"),
    ("batchIndex", "batch_index"),
    ("batchSize", "batch_size"),
)

# ``(WorkItem attribute, key in the description)`` for the file lists.
_FILE_LISTS: Tuple[Tuple[str, str], ...] = (
    ("inputFiles", "input_files"),
    ("expectedInputFiles", "expected_input_files"),
    ("outputFiles", "output_files"),
    ("expectedOutputFiles", "expected_output_files"),
)


def _file_entries(files: Any) -> List[Dict[str, str]]:
    """Each file's tokenized ``path`` and tag, plus ``local_path`` when it differs."""
    entries: List[Dict[str, str]] = []
    for entry in _iterate(files):
        # A file with no path is skipped; the local path is kept only when it adds something.
        path: Any = _attr(entry, "path", None)
        if path is None:
            continue
        record: Dict[str, str] = {"path": str(path), "tag": str(_attr(entry, "tag", "") or "")}
        local: Any = _attr(entry, "local_path", None)
        if local is not None and str(local) != str(path):
            record["local_path"] = str(local)
        entries.append(record)
    return entries


def _generated(
    dependency_graph: Any,
) -> Tuple[Dict[str, List[RawItem]], Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Items, item-derived upstream nodes, and exported attribute names, by node name.

    ``dependencyGraph`` returns ``(dependencies, dependents, ready)``. Only the
    first is read: it maps each schedulable item to the items it waits on.
    """
    items: Dict[str, List[RawItem]] = {}
    upstream: Dict[str, Set[str]] = {}
    attributes: Dict[str, Set[str]] = {}
    # Each readable item joins its node's list, and its dependencies on other
    # nodes' items give that node's item-derived upstream.
    for work_item, depends_on in _dependency_map(dependency_graph):
        node_name: str = _str_attr(_attr(work_item, "node", None), "name")
        item: Optional[RawItem] = _raw_item(work_item, depends_on)
        if not node_name or item is None:
            continue
        # An item on its own node's dependency adds no edge.
        items.setdefault(node_name, []).append(item)
        attributes.setdefault(node_name, set()).update(_exported_attributes(work_item))
        edges: Set[str] = upstream.setdefault(node_name, set())
        for dep in _iterate(depends_on):
            dep_node: str = _str_attr(_attr(dep, "node", None), "name")
            if dep_node and dep_node != node_name:
                edges.add(dep_node)
    return items, upstream, attributes


def _dependency_map(dependency_graph: Any) -> List[Tuple[Any, Any]]:
    """``(item, items it waits on)`` pairs from PDG's dependency graph, or a bare mapping."""
    is_triple: bool = (
        not isinstance(dependency_graph, Mapping)
        and isinstance(dependency_graph, (tuple, list))
        and bool(dependency_graph)
    )
    graph: Any = dependency_graph[0] if is_triple else dependency_graph
    try:
        return list(graph.items()) if graph is not None else []
    except Exception:
        _logger.warning("Could not read PDG's dependency graph.", exc_info=True)
        return []


def _raw_item(work_item: Any, depends_on: Any) -> Optional[RawItem]:
    """One work item as a value, or None when it has no name."""
    name: str = _str_attr(work_item, "name")
    if not name:
        return None
    # Read batching from the child: the dependency graph's items are batch children,
    # so PDG's ``isBatch`` is False on every one of them.
    parent: Any = _attr(work_item, "batchParent", None)
    return RawItem(
        name=name,
        index=_as_int(_attr(work_item, "index", 0), 0),
        item_id=_as_int(_attr(work_item, "id", -1), -1),
        command=str(_attr(work_item, "command", "") or ""),
        is_out_of_process=bool(_attr(work_item, "isOutOfProcess", False)),
        is_batch=bool(_attr(work_item, "isBatch", False)),
        batch_parent=_str_attr(parent, "name") if parent is not None else "",
        declared_outputs=_declared(_attr(work_item, "expectedOutputFiles", ())),
        declared_inputs=_declared(_attr(work_item, "inputFiles", ())),
        depends_on=tuple(n for n in (_str_attr(d, "name") for d in _iterate(depends_on)) if n),
        in_cook=_in_cook(_attr(work_item, "state", None)),
    )


def _in_cook(state: Any) -> Optional[bool]:
    """False for a state the cook will not schedule, None when the state is missing or blank."""
    if state is None:
        return None
    # Read by name, so the state needs no ``pdg`` import.
    name: str = _enum_name(state)
    if name in _OUTSIDE_COOK_STATES:
        return False
    return True if name else None


def _declared(files: Any) -> Tuple[DeclaredFile, ...]:
    """Declared files with a path. PDG gives file objects; a ``(path, tag)`` pair is read too."""
    declared: List[DeclaredFile] = []
    for entry in _iterate(files):
        path: Any = _attr(entry, "path", None)
        tag: Any = _attr(entry, "tag", "")
        if path is None and isinstance(entry, (tuple, list)) and entry:
            path, tag = entry[0], entry[1] if len(entry) > 1 else ""
        if path:
            declared.append(DeclaredFile(path=str(path), tag=str(tag or "")))
    return tuple(declared)


def _exported_attributes(work_item: Any) -> Tuple[str, ...]:
    """The attributes PDG exports to a task's environment.

    ``WorkItem.environment`` is exactly the ``EnvExport`` attributes minus the
    ``Internal`` ones, so reading it delegates the flag rules to PDG.
    """
    try:
        return tuple(str(name) for name in work_item.environment.keys())
    except Exception:
        return ()


# -- nodes --------------------------------------------------------------------


def _has_job_script(node: Any) -> bool:
    """Whether the node runs a job script on a worker. An unreadable node is not a step."""
    try:
        return bool(node.scriptInfo.command)
    except Exception:
        return False


def _cook_type(node: Any, hom: Any) -> Optional[str]:
    """Where the node's items cook: PDG's own answer first, then the node's parm."""
    value: Optional[str] = _cook_type_name(_attr(node, "cookType", None))
    if value:
        return value
    # The parm's menu uses PDG's numbers. A missing or unreadable parm is unknown.
    parm: Any = _call(hom, "parm", "pdg_cooktype")
    if parm is None:
        return None
    try:
        return _COOK_TYPES.get(int(parm.eval()))
    except Exception:
        return None


def _cook_type_name(value: Any) -> Optional[str]:
    """A cook type given as a number or as a PDG enum, or None for Generate, Automatic, or unknown."""
    if value is None:
        return None
    try:
        return _COOK_TYPES.get(int(value))
    except Exception:
        name: str = _enum_name(value)
        return name if name in _COOK_TYPES.values() else None


def _cache_mode(hom: Any) -> Optional[str]:
    """The Cache Mode menu label. The tokens are PDG enum values, so the label is the stable name."""
    parm: Any = _call(hom, "parm", "pdg_cachemode")
    if parm is None:
        return None
    try:
        return str(parm.menuLabels()[list(parm.menuItems()).index(parm.evalAsString())])
    except Exception:
        return None


def _scheduler_override(hom: Any) -> str:
    """The scheduler the node's ``topscheduler`` parm names, or ``""``."""
    parm: Any = _call(hom, "parm", _SCHEDULER_OVERRIDE_PARM)
    if parm is None:
        return ""
    try:
        return str(parm.eval() or "")
    except Exception:
        return ""


def _node_type_name(hom: Any) -> str:
    """The Houdini node type name, such as ``houdiniserver``, or ``""`` when it cannot be read."""
    try:
        return str(hom.type().name() or "")
    except Exception:
        return ""


def _hom_wiring(pdg_nodes: Mapping[str, Any]) -> Optional[Dict[str, Set[str]]]:
    """Each PDG node's nearest upstream PDG nodes, from the TOP network's wires.

    PDG's own input ports carry no connections, so the wiring is read from the
    Houdini nodes. A PDG node can map to two of them, a wrapper subnet and the
    node inside it, so every candidate's inputs are unioned. None when no TOP
    network could be found.
    """
    # The TOP networks that hold the nodes, by path.
    networks: Dict[str, Any] = {}
    for node in pdg_nodes.values():
        network: Any = _network_of(_call(node, "topNode"))
        path: str = _path_of(network)
        if path and path not in networks:
            networks[path] = network
    if not networks:
        return None

    # Every Houdini node in those networks that maps to one of the PDG nodes.
    hom_for: Dict[str, List[Any]] = {}
    path_to_pdg: Dict[str, str] = {}
    for network in networks.values():
        for hom in _iterate(_call(network, "allSubChildren")):
            pdg_name: str = _str_attr(_call(hom, "getPDGNode"), "name")
            hom_path: str = _path_of(hom)
            if pdg_name in pdg_nodes and hom_path:
                hom_for.setdefault(pdg_name, []).append(hom)
                path_to_pdg[hom_path] = pdg_name

    # Union what each of a PDG node's Houdini nodes is wired to.
    wiring: Dict[str, Set[str]] = {}
    for name in pdg_nodes:
        upstream: Set[str] = set()
        for hom in hom_for.get(name, ()):
            for source in _iterate(_call(hom, "inputs")):
                upstream |= _upstream_of(source, path_to_pdg, {_path_of(hom)})
        wiring[name] = upstream
    return wiring


def _network_of(hom: Any) -> Any:
    """The outermost TOP network holding ``hom``. A node may sit inside a subnet."""
    return _outermost_top_network(_call(hom, "parent"))


def _outermost_top_network(network: Any) -> Any:
    """``network``, or the outermost network above it that still holds TOP nodes."""
    if network is None:
        return None
    # The parent holds TOP nodes when its children are in the TOP category.
    above: Any = _call(network, "parent")
    if str(_call(_call(above, "childTypeCategory"), "name")) == "Top":
        return _outermost_top_network(above)
    return network


def _upstream_of(source: Any, path_to_pdg: Mapping[str, str], seen: Set[str]) -> Set[str]:
    """The nearest PDG nodes upstream of one input, through subnets and node-less TOP nodes."""
    if source is None:
        return set()
    # A subnet's indirect input has an ``inputIndex`` and no ``inputs``. The
    # attributes are read through ``_attr``, because ``hasattr`` lets any error
    # other than ``AttributeError`` escape.
    if _attr(source, "inputs", None) is None and _attr(source, "inputIndex", None) is not None:
        # Continue from the subnet's own input at that index.
        index: Any = _call(source, "inputIndex")
        external: List[Any] = list(_iterate(_call(_call(source, "parent"), "inputs")))
        if isinstance(index, int) and 0 <= index < len(external):
            return _upstream_of(external[index], path_to_pdg, seen)
        return set()

    # Each Houdini node is visited once, so cyclic wiring terminates.
    path: str = _path_of(source)
    if not path or path in seen:
        return set()
    seen.add(path)
    if path in path_to_pdg:
        return {path_to_pdg[path]}

    # A subnet with no PDG node of its own: continue from its display node.
    display: Any = _call(source, "displayNode")
    if display is not None:
        return _upstream_of(display, path_to_pdg, seen)

    # A TOP node with no PDG node of its own: walk through it.
    if _is_top(source):
        found: Set[str] = set()
        for upstream in _iterate(_call(source, "inputs")):
            found |= _upstream_of(upstream, path_to_pdg, seen)
        return found
    return set()


# -- the scene walk -----------------------------------------------------------


def _walk(top_node: Any, max_nodes: int) -> List[Any]:
    """Every scene node whose parms a step's tasks evaluate, the TOP node first.

    Following TOP-to-TOP wires would make every downstream step reach the whole
    scene, and those edges are the projection's anyway. A TOP node's own subtree
    is entered, because a wrapper's reads live in its children. A ``max_nodes``
    below 1 walks no node.
    """
    root: str = _path_of(top_node)
    prefix: str = root + "/"
    seen: Set[str] = set()
    order: List[Any] = []
    stack: List[Any] = [top_node]
    # Depth-first, each path once, until the stack is empty or the ceiling is hit.
    while stack and len(order) < max_nodes:
        node: Any = stack.pop()
        path: str = _path_of(node)
        if not path or path in seen:
            continue
        # A TOP node outside the step's subtree belongs to another step.
        inside: bool = path == root or path.startswith(prefix)
        if not inside and _is_top(node):
            continue
        # Children are entered only inside the subtree; references and inputs always.
        seen.add(path)
        order.append(node)
        if inside:
            stack.extend(_iterate(_call(node, "children")))
        stack.extend(_node_references(node))
        stack.extend(_iterate(_call(node, "inputAncestors")))
    if stack and len(order) >= max_nodes:
        _logger.warning(
            "The scene walk from '%s' stopped at %d nodes; its reads may be incomplete.",
            root,
            max_nodes,
        )
    return order


def _string_parms(node: Any, kind: str) -> List[Any]:
    """String parms of one ``stringParmType``, compared by name so ``hou`` is not needed."""
    found: List[Any] = []
    for parm in _iterate(_call(node, "parms")):
        try:
            template: Any = parm.parmTemplate()
            if str(template.type().name()) == "String" and (
                str(template.stringType().name()) == kind
            ):
                found.append(parm)
        except Exception:
            continue
    return found


# ``$OS`` and ``${OS}``: the name of the node that holds the parm.
_OS_VARIABLE: "re.Pattern[str]" = re.compile(r"\$\{OS\}|\$OS(?!\w)")


def _static_prefix(raw: str, owner: str, expand: Optional[Callable[[str], str]]) -> str:
    """The evaluated text of ``raw`` before its first expression or ``@`` attribute.

    ``owner`` is the name of the node that holds the parm ``raw`` was authored on,
    which ``$OS`` names. ``""`` when ``raw`` has no expression or attribute, or when
    the prefix cannot be evaluated.
    """
    cuts: List[int] = [i for i in (raw.find("`"), raw.find("@")) if i >= 0]
    if not cuts or expand is None:
        return ""
    static: str = raw[: min(cuts)]
    if not static.strip():
        return ""
    # ``$OS`` is substituted here, because the expander does not know which node
    # the text belongs to. Without the node's name the prefix cannot be evaluated.
    if _OS_VARIABLE.search(static) and not owner:
        return ""
    named: str = _OS_VARIABLE.sub(lambda _: owner, static)
    try:
        return str(expand(named))
    except Exception:
        return ""


def _name_of(node: Any) -> str:
    """The node's name, or the last component of its path when ``name()`` fails."""
    return str(_call(node, "name") or "") or _path_of(node).rstrip("/").split("/")[-1]


def _file_parms(node: Any, node_name: str) -> List[Tuple[str, str, str, str]]:
    """``(name, authored, evaluated, owner)`` for each file reference that names a file.

    ``owner`` is the name of the node that holds the authored text: ``node_name``,
    or the node of the parm a channel reference copies.
    """
    found: List[Tuple[str, str, str, str]] = []
    for parm in _string_parms(node, "FileReference"):
        try:
            name: str = str(parm.name())
            if name in IGNORE_PARMS:
                continue
            value: str = str(parm.eval())
        except Exception:
            continue
        if not value or value.startswith(IGNORE_VALUE_PREFIXES):
            continue
        raw, owner = _authored(parm, value, node_name)
        found.append((name, raw, value, owner))
    return found


def _authored(parm: Any, evaluated: str, node_name: str) -> Tuple[str, str]:
    """The parm as authored, and the name of the node that holds that text.

    A channel reference is followed to the parm it copies: a ROP inside a ROP Fetch
    wrapper links its output parm to the wrapper's with ``chs(...)``, and the
    portability check must judge the wrapper's value. Houdini evaluates that text
    in the wrapper, so the wrapper's name is the owner. With no authored text, the
    evaluated value stands in for it.
    """
    # HOM returns the parm itself when it references no other parm, which then
    # gives the same text twice.
    referenced: Any = _call(parm, "getReferencedParm")
    candidates: List[Tuple[Any, str]] = [(parm, node_name)]
    if referenced is not None:
        candidates.append((referenced, _name_of(_call(referenced, "node"))))
    # The first candidate whose text is authored, not a copy of another parm.
    for candidate, owner in candidates:
        raw: str = _authored_text(candidate)
        if raw and not raw.startswith(_CHANNEL_REFERENCES):
            return raw, owner
    return evaluated, node_name


def _authored_text(parm: Any) -> str:
    """The parm's text with its tokens intact, or ``""`` when it cannot be read."""
    try:
        return str(parm.unexpandedString())
    except Exception:
        pass
    # Keyframed or expression-driven parms refuse ``unexpandedString``.
    try:
        return str(parm.rawValue())
    except Exception:
        return ""


def _node_references(node: Any) -> List[Any]:
    """Nodes reached through ``NodeReference`` parms, such as ``roppath`` and ``soppath``."""
    targets: List[Any] = []
    for parm in _string_parms(node, "NodeReference"):
        try:
            value: str = str(parm.eval())
        except Exception:
            continue
        # ``node.node`` resolves relative and absolute references alike.
        target: Any = _call(node, "node", value) if value else None
        if target is not None:
            targets.append(target)
    return targets


def _is_top(node: Any) -> bool:
    """Whether the node is in the TOP category. Unreadable means no, so the walk goes on."""
    try:
        return str(node.type().category().name()) == "Top"
    except Exception:
        return False


# -- duck typing --------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any) -> Any:
    """``obj.name``, or ``default`` when it is missing or raises."""
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _str_attr(obj: Any, name: str) -> str:
    """``obj.name`` as text, or ``""`` when it is missing, empty, or raises."""
    value: Any = _attr(obj, name, "")
    return str(value) if value else ""


def _call(obj: Any, name: str, *args: Any) -> Any:
    """``obj.name(*args)``, or None when it is missing or raises."""
    try:
        return getattr(obj, name)(*args)
    except Exception:
        return None


def _iterate(values: Any) -> List[Any]:
    """The values as a list, or ``[]`` when they are None or cannot be iterated."""
    try:
        return list(values or ())
    except Exception:
        return []


def _path_of(node: Any) -> str:
    """The node's path, or ``""`` when it has none."""
    value: Any = _call(node, "path")
    return str(value) if value else ""


def _as_int(value: Any, default: int) -> int:
    """``int(value)``, or ``default`` when it is None or not a number."""
    # Not ``value or default``: an index of 0 is valid.
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _enum_name(value: Any) -> str:
    """``nodeType.Processor`` and similar PDG enums, as ``Processor``."""
    # An enum with a ``name`` gives it; otherwise the printed form's last part.
    name: Any = _attr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    text: str = str(value or "")
    return text.split(".")[-1]
