# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for reading PDG and Houdini objects into the graph's value types.

The readers take live objects by duck typing, so these tests drive them with small
fakes that offer only the attributes and methods the readers call. No test imports
``hou`` or ``pdg``, and none patches a module.
"""

from __future__ import annotations

# Standard library, pytest and hypothesis, and the public surface of ``graph`` and
# ``houdini.readers`` only.
import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deadline.houdini_pdg.graph import DeclaredFile, GraphSnapshot, NodeInfo, RawItem, synthesize
from deadline.houdini_pdg.houdini.readers import (
    collect_scene_reads,
    describe_work_item,
    node_infos,
)
from deadline.houdini_pdg.scene_reads import SceneReads

# -- HOM fakes ----------------------------------------------------------------


class _Name:
    """Anything HOM exposes through ``name()``, such as a node type category."""

    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Type:
    """A ``hou.NodeType``: its category and its type name."""

    def __init__(self, category: str, name: str = "node") -> None:
        self._category = _Name(category)
        self._name = name

    def category(self) -> _Name:
        return self._category

    def name(self) -> str:
        return self._name


class _Template:
    """A ``hou.ParmTemplate``: its type and, for strings, its string type."""

    def __init__(self, kind: str, string_kind: str = "") -> None:
        self._type = _Name(kind)
        self._string = _Name(string_kind)

    def type(self) -> _Name:
        return self._type

    def stringType(self) -> _Name:
        return self._string


class _Parm:
    """A ``hou.Parm`` with an evaluated value and an authored one.

    ``referenced`` is the parm a channel reference copies, and ``owner`` is the
    node ``node()`` returns.
    """

    def __init__(
        self,
        name: str,
        value: Any = "",
        raw: Optional[str] = None,
        *,
        kind: str = "FileReference",
        template: str = "String",
        raises: bool = False,
        referenced: Optional["_Parm"] = None,
        node_path: str = "/obj/n",
        unexpanded_raises: bool = False,
        owner: Any = None,
    ) -> None:
        # Keep what each HOM method returns.
        self._name = name
        self._value = value
        self._raw = raw if raw is not None else str(value)
        self._template = _Template(template, kind)
        self._raises = raises
        self._referenced = referenced
        self._path = f"{node_path}/{name}"
        self._unexpanded_raises = unexpanded_raises
        self._owner = owner

    def name(self) -> str:
        return self._name

    def path(self) -> str:
        return self._path

    def node(self) -> Any:
        return self._owner

    def parmTemplate(self) -> _Template:
        return self._template

    def eval(self) -> Any:
        if self._raises:
            raise RuntimeError("eval failed")
        return self._value

    def unexpandedString(self) -> str:
        if self._unexpanded_raises:
            raise RuntimeError("keyframed")
        return self._raw

    def rawValue(self) -> str:
        return self._raw

    def getReferencedParm(self) -> "_Parm":
        # HOM returns the parm itself when it references nothing.
        return self._referenced or self


class _Hom:
    """A ``hou.Node``: its wires, children, parms, and the PDG node it maps to."""

    def __init__(
        self,
        path: str,
        *,
        category: str = "Sop",
        child_category: Optional[str] = None,
        parms: Sequence[Any] = (),
        inputs: Sequence[Any] = (),
        children: Sequence["_Hom"] = (),
        ancestors: Sequence["_Hom"] = (),
        lookup: Optional[Dict[str, "_Hom"]] = None,
        pdg: Any = None,
        display: Optional["_Hom"] = None,
        parent: Optional["_Hom"] = None,
        type_name: str = "node",
    ) -> None:
        # Keep the wiring and parms as plain lists, so a test can rewire a node.
        self._path = path
        self._type = _Type(category, type_name)
        self._child_category = _Name(child_category) if child_category else None
        self.parm_list: List[Any] = list(parms)
        self.input_list: List[Any] = list(inputs)
        self.child_list: List[_Hom] = list(children)
        self.ancestor_list: List[_Hom] = list(ancestors)
        self.lookup: Dict[str, _Hom] = dict(lookup or {})
        self.pdg = pdg
        self.display = display
        self.parent_node = parent

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _Type:
        return self._type

    def parms(self) -> List[Any]:
        return list(self.parm_list)

    def parm(self, name: str) -> Any:
        return next((p for p in self.parm_list if p.name() == name), None)

    def inputs(self) -> List[Any]:
        return list(self.input_list)

    def children(self) -> List["_Hom"]:
        return list(self.child_list)

    def allSubChildren(self) -> List["_Hom"]:
        found: List[_Hom] = []
        for child in self.child_list:
            found.append(child)
            found.extend(child.allSubChildren())
        return found

    def inputAncestors(self) -> List["_Hom"]:
        return list(self.ancestor_list)

    def node(self, ref: str) -> Optional["_Hom"]:
        return self.lookup.get(ref)

    def getPDGNode(self) -> Any:
        return self.pdg

    def displayNode(self) -> Optional["_Hom"]:
        return self.display

    def parent(self) -> Optional["_Hom"]:
        return self.parent_node

    def childTypeCategory(self) -> Optional[_Name]:
        return self._child_category

    def inputIndex(self, input_name: str) -> int:
        # ``hou.Node`` has this too, so it cannot tell a node from a subnet's
        # indirect input by itself.
        return 0


class _IndirectInput:
    """A subnet's indirect input: ``parent`` and ``inputIndex``, no ``inputs``."""

    def __init__(self, parent: Any, index: int) -> None:
        self._parent = parent
        self._index = index

    def parent(self) -> Any:
        return self._parent

    def inputIndex(self) -> int:
        return self._index


# -- PDG fakes ----------------------------------------------------------------


class _ScriptInfo:
    """A PDG node's job script; an empty command means the node runs nothing on a worker."""

    def __init__(self, command: str) -> None:
        self.command = command


class _Enum:
    """A PDG enum value, which prints as ``enumType.Name`` and has no ``name``."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


class _PdgNode:
    """A ``pdg.Node``, linked to its TOP node by :func:`_wire`."""

    def __init__(
        self,
        name: str,
        *,
        command: str = "",
        node_type: Any = "nodeType.Processor",
        dynamic: bool = False,
        cook_type: Any = None,
    ) -> None:
        # The attributes the readers read from a PDG node.
        self.name = name
        self.scriptInfo = _ScriptInfo(command)
        self.nodeType = _Enum(node_type) if isinstance(node_type, str) else node_type
        self.isDynamic = dynamic
        self.cookType = cook_type
        self.hom: Any = None

    def topNode(self) -> Any:
        return self.hom


class _File:
    """A ``pdg.File``, with ``local_path`` only when one is given."""

    def __init__(self, path: str, tag: str = "file/geo", local_path: Optional[str] = None) -> None:
        self.path = path
        self.tag = tag
        if local_path is not None:
            self.local_path = local_path


class _WorkItem:
    """A ``pdg.WorkItem`` named ``<node>_<index>``; any extra keyword becomes an attribute."""

    def __init__(self, node: _PdgNode, index: int, **kwargs: Any) -> None:
        # The attributes the readers read from a work item; the rest come from keywords.
        self.node = node
        self.index = index
        self.id = 1000 + index
        self.name = f"{node.name}_{index}"
        self.command = kwargs.pop("command", "hython job.py")
        self.isOutOfProcess = bool(self.command)
        self.isBatch = False
        self.batchParent = kwargs.pop("batch_parent", None)
        self.expectedOutputFiles = kwargs.pop("outputs", [])
        self.inputFiles = kwargs.pop("inputs", [])
        self.environment = dict(kwargs.pop("environment", {}))
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Raises:
    """An object whose every attribute raises, like a deleted node.

    It raises ``RuntimeError`` on purpose, not ``AttributeError``: Houdini raises
    ``hou.ObjectWasDeleted``, which is not an ``AttributeError``, so ``getattr``
    with a default does not absorb it, and the readers must survive it anyway.
    """

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(name)


def _topnet(*children: _Hom) -> _Hom:
    """Put ``children`` in ``/obj/topnet1``, a TOP network inside ``/obj``."""
    # Parent each child to the network, as Houdini does.
    obj: _Hom = _Hom("/obj", category="Object", child_category="Object")
    net: _Hom = _Hom(
        "/obj/topnet1", category="Object", child_category="Top", parent=obj, children=children
    )
    for child in children:
        child.parent_node = net
    return net


def _wire(pdg: _PdgNode, path: str, inputs: Sequence[Any] = (), **kwargs: Any) -> _Hom:
    """A TOP node for ``pdg``, wired to ``inputs``."""
    hom: _Hom = _Hom(path, category="Top", pdg=pdg, inputs=inputs, **kwargs)
    pdg.hom = hom
    return hom


def _by_name(infos: Sequence[NodeInfo]) -> Dict[str, NodeInfo]:
    """The node infos by node name."""
    return {info.name: info for info in infos}


# -- node_infos ---------------------------------------------------------------


def _pyro_like() -> List[_PdgNode]:
    """Wedge, then a cache, a partitioner, and a render: two steps around two non-steps."""
    # The PDG nodes, then their TOP nodes, wired in a chain.
    wedge: _PdgNode = _PdgNode("wedge1")
    cache: _PdgNode = _PdgNode(
        "cache", command="hython rop.py", cook_type=_Enum("workItemCookType.OutOfProcess")
    )
    part: _PdgNode = _PdgNode("partition1", node_type="nodeType.Partitioner")
    render: _PdgNode = _PdgNode("render", command="hython rop.py", cook_type=2)
    h_wedge: _Hom = _wire(wedge, "/obj/topnet1/wedge1", type_name="wedge")
    h_cache: _Hom = _wire(
        cache,
        "/obj/topnet1/cache",
        [h_wedge],
        parms=[_Parm("pdg_cooktype", 1, kind="", template="Int")],
        type_name="ropfetch",
    )
    h_part: _Hom = _wire(part, "/obj/topnet1/partition1", [h_cache])
    h_render: _Hom = _wire(
        render,
        "/obj/topnet1/render",
        [h_part],
        parms=[_Parm("topscheduler", "/obj/topnet1/local", kind="NodeReference")],
    )
    _topnet(h_wedge, h_cache, h_part, h_render)
    return [wedge, cache, part, render]


def test_topology_gives_membership_wiring_and_classification_before_any_items() -> None:
    """Before generation, the topology alone gives every node, its wires, and which are steps.

    This is what keeps a cook that starts before PDG has generated every item from
    submitting a job with steps missing.
    """
    infos: Dict[str, NodeInfo] = _by_name(node_infos(_pyro_like(), ({}, {}, [])))
    assert set(infos) == {"wedge1", "cache", "partition1", "render"}
    assert infos["cache"].upstream == ("wedge1",)
    assert infos["render"].upstream == ("partition1",)
    assert [infos[n].schedulable for n in ("wedge1", "cache", "partition1", "render")] == [
        False,
        True,
        False,
        True,
    ]
    assert all(info.items == () for info in infos.values())
    # Both steps survive synthesis as steps that have no items yet.
    snapshot: GraphSnapshot = synthesize(list(infos.values()))
    assert snapshot.steps_without_items == ("cache", "render")


def test_node_type_cook_type_and_scheduler_override_are_read() -> None:
    """PDG's node type, where items cook, and a scheduler override are read from each node.

    Validation refuses in-process steps and nodes sent to another scheduler, so a
    misread here would let either through.
    """
    infos: Dict[str, NodeInfo] = _by_name(node_infos(_pyro_like(), {}))
    assert infos["wedge1"].top_type == "Processor"
    assert infos["partition1"].top_type == "Partitioner"
    # PDG's enum and PDG's number both name the cook type.
    assert infos["cache"].cook_type == "OutOfProcess"
    assert infos["render"].cook_type == "OutOfProcess"
    assert infos["wedge1"].cook_type is None
    assert infos["render"].scheduler_override == "/obj/topnet1/local"
    assert infos["cache"].scheduler_override == ""


def test_the_houdini_node_type_name_is_read_from_the_top_node() -> None:
    """Each node carries its Houdini type name, ``""`` when it cannot be read.

    PDG's own node type cannot tell a command server from a renderer, so this name
    is the only way to recognize one.
    """
    nodes: List[_PdgNode] = _pyro_like()
    server: _PdgNode = _PdgNode("houdiniserver_begin1", command="x")
    _wire(server, "/obj/topnet1/houdiniserver_begin1", type_name="houdiniserver")
    loose: _PdgNode = _PdgNode("loose", command="x")
    broken: _PdgNode = _PdgNode("broken", command="x")
    broken.hom = _Raises()
    infos: Dict[str, NodeInfo] = _by_name(node_infos(nodes + [server, loose, broken], {}))
    assert infos["houdiniserver_begin1"].node_type_name == "houdiniserver"
    assert infos["cache"].node_type_name == "ropfetch"
    assert infos["wedge1"].node_type_name == "wedge"
    # No TOP node, or one whose type cannot be read.
    assert infos["loose"].node_type_name == ""
    assert infos["broken"].node_type_name == ""


def test_cook_type_falls_back_to_the_parm() -> None:
    """When PDG gives no cook type, or Automatic, the node's ``pdg_cooktype`` parm decides.

    Without the fallback, a service or in-process node would read as unknown and
    escape validation's checks.
    """
    node: _PdgNode = _PdgNode("n", command="x")
    hom: _Hom = _wire(
        node, "/obj/topnet1/n", parms=[_Parm("pdg_cooktype", 3, kind="", template="Int")]
    )
    _topnet(hom)
    (info,) = node_infos([node], {})
    assert info.cook_type == "Service"
    # Automatic says nothing, so the parm still decides.
    node.cookType = _Enum("workItemCookType.Automatic")
    (info,) = node_infos([node], {})
    assert info.cook_type == "Service"
    # A parm value that is not a cook type, or one that cannot be read, is unknown.
    hom.parm_list = [_Parm("pdg_cooktype", 4, kind="", template="Int")]
    (info,) = node_infos([node], {})
    assert info.cook_type is None
    hom.parm_list = [_Parm("pdg_cooktype", "bad", kind="", template="Int", raises=True)]
    (info,) = node_infos([node], {})
    assert info.cook_type is None
    # With no parm at all, the cook type is unknown too.
    hom.parm_list = []
    (info,) = node_infos([node], {})
    assert info.cook_type is None


def test_a_cook_type_given_by_name_is_read() -> None:
    """A cook type PDG gives as an enum with a ``name`` is read by that name, if it is one."""

    class Named:
        """An enum value whose ``name`` attribute holds the name."""

        def __init__(self, name: str) -> None:
            self.name = name

    node: _PdgNode = _PdgNode("n", command="x", cook_type=Named("InProcess"))
    (info,) = node_infos([node], {})
    assert info.cook_type == "InProcess"
    # A name that is not a cook type is unknown, not passed through.
    node.cookType = Named("Generate")
    (info,) = node_infos([node], {})
    assert info.cook_type is None


def test_enum_values_with_a_name_attribute_are_read_by_name() -> None:
    """PDG enums with a ``name`` give that name, not their printed form."""

    class Named:
        """An enum value whose ``name`` attribute holds the name."""

        name: str = "Mapper"

    node: _PdgNode = _PdgNode("m", node_type=Named())
    (info,) = node_infos([node], {})
    assert info.top_type == "Mapper"

    class Method:
        """An enum value whose ``name`` is a method, so its printed form is read instead."""

        def name(self) -> str:
            return "Wrong"

        def __str__(self) -> str:
            return "nodeType.Partitioner"

    node.nodeType = Method()
    (info,) = node_infos([node], {})
    assert info.top_type == "Partitioner"


class _MenuParm:
    """The ``pdg_cachemode`` menu parm: tokens are PDG enum values, labels are names."""

    def __init__(self, token: str) -> None:
        self._token = token

    def name(self) -> str:
        return "pdg_cachemode"

    def evalAsString(self) -> str:
        return self._token

    def menuItems(self) -> Tuple[str, ...]:
        return ("0", "1", "2", "3")

    def menuLabels(self) -> Tuple[str, ...]:
        return ("Automatic", "Automatic (Ignore Upstream)", "Read Files", "Write Files")


@pytest.mark.parametrize(
    "parms, expected",
    [
        ([_MenuParm("3")], "Write Files"),
        ([_MenuParm("0")], "Automatic"),
        ([_MenuParm("2")], "Read Files"),
        ([_MenuParm("9")], None),
        ([], None),
    ],
)
def test_cache_mode_is_read_as_its_menu_label(parms: List[Any], expected: Optional[str]) -> None:
    """A node's Cache Mode is its menu label, and None when the parm is missing or off the menu.

    The label is what an artist sees, so a message that quotes it can be acted on.
    """
    # One step whose TOP node carries the parms under test.
    node: _PdgNode = _PdgNode("n", command="x")
    _topnet(_wire(node, "/obj/topnet1/n", parms=parms))
    (info,) = node_infos([node], {})
    assert info.cache_mode == expected


def test_a_node_without_a_top_node_reads_no_parms() -> None:
    """A PDG node with no TOP node still reads, with nothing taken from parms."""
    node: _PdgNode = _PdgNode("n", command="x")
    (info,) = node_infos([node], {})
    assert (info.cook_type, info.scheduler_override, info.cache_mode) == (None, "", None)


def test_an_unreadable_scheduler_override_is_empty() -> None:
    """A ``topscheduler`` parm that raises, or holds nothing, means no override."""
    # A parm that raises, then one that holds nothing.
    node: _PdgNode = _PdgNode("n", command="x")
    hom: _Hom = _wire(
        node, "/obj/topnet1/n", parms=[_Parm("topscheduler", "", kind="NodeReference", raises=True)]
    )
    _topnet(hom)
    (info,) = node_infos([node], {})
    assert info.scheduler_override == ""
    hom.parm_list = [_Parm("topscheduler", None, kind="NodeReference")]
    (info,) = node_infos([node], {})
    assert info.scheduler_override == ""


def test_generated_items_are_merged_with_declarations_and_attributes() -> None:
    """Each item carries its declared files, its dependencies, and its node's exported attributes.

    The job's outputs, task order, and attribute checks are all built from these
    fields, so a field read wrong is a wrong job.
    """
    nodes: List[_PdgNode] = _pyro_like()
    cache: _PdgNode = nodes[1]
    upstream_item: _WorkItem = _WorkItem(nodes[0], 0, command="")
    items: List[_WorkItem] = [
        _WorkItem(
            cache,
            i,
            outputs=[
                _File(f"__PDG_DIR__/geo/c.{i}.bgeo.sc"),
                ("tuple/path", "file/x"),
                ("tagless",),
                _File(""),
            ],
            inputs=[_File("/src/in.bgeo.sc", tag="")],
            environment={"zeta": "z", "wedgeindex": i},
        )
        for i in range(2)
    ]
    dependencies: Dict[Any, List[Any]] = {items[0]: [upstream_item], items[1]: []}
    infos: Dict[str, NodeInfo] = _by_name(
        node_infos(nodes, (dependencies, {}, []), cook_set=["cache"])
    )
    cache_info: NodeInfo = infos["cache"]
    assert [i.name for i in cache_info.items] == ["cache_0", "cache_1"]
    first: RawItem = cache_info.items[0]
    # File objects and (path, tag) pairs are read; an empty path is skipped.
    assert first.declared_outputs == (
        DeclaredFile("__PDG_DIR__/geo/c.0.bgeo.sc", "file/geo"),
        DeclaredFile("tuple/path", "file/x"),
        DeclaredFile("tagless", ""),
    )
    assert first.declared_inputs == (DeclaredFile("/src/in.bgeo.sc", ""),)
    assert first.depends_on == ("wedge1_0",)
    assert (first.item_id, first.index, first.command) == (1000, 0, "hython job.py")
    assert first.is_out_of_process
    assert cache_info.items[1].depends_on == ()
    # Sorted, so the same scene always gives the same NodeInfo.
    assert cache_info.exported_attribute_names == ("wedgeindex", "zeta")
    # The cook set names the cache only.
    assert cache_info.in_cook_set is True
    assert infos["render"].in_cook_set is False


def test_missing_item_fields_take_their_defaults() -> None:
    """An item with no index or id still reads: index 0, id -1, and no command or files."""

    class Bare:
        """A work item with only a name and a node."""

        def __init__(self, node: _PdgNode) -> None:
            self.name = "bare_0"
            self.node = node

    # An item whose index and id are not numbers.
    nodes: List[_PdgNode] = _pyro_like()
    infos: Dict[str, NodeInfo] = _by_name(node_infos(nodes, {Bare(nodes[1]): []}))
    (item,) = infos["cache"].items
    assert (item.index, item.item_id, item.command) == (0, -1, "")
    assert (item.declared_outputs, item.declared_inputs, item.in_cook) == ((), (), None)
    assert not item.is_out_of_process and not item.is_batch


def test_an_index_that_is_not_a_number_takes_its_default() -> None:
    """An index or id PDG gives as something other than a number reads as the default."""
    nodes: List[_PdgNode] = _pyro_like()
    item: _WorkItem = _WorkItem(nodes[1], 3)
    item.index = "three"  # type: ignore[assignment]
    item.id = None  # type: ignore[assignment]
    (read,) = _by_name(node_infos(nodes, {item: []}))["cache"].items
    assert (read.index, read.item_id) == (0, -1)


def test_a_node_with_items_is_in_the_cook_even_when_the_set_misses_it() -> None:
    """A node that generated items is in the cook even when the cook set does not name it.

    PDG takes the set when the cook starts, before an HDA's inner nodes exist, so
    those nodes are missing from it while they cook.
    """
    # The cook set is empty, and only render has an item.
    nodes: List[_PdgNode] = _pyro_like()
    item: _WorkItem = _WorkItem(nodes[3], 0)
    infos: Dict[str, NodeInfo] = _by_name(node_infos(nodes, {item: []}, cook_set=[]))
    assert infos["render"].in_cook_set is True
    assert infos["cache"].in_cook_set is False


def test_a_node_the_cook_set_names_is_in_the_cook_before_it_has_items() -> None:
    """A node the cook set names is in the cook while it has no items yet, during a cook or not.

    A step that has not generated when the cook is read is still part of what PDG
    asked to cook. Reading it as outside the cook would make leaving it out of the
    job look harmless instead of a reason to refuse.
    """
    # The cache has a Waiting item; the render, also named, has none yet.
    nodes: List[_PdgNode] = _pyro_like()
    items: Dict[Any, List[Any]] = {
        _WorkItem(nodes[1], 0, state=_Enum("workItemState.Waiting")): [],
    }
    for only_items_in_cook in (False, True):
        infos: Dict[str, NodeInfo] = _by_name(
            node_infos(
                nodes, (items, {}, []), ["cache", "render"], only_items_in_cook=only_items_in_cook
            )
        )
        assert infos["render"].items == ()
        assert infos["render"].in_cook_set is True
        assert infos["cache"].in_cook_set is True
        # A node neither named nor with items stays outside.
        assert infos["wedge1"].in_cook_set is False


def test_during_a_cook_only_the_items_pdg_will_schedule_are_kept() -> None:
    """During a cook, items PDG will not schedule are dropped; outside one, every item is kept.

    Cooking one work item from the UI must not submit every item of its nodes,
    whose extra tasks would never be offered and would stall the cook.
    """
    # Cooking render_1 alone: PDG marks it and its upstream cache_1 Waiting or
    # Scheduled, and leaves the others Uncooked or with an earlier result.
    nodes: List[_PdgNode] = _pyro_like()
    cache, render = nodes[1], nodes[3]
    items: Dict[Any, List[Any]] = {
        _WorkItem(cache, 0, state=_Enum("workItemState.CookedSuccess")): [],
        _WorkItem(cache, 1, state=_Enum("workItemState.Scheduled")): [],
        _WorkItem(cache, 2, state=_Enum("workItemState.CookedFail")): [],
        _WorkItem(cache, 3, state=_Enum("workItemState.CookedCancel")): [],
        _WorkItem(cache, 4, state=_Enum("workItemState.CookedCache")): [],
        _WorkItem(render, 0, state=_Enum("workItemState.Uncooked")): [],
        _WorkItem(render, 1, state=_Enum("workItemState.Waiting")): [],
        _WorkItem(render, 2, state=_Enum("workItemState.Dirty")): [],
    }
    cook_set: List[str] = ["wedge1", "cache", "partition1", "render"]
    infos: Dict[str, NodeInfo] = _by_name(
        node_infos(nodes, (items, {}, []), cook_set, only_items_in_cook=True)
    )
    assert [i.name for i in infos["cache"].items] == ["cache_1"]
    assert [i.name for i in infos["render"].items] == ["render_1"]
    assert infos["cache"].items[0].in_cook is True
    snapshot: GraphSnapshot = synthesize(list(infos.values()))
    assert snapshot.total_tasks == 2
    assert [(s.name, s.dependencies) for s in snapshot.steps] == [
        ("cache", ()),
        ("render", ("cache",)),
    ]

    # Outside a cook every item is kept, whatever its state, and only its state
    # says whether it is in the cook.
    outside_a_cook: Dict[str, NodeInfo] = _by_name(node_infos(nodes, (items, {}, []), cook_set))
    assert len(outside_a_cook["cache"].items) == 5
    assert [i.in_cook for i in outside_a_cook["render"].items] == [False, True, False]
    assert outside_a_cook["cache"].in_cook_set is True


def test_a_node_with_no_item_in_the_cook_is_outside_it() -> None:
    """A node whose every item was dropped is outside the cook, so leaving it out is not a refusal."""
    nodes: List[_PdgNode] = _pyro_like()
    items: Dict[Any, List[Any]] = {
        _WorkItem(nodes[1], 0, state=_Enum("workItemState.CookedSuccess")): [],
        _WorkItem(nodes[3], 0, state=_Enum("workItemState.Waiting")): [],
    }
    infos: Dict[str, NodeInfo] = _by_name(
        node_infos(nodes, (items, {}, []), ["cache", "render"], only_items_in_cook=True)
    )
    assert infos["cache"].items == () and infos["cache"].in_cook_set is False
    assert infos["render"].in_cook_set is True
    # The drop decides even when the cook set is unknown.
    unknown_set: Dict[str, NodeInfo] = _by_name(
        node_infos(nodes, (items, {}, []), only_items_in_cook=True)
    )
    assert unknown_set["cache"].in_cook_set is False
    assert unknown_set["render"].in_cook_set is None


def test_an_item_whose_state_is_unknown_or_unreadable_is_kept() -> None:
    """An item with no state, a new state, or a blank one is kept: only known states drop items."""
    # No state, a state PDG may add later, and a blank one.
    nodes: List[_PdgNode] = _pyro_like()
    items: Dict[Any, List[Any]] = {
        _WorkItem(nodes[1], 0): [],
        _WorkItem(nodes[1], 1, state=_Enum("workItemState.SomethingNew")): [],
        _WorkItem(nodes[1], 2, state=_Enum("")): [],
    }
    infos: Dict[str, NodeInfo] = _by_name(
        node_infos(nodes, (items, {}, []), only_items_in_cook=True)
    )
    assert [i.in_cook for i in infos["cache"].items] == [None, True, None]


def test_an_unknown_cook_set_leaves_membership_unknown() -> None:
    """With no cook set, whether each node is in the cook is unknown, not False."""
    infos: Tuple[NodeInfo, ...] = node_infos(_pyro_like(), {})
    assert {info.in_cook_set for info in infos} == {None}


def test_batched_children_carry_their_parent() -> None:
    """A batch child names its parent, and is not itself a batch.

    The dependency graph holds the children, so the parent is the only record of
    the batch.
    """
    nodes: List[_PdgNode] = _pyro_like()
    parent: Any = type("Parent", (), {"name": "render_batch"})()
    item: _WorkItem = _WorkItem(nodes[3], 0, batch_parent=parent, batchIndex=0)
    item.isBatch = True
    (render,) = [n for n in node_infos(nodes, {item: []}) if n.name == "render"]
    assert render.items[0].batch_parent == "render_batch"
    assert render.items[0].is_batch is True
    # Without a parent, the item is not batched.
    plain: _WorkItem = _WorkItem(nodes[3], 1)
    (render,) = [n for n in node_infos(nodes, {plain: []}) if n.name == "render"]
    assert (render.items[0].batch_parent, render.items[0].is_batch) == ("", False)


def test_items_of_a_node_the_topology_lacks_are_kept_and_classified_by_their_commands() -> None:
    """Items whose node the topology lacks are kept, and their commands decide whether it is a step.

    Nothing is known of such a node, so calling it a step outright would make a
    step whose tasks run nothing.
    """
    nodes: List[_PdgNode] = _pyro_like()
    stray: _PdgNode = _PdgNode("stray")
    idle: _PdgNode = _PdgNode("idle")
    upstream: _WorkItem = _WorkItem(nodes[1], 0)
    items: Dict[Any, List[Any]] = {
        _WorkItem(stray, 0): [upstream],
        upstream: [],
        _WorkItem(idle, 0, command=""): [],
    }
    infos: Dict[str, NodeInfo] = _by_name(node_infos(nodes, items, cook_set=[]))
    stray_info: NodeInfo = infos["stray"]
    assert stray_info.schedulable is None
    assert (stray_info.top_type, stray_info.is_dynamic, stray_info.node_type_name) == (
        "",
        False,
        "",
    )
    assert stray_info.upstream == ("cache",)
    assert len(stray_info.items) == 1
    assert stray_info.in_cook_set is True
    # The item with a command makes a step; the one without is skipped.
    snapshot: GraphSnapshot = synthesize(list(infos.values()))
    assert "stray" in snapshot.step_names
    assert "idle" in snapshot.skipped_nodes


def test_a_dynamic_node_is_reported() -> None:
    """A dynamic node is flagged, because its items appear only while upstream items cook."""
    node: _PdgNode = _PdgNode("dyn", command="x", dynamic=True)
    (info,) = node_infos([node], {})
    assert info.is_dynamic and info.schedulable


def test_an_unreadable_script_info_is_not_a_step() -> None:
    """A node whose job script cannot be read is not a step, rather than a step that runs nothing."""
    # A node with no scriptInfo at all.
    node: _PdgNode = _PdgNode("n")
    del node.scriptInfo
    (info,) = node_infos([node], {})
    assert info.schedulable is False


def test_a_node_that_does_not_say_it_is_dynamic_is_not() -> None:
    """A node with no ``isDynamic`` is static, so it is not refused as dynamic."""
    # A PDG node object with the attribute missing altogether.
    node: _PdgNode = _PdgNode("n", command="x")
    del node.isDynamic
    (info,) = node_infos([node], {})
    assert info.is_dynamic is False


def test_a_wrapper_and_its_inner_node_union_their_inputs() -> None:
    """A ROP Fetch wrapper subnet and the node inside it share one PDG node, and their wires add up."""
    up: _PdgNode = _PdgNode("karma", command="x")
    wrapped: _PdgNode = _PdgNode("overlay_ropfetch1", command="x")
    h_up: _Hom = _wire(up, "/obj/topnet1/karma")
    wrapper: _Hom = _Hom(
        "/obj/topnet1/overlay", category="Top", child_category="Top", pdg=wrapped, inputs=[h_up]
    )
    # The inner node's indirect inputs: one the wrapper has, and one it does not.
    inner: _Hom = _Hom(
        "/obj/topnet1/overlay/ropfetch1",
        category="Top",
        pdg=wrapped,
        inputs=[_IndirectInput(wrapper, 0), _IndirectInput(wrapper, 5)],
        parent=wrapper,
    )
    wrapper.child_list = [inner]
    wrapped.hom = inner
    _topnet(h_up, wrapper)
    infos: Dict[str, NodeInfo] = _by_name(node_infos([up, wrapped], {}))
    assert infos["overlay_ropfetch1"].upstream == ("karma",)


def test_an_indirect_input_reaches_through_the_subnet() -> None:
    """An inner node wired only through its subnet's input gets the node wired to the subnet."""
    # Two indirect inputs: one in range of the subnet's inputs, one not.
    up: _PdgNode = _PdgNode("up", command="x")
    inner_pdg: _PdgNode = _PdgNode("inner", command="x")
    h_up: _Hom = _wire(up, "/obj/topnet1/up")
    last: _PdgNode = _PdgNode("last", command="x")
    h_last: _Hom = _wire(last, "/obj/topnet1/last")
    sub: _Hom = _Hom(
        "/obj/topnet1/sub", category="Top", child_category="Top", inputs=[h_up, h_last]
    )
    inner: _Hom = _wire(
        inner_pdg,
        "/obj/topnet1/sub/inner",
        [_IndirectInput(sub, 0), _IndirectInput(sub, -1)],
        parent=sub,
    )
    sub.child_list = [inner]
    _topnet(h_up, h_last, sub)
    infos: Dict[str, NodeInfo] = _by_name(node_infos([up, last, inner_pdg], {}))
    # A negative index names no input, not one counted from the end.
    assert infos["inner"].upstream == ("up",)


def test_wiring_passes_through_a_top_node_without_a_pdg_node_and_into_subnets() -> None:
    """Wires pass through TOP nodes with no PDG node and into a subnet's display node, but not out of TOPs."""
    a: _PdgNode = _PdgNode("a", command="x")
    b: _PdgNode = _PdgNode("b", command="x")
    inner_a: _Hom = _wire(a, "/obj/topnet1/sub/a")
    sub: _Hom = _Hom(
        "/obj/topnet1/sub",
        category="Top",
        child_category="Top",
        children=[inner_a],
        display=inner_a,
    )
    inner_a.parent_node = sub
    sort: _Hom = _Hom("/obj/topnet1/sort_by_frame", category="Top", inputs=[sub, None])
    h_b: _Hom = _wire(b, "/obj/topnet1/b", [sort])
    # A SOP wired in is not followed: it has no PDG node and is not a TOP node.
    c: _PdgNode = _PdgNode("c", command="x")
    stranger: _Hom = _Hom("/obj/geo1", category="Sop", inputs=[inner_a])
    h_c: _Hom = _wire(c, "/obj/topnet1/c", [stranger])
    _topnet(sub, sort, h_b, h_c)
    infos: Dict[str, NodeInfo] = _by_name(node_infos([a, b, c], {}))
    assert infos["b"].upstream == ("a",)
    assert infos["c"].upstream == ()


def test_a_node_in_a_nested_subnet_finds_the_outer_network() -> None:
    """A node two subnets deep is wired to a node in the outer network."""
    # a sits in a subnet; b sits one subnet below that. Neither is a direct
    # child of the network.
    a: _PdgNode = _PdgNode("a", command="x")
    b: _PdgNode = _PdgNode("b", command="x")
    outer: _Hom = _Hom("/obj/topnet1/outer", category="Top", child_category="Top")
    h_a: _Hom = _wire(a, "/obj/topnet1/outer/a", parent=outer)
    inner: _Hom = _Hom("/obj/topnet1/outer/inner", category="Top", child_category="Top")
    h_b: _Hom = _wire(b, "/obj/topnet1/outer/inner/b", [h_a], parent=inner)
    inner.parent_node = outer
    inner.child_list = [h_b]
    outer.child_list = [h_a, inner]
    _topnet(outer)
    infos: Dict[str, NodeInfo] = _by_name(node_infos([b, a], {}))
    assert infos["b"].upstream == ("a",)


def test_cyclic_wiring_terminates() -> None:
    """A wire loop, or an indirect input whose subnet cannot be read, ends with no upstream."""
    # A TOP node wired to itself, and an indirect input that raises.
    a: _PdgNode = _PdgNode("a", command="x")
    loop: _Hom = _Hom("/obj/topnet1/loop", category="Top")
    loop.input_list = [loop]
    h_a: _Hom = _wire(a, "/obj/topnet1/a", [loop, _IndirectInput(_Raises(), 0)])
    _topnet(h_a, loop)
    (info,) = node_infos([a], {})
    assert info.upstream == ()


def test_a_node_wired_to_itself_has_no_upstream() -> None:
    """A node wired back to itself, through a wire or an item, is not its own upstream."""
    a: _PdgNode = _PdgNode("a", command="x")
    h_a: _Hom = _wire(a, "/obj/topnet1/a")
    passthrough: _Hom = _Hom("/obj/topnet1/p", category="Top", inputs=[h_a])
    h_a.input_list = [passthrough]
    _topnet(h_a, passthrough)
    (info,) = node_infos([a], {})
    assert info.upstream == ()
    # A wrapper whose inner node is wired to the wrapper: both are one PDG node.
    w: _PdgNode = _PdgNode("w", command="x")
    wrapper: _Hom = _Hom("/obj/topnet1/w", category="Top", child_category="Top", pdg=w)
    inner: _Hom = _wire(w, "/obj/topnet1/w/inner", [wrapper], parent=wrapper)
    wrapper.child_list = [inner]
    _topnet(wrapper)
    (info,) = node_infos([w], {})
    assert info.upstream == ()
    # Without a network, an item that depends on its own node's item adds no edge.
    b: _PdgNode = _PdgNode("b", command="x")
    first, second = _WorkItem(b, 0), _WorkItem(b, 1)
    (info,) = node_infos([b], {first: [], second: [first]})
    assert info.upstream == ()


def test_without_any_top_network_wiring_comes_from_item_dependencies() -> None:
    """With no TOP network to read, each node's upstream comes from its items' dependencies."""
    # No TOP node for either PDG node, so no network.
    a, b = _PdgNode("a", command="x"), _PdgNode("b", command="x")
    item_a, item_b = _WorkItem(a, 0), _WorkItem(b, 0)
    # A dependency on an item with no node adds no edge.
    orphan: _WorkItem = _WorkItem(a, 1)
    orphan.node = None  # type: ignore[assignment]
    graph: Dict[Any, List[Any]] = {item_a: [], item_b: [item_a, orphan]}
    infos: Dict[str, NodeInfo] = _by_name(node_infos([a, b], graph))
    assert infos["b"].upstream == ("a",)
    assert infos["a"].upstream == ()


def test_with_a_top_network_item_dependencies_do_not_add_wiring() -> None:
    """When the network can be read, item dependencies add no edge: the topology alone wires."""
    # Both nodes are in a network with no wire between them.
    a, b = _PdgNode("a", command="x"), _PdgNode("b", command="x")
    _topnet(_wire(a, "/obj/topnet1/a"), _wire(b, "/obj/topnet1/b"))
    item_a, item_b = _WorkItem(a, 0), _WorkItem(b, 0)
    infos: Dict[str, NodeInfo] = _by_name(node_infos([a, b], {item_a: [], item_b: [item_a]}))
    assert infos["b"].upstream == ()


def test_unreadable_inputs_never_raise() -> None:
    """Nameless, repeated, and raising nodes and items are skipped, and the rest still read."""
    good: _PdgNode = _PdgNode("good", command="x")
    nameless: _PdgNode = _PdgNode("")
    twin: _PdgNode = _PdgNode("good")
    _topnet(_wire(good, "/obj/topnet1/good"))
    broken_item: _WorkItem = _WorkItem(good, 1)
    broken_item.name = ""
    homeless: _WorkItem = _WorkItem(good, 2)
    homeless.node = None  # type: ignore[assignment]
    infos: Tuple[NodeInfo, ...] = node_infos(
        [good, nameless, _Raises(), twin], {broken_item: [], _Raises(): [], homeless: []}
    )
    assert [i.name for i in infos] == ["good"]
    assert infos[0].schedulable is True and infos[0].items == ()
    assert node_infos(_Raises(), _Raises(), cook_set=None) == ()
    assert node_infos([], ["not a mapping"]) == ()


def test_an_input_whose_every_attribute_raises_is_skipped() -> None:
    """An input that raises on any attribute, like a deleted node, is skipped, and the other inputs still wire.

    Such an input raises ``RuntimeError``, not ``AttributeError``, so a check that
    only tolerates a missing attribute would let it escape ``node_infos``.
    """
    # Two steps; the second is wired to a raising input and to the first.
    up: _PdgNode = _PdgNode("up", command="x")
    down: _PdgNode = _PdgNode("down", command="x")
    h_up: _Hom = _wire(up, "/obj/topnet1/up")
    _topnet(h_up, _wire(down, "/obj/topnet1/down", [_Raises(), h_up]))
    infos: Dict[str, NodeInfo] = _by_name(node_infos([up, down], {}))
    assert infos["down"].upstream == ("up",)


def test_the_dependency_graph_is_read_from_its_first_element() -> None:
    """PDG's ``dependencyGraph`` is a triple, and only its first element maps items to dependencies."""
    # PDG's triple, as a list and as a tuple.
    nodes: List[_PdgNode] = _pyro_like()
    item: _WorkItem = _WorkItem(nodes[1], 0)
    other: _WorkItem = _WorkItem(nodes[3], 0)
    graphs: List[Any] = [[{item: []}, {other: []}], ({item: []}, {other: []}, [])]
    for graph in graphs:
        infos: Dict[str, NodeInfo] = _by_name(node_infos(nodes, graph))
        assert [i.name for i in infos["cache"].items] == ["cache_0"]
        assert infos["render"].items == ()


def _st_generation(nodes: List[_PdgNode]) -> st.SearchStrategy[Dict[str, List[_WorkItem]]]:
    """For any subset of the step nodes, two items each: what generation has reached so far."""
    items: Dict[str, List[_WorkItem]] = {
        node.name: [_WorkItem(node, i) for i in range(2)]
        for node in nodes
        if node.scriptInfo.command
    }
    return st.sets(st.sampled_from(sorted(items))).map(
        lambda keep: {name: items[name] for name in keep}
    )


@settings(max_examples=50)
@given(st.data())
def test_less_generation_never_changes_classification_or_wiring(data: st.DataObject) -> None:
    """For any partial generation, classification and wiring equal the full one's.

    The only difference is that steps without items yet move to
    ``steps_without_items``. This is what makes a cook that starts mid-generation
    safe to refuse rather than silently short.
    """
    nodes: List[_PdgNode] = _pyro_like()
    full_items: Dict[str, List[_WorkItem]] = {
        node.name: [_WorkItem(node, i) for i in range(2)]
        for node in nodes
        if node.scriptInfo.command
    }
    partial_items: Dict[str, List[_WorkItem]] = data.draw(_st_generation(nodes))
    full: Dict[Any, List[Any]] = {wi: [] for its in full_items.values() for wi in its}
    partial: Dict[Any, List[Any]] = {wi: [] for its in partial_items.values() for wi in its}
    a: Dict[str, NodeInfo] = _by_name(node_infos(nodes, full))
    b: Dict[str, NodeInfo] = _by_name(node_infos(nodes, partial))
    assert set(a) == set(b)
    for name in a:
        assert (a[name].schedulable, a[name].upstream) == (b[name].schedulable, b[name].upstream)
    # Going from full to partial generation only moves steps to steps_without_items.
    full_snapshot, partial_snapshot = synthesize(list(a.values())), synthesize(list(b.values()))
    moved: Set[str] = set(full_snapshot.step_names) - set(partial_snapshot.step_names)
    assert moved == set(partial_snapshot.steps_without_items) - set(
        full_snapshot.steps_without_items
    )
    assert moved == {"cache", "render"} - set(partial_items)


# -- the scene walk -----------------------------------------------------------


def _file(name: str, value: str, raw: Optional[str] = None, **kwargs: Any) -> _Parm:
    """A file-reference parm, authored as ``raw`` and evaluated to ``value``."""
    return _Parm(name, value, raw, **kwargs)


def _noderef(name: str, target: str) -> _Parm:
    """A node-reference parm naming ``target``."""
    return _Parm(name, target, kind="NodeReference")


def test_file_reference_parms_become_reads_and_writes_are_flagged() -> None:
    """Every file-reference parm that names a file is a read, and output parms are flagged.

    Embedded, empty, ignored, and non-file parms are left out, because matching
    them would invent dependencies or false path refusals.
    """
    # One parm for each case the reader must keep or skip.
    top: _Hom = _Hom(
        "/obj/topnet1/imagemagick1",
        category="Top",
        parms=[
            _file("inputfile", "/proj/in.png", "$HIP/in.png"),
            _file("outputfilepath", "/proj/out.png", "$HIP/out.png"),
            _file("perffile", "/proj/perf.hiperf"),
            _file("prerender", "/proj/script.py"),
            _file("empty", ""),
            _file("embedded", "opdef:/Sop/x?icon"),
            _file("broken", "", raises=True),
            _Parm("label", "/proj/in.png", kind="Regular"),
            _Parm("count", 3, template="Int"),
            _Raises(),
        ],
    )
    result: SceneReads = collect_scene_reads({"imagemagick1": top})
    assert result.available
    assert result.walked == {"imagemagick1": 1}
    assert [(r.parm, r.raw, r.path, r.is_output) for r in result.reads] == [
        ("inputfile", "$HIP/in.png", "/proj/in.png", False),
        ("outputfilepath", "$HIP/out.png", "/proj/out.png", True),
    ]
    assert result.reads[0].step == "imagemagick1"
    assert result.reads[0].node_path == "/obj/topnet1/imagemagick1"


def test_node_references_inputs_and_the_own_subtree_are_walked_but_not_other_top_nodes() -> None:
    """The walk follows node references, input ancestors, and the step's own children, and no other TOP node.

    Entering another TOP node would give this step that node's reads, and an edge
    the topology already decides.
    """

    class Untyped(_Hom):
        """A node whose type cannot be read. It is walked, as a node that is not a TOP node."""

        def type(self) -> _Type:
            raise RuntimeError("no type")

    cache: _Hom = _Hom("/obj/geo/filecache1", parms=[_file("file", "/proj/geo/pyro.vdb")])
    other_top: _Hom = _Hom("/obj/topnet1/other", category="Top", parms=[_file("file", "/x")])
    untyped: Untyped = Untyped("/obj/geo/untyped", parms=[_file("file", "/u")])
    importer: _Hom = _Hom("/obj/geo/import", ancestors=[cache, other_top, untyped])
    font: _Hom = _Hom(
        "/obj/topnet1/overlay/c/font1", category="Cop", parms=[_file("file", "/f.ttf")]
    )
    inner_top: _Hom = _Hom("/obj/topnet1/overlay/inner", category="Top")
    # A SOP's children are not entered: only the step's own subtree is.
    outside_child: _Hom = _Hom("/obj/geo/import/hidden", parms=[_file("file", "/hidden")])
    importer.child_list = [outside_child]
    top: _Hom = _Hom(
        "/obj/topnet1/overlay",
        category="Top",
        children=[font, inner_top],
        parms=[
            _noderef("roppath", "../geo/import"),
            _noderef("missing", "nowhere"),
            _noderef("blank", ""),
            _Parm("unreadable", "", kind="NodeReference", raises=True),
        ],
        lookup={"../geo/import": importer},
    )
    result: SceneReads = collect_scene_reads({"overlay": top})
    paths: Set[str] = {r.node_path for r in result.reads}
    assert paths == {"/obj/geo/filecache1", "/obj/topnet1/overlay/c/font1", "/obj/geo/untyped"}
    assert result.walked == {"overlay": 6}


def test_a_channel_reference_reports_the_authored_value_it_copies() -> None:
    """A parm that only copies another with ``chs`` reports the text authored on the other.

    A ROP inside a ROP Fetch wrapper copies the wrapper's output parm, and the
    portability checks must judge what the artist typed on the wrapper.
    """
    wrapper_parm: _Parm = _Parm(
        "copoutput", "/proj/img/x.png", "$DATA/img/x.png", node_path="/obj/topnet1/overlay"
    )
    inner: _Parm = _Parm(
        "copoutput",
        "/proj/img/x.png",
        'chs("../../copoutput")',
        referenced=wrapper_parm,
        node_path="/obj/topnet1/overlay/ropnet1/comp1",
    )
    keyed: _Parm = _Parm("file", "/proj/k.png", "$HIP/k.png", unexpanded_raises=True)
    only_ref: _Parm = _Parm("picture", "/proj/r.exr", 'chs("x")')
    # A copy of a copy reports the evaluated value, as does a parm with no text.
    chained: _Parm = _Parm(
        "chained",
        "/proj/c.exr",
        'ch("../a")',
        referenced=_Parm("a", "/proj/c.exr", 'chsraw("../b")', node_path="/obj/up"),
    )
    blank: _Parm = _Parm("blank", "/proj/b.exr", "")
    top: _Hom = _Hom(
        "/obj/topnet1/overlay", category="Top", parms=[inner, keyed, only_ref, chained, blank]
    )
    reads: Dict[str, str] = {r.parm: r.raw for r in collect_scene_reads({"overlay": top}).reads}
    assert reads == {
        "copoutput": "$DATA/img/x.png",
        "file": "$HIP/k.png",
        "picture": "/proj/r.exr",
        "chained": "/proj/c.exr",
        "blank": "/proj/b.exr",
    }


def test_a_parm_whose_text_cannot_be_read_reports_its_value() -> None:
    """A parm that refuses both ``unexpandedString`` and ``rawValue`` reports its evaluated value."""

    class Sealed(_Parm):
        """A parm whose authored text cannot be read at all."""

        def rawValue(self) -> str:
            raise RuntimeError("sealed")

    sealed: Sealed = Sealed("file", "/proj/s.png", "$HIP/s.png", unexpanded_raises=True)
    top: _Hom = _Hom("/obj/topnet1/t", category="Top", parms=[sealed])
    (read,) = collect_scene_reads({"t": top}).reads
    assert (read.raw, read.path) == ("/proj/s.png", "/proj/s.png")


def test_one_failing_step_does_not_stop_the_others() -> None:
    """A step whose walk raises loses only its own reads; a step with no TOP node is skipped."""

    class Exploding(_Hom):
        """A TOP node whose path raises, like a node deleted mid-cook."""

        def path(self) -> str:
            raise RuntimeError("gone")

    # One readable step, one with no TOP node, and one that raises.
    ok: _Hom = _Hom("/obj/topnet1/ok", category="Top", parms=[_file("file", "/a")])
    result: SceneReads = collect_scene_reads(
        {"ok": ok, "none": None, "unreadable": Exploding("/x")}
    )
    assert [r.step for r in result.reads] == ["ok"]
    assert result.walked == {"ok": 1, "unreadable": 0}
    assert result.available


def test_a_walk_that_raises_marks_the_reads_unavailable() -> None:
    """A step that cannot be looked up, or steps that cannot be listed, mark the reads unavailable.

    Validation then says the path checks did not run, instead of passing them.
    """

    class BadMapping(dict):  # type: ignore[type-arg]
        """A mapping that lists a step it cannot return."""

        def __getitem__(self, key: Any) -> Any:
            raise KeyError(key)

    result: SceneReads = collect_scene_reads(BadMapping({"s": object(), "t": object()}))
    assert not result.available
    assert result.walked == {}
    assert collect_scene_reads(None).available is False  # type: ignore[arg-type]


def test_the_node_ceiling_stops_a_runaway_walk() -> None:
    """The walk stops at ``max_nodes``, so a huge scene cannot hang the artist's session."""
    chain: List[_Hom] = []
    for i in range(50):
        chain.append(_Hom(f"/obj/n{i}", ancestors=chain[-1:], parms=[_file("file", f"/f{i}")]))
    top: _Hom = _Hom("/obj/topnet1/t", category="Top", ancestors=[chain[-1]])
    result: SceneReads = collect_scene_reads({"t": top}, max_nodes=10)
    assert result.walked == {"t": 10}
    assert len(result.reads) == 9
    # A walk that ends exactly at the ceiling reads every node.
    exact: SceneReads = collect_scene_reads({"t": top}, max_nodes=51)
    assert exact.walked == {"t": 51} and len(exact.reads) == 50


def test_a_walk_cut_short_by_the_ceiling_warns(caplog: pytest.LogCaptureFixture) -> None:
    """A walk stopped by the ceiling logs a warning, so the artist knows its reads may be incomplete.

    A walk that reaches every node, even exactly at the ceiling, does not warn.
    """
    chain: List[_Hom] = []
    for i in range(5):
        chain.append(_Hom(f"/obj/n{i}", ancestors=chain[-1:]))
    top: _Hom = _Hom("/obj/topnet1/t", category="Top", ancestors=[chain[-1]])
    # Exactly at the ceiling, then under it, then cut short.
    with caplog.at_level("WARNING"):
        collect_scene_reads({"t": top}, max_nodes=6)
        collect_scene_reads({"t": top}, max_nodes=7)
    assert not [r for r in caplog.records if "stopped" in r.getMessage()]
    with caplog.at_level("WARNING"):
        collect_scene_reads({"t": top}, max_nodes=3)
    assert [r.getMessage() for r in caplog.records if "stopped" in r.getMessage()] == [
        "The scene walk from '/obj/topnet1/t' stopped at 3 nodes; its reads may be incomplete."
    ]


def test_the_default_ceiling_is_five_thousand_nodes() -> None:
    """Without a ``max_nodes``, a walk stops at 5,000 nodes."""
    # A chain one node longer than the ceiling, the step's TOP node included.
    chain: List[_Hom] = []
    for i in range(5_001):
        chain.append(_Hom(f"/obj/n{i}", ancestors=chain[-1:]))
    top: _Hom = _Hom("/obj/topnet1/t", category="Top", ancestors=[chain[-1]])
    assert collect_scene_reads({"t": top}).walked == {"t": 5_000}


def test_a_ceiling_below_one_walks_nothing() -> None:
    """A ``max_nodes`` below 1 walks no node, not even the step's own."""
    top: _Hom = _Hom("/obj/topnet1/t", category="Top", parms=[_file("file", "/a")])
    for max_nodes in (0, -3):
        result: SceneReads = collect_scene_reads({"t": top}, max_nodes=max_nodes)
        assert (result.reads, result.walked) == ((), {"t": 0})


@st.composite
def _scenes(draw: Callable[..., Any]) -> Tuple[_Hom, int, Dict[str, str]]:
    """For any scene graph with cycles and scattered TOP nodes, a step's TOP node, a ceiling, and each node's category.

    Every node has one file parm naming its own path, so the reads show which
    nodes the walk reached, and in what order. The categories, by path, let the
    property tell a foreign TOP node from a SOP.
    """
    # The step's own children and outside nodes, each a SOP or a TOP node.
    count: int = draw(st.integers(1, 12))
    nodes: List[_Hom] = [
        _Hom(
            f"/obj/topnet1/root/c{i}" if draw(st.booleans()) else f"/obj/s{i}",
            category=draw(st.sampled_from(["Sop", "Top"])),
        )
        for i in range(count)
    ]
    root: _Hom = _Hom("/obj/topnet1/root", category="Top")
    everything: List[_Hom] = [root] + nodes
    for node in everything:
        # Random wires, children, and node references, cycles included.
        node.ancestor_list = draw(st.lists(st.sampled_from(everything), max_size=3))
        node.child_list = draw(st.lists(st.sampled_from(everything), max_size=2))
        targets: List[_Hom] = draw(st.lists(st.sampled_from(everything), max_size=2))
        node.parm_list = [_noderef(f"ref{k}", t.path()) for k, t in enumerate(targets)]
        node.parm_list.append(_file("file", node.path()))
        node.lookup = {t.path(): t for t in targets}
    categories: Dict[str, str] = {node.path(): node.type().category().name() for node in nodes}
    return root, draw(st.integers(-2, 15)), categories


@settings(max_examples=150)
@given(_scenes())
def test_the_walk_terminates_within_bounds_and_never_crosses_into_foreign_top_nodes(
    scene: Tuple[_Hom, int, Dict[str, str]],
) -> None:
    """For any scene and ceiling, the walk ends, stays under the ceiling, starts at the step, and visits each node once.

    It never reaches a TOP node outside the step's own subtree, whose reads belong
    to another step.
    """
    # The reads follow the walk, one per node, so they show its order.
    root, max_nodes, categories = scene
    result: SceneReads = collect_scene_reads({"root": root}, max_nodes)
    paths: List[str] = [r.node_path for r in result.reads]
    assert result.walked["root"] == len(paths)
    assert len(paths) <= max(0, max_nodes)
    assert len(paths) == len(set(paths))
    if max_nodes >= 1:
        assert paths[0] == root.path()
    # Outside the step's own subtree, only non-TOP nodes are walked.
    for path in paths:
        inside: bool = path == root.path() or path.startswith(root.path() + "/")
        assert inside or categories[path] != "Top"


# -- static prefixes ----------------------------------------------------------


def _pyro_karma(raws: Dict[str, str]) -> Dict[str, _Hom]:
    """A karma step whose ``roppath`` reaches a pyro cache SOP with one file parm per raw."""
    cache: _Hom = _Hom(
        "/obj/pyro/pyro_cache",
        parms=[_file(name, "/proj/evaluated", raw) for name, raw in raws.items()],
    )
    top: _Hom = _Hom(
        "/obj/topnet1/karma",
        category="Top",
        parms=[_noderef("roppath", "../../pyro/pyro_cache")],
        lookup={"../../pyro/pyro_cache": cache},
    )
    return {"karma": top}


def _hip(text: str) -> str:
    """An expander that knows ``$HIP`` only, as ``/proj``, and raises for any other variable."""
    expanded: str = text.replace("$HIP", "/proj")
    if "$" in expanded:
        raise KeyError(expanded)
    return expanded


def _prefixes(
    top_nodes: Dict[str, _Hom], expand: Optional[Callable[[str], str]] = _hip
) -> Dict[str, str]:
    """Each read's static prefix, by parm name."""
    return {r.parm: r.static_prefix for r in collect_scene_reads(top_nodes, expand=expand).reads}


def test_a_read_driven_by_an_attribute_records_its_evaluated_static_prefix() -> None:
    """A wedged read records the evaluated text before its first expression, with ``$OS`` as its node.

    Matching that prefix is what gives the karma step its edge to the pyro cache
    step, so its worker syncs the cache.
    """
    prefixes: Dict[str, str] = _prefixes(
        _pyro_karma(
            {
                "file": "$HIP/geo/$OS/wedge`@wedgeindex`/$OS.w`@wedgeindex`.$F4.bgeo.sc",
                "braced": "$HIP/geo/${OS}_v2/w`@wedgeindex`.bgeo.sc",
                "bare": "$HIP/geo/$OS/w@wedgeindex.bgeo.sc",
                "longer": "$HIP/$OSX/w`@wedgeindex`",
                "plain": "$HIP/geo/x.bgeo.sc",
                "input": "`@pdg_input`",
                "blank": "  `@pdg_input`",
                "unknown": "$HIP/$NOPE/wedge`@wedgeindex`",
            }
        )
    )
    assert prefixes == {
        "file": "/proj/geo/pyro_cache/wedge",
        "braced": "/proj/geo/pyro_cache_v2/w",
        # An attribute without backticks cuts the text too.
        "bare": "/proj/geo/pyro_cache/w",
        # ``$OSX`` is another variable, which the expander does not know.
        "longer": "",
        "plain": "",
        "input": "",
        "blank": "",
        "unknown": "",
    }
    # Without an expander nothing is evaluated.
    assert set(_prefixes(_pyro_karma({"file": "$HIP/w`@i`"}), expand=None).values()) == {""}


def test_os_in_a_copied_parm_names_the_node_that_holds_the_text() -> None:
    """``$OS`` in text copied with ``chs`` names the node the text was authored on.

    Houdini evaluates the copied text in that node, so naming the reading node
    instead gives a prefix no step writes under, and the data edge is lost.
    """
    # The wrapper holds the text; the inner node only copies it with chs.
    wrapper: _Hom = _Hom("/obj/topnet1/overlay", category="Top")
    authored: _Parm = _Parm(
        "copoutput",
        "/proj/render/overlay/w0/x.png",
        "$HIP/render/$OS/w`@wedgeindex`/x.png",
        node_path="/obj/topnet1/overlay",
        owner=wrapper,
    )
    inner: _Parm = _Parm(
        "copoutput",
        "/proj/render/overlay/w0/x.png",
        'chs("../../copoutput")',
        referenced=authored,
        node_path="/obj/topnet1/overlay/ropnet1/comp1",
    )
    comp: _Hom = _Hom("/obj/topnet1/overlay/ropnet1/comp1", category="Cop", parms=[inner])
    wrapper.child_list = [comp]
    (read,) = collect_scene_reads({"overlay": wrapper}, expand=_hip).reads
    assert read.node_path == "/obj/topnet1/overlay/ropnet1/comp1"
    assert read.raw == "$HIP/render/$OS/w`@wedgeindex`/x.png"
    assert read.static_prefix == "/proj/render/overlay/w"


def test_os_in_copied_text_whose_node_cannot_be_read_gives_no_prefix() -> None:
    """When the node holding copied text cannot be read, ``$OS`` cannot be evaluated, so the prefix is empty.

    A copied text without ``$OS`` still gets its prefix: only ``$OS`` needs the node.
    """
    # Copied text whose node is missing, and copied text whose node raises.
    with_os: _Parm = _Parm("a", "/v", "$HIP/$OS/w`@i`", node_path="/obj/x", owner=None)
    without_os: _Parm = _Parm("b", "/v", "$HIP/geo/w`@i`", node_path="/obj/x", owner=_Raises())
    parms: List[_Parm] = [
        _Parm("a", "/v", 'chs("../x/a")', referenced=with_os, node_path="/obj/t/c"),
        _Parm("b", "/v", 'chs("../x/b")', referenced=without_os, node_path="/obj/t/c"),
    ]
    top: _Hom = _Hom("/obj/topnet1/t", category="Top", parms=parms)
    assert _prefixes({"t": top}) == {"a": "", "b": "/proj/geo/w"}


def test_a_node_whose_name_cannot_be_read_is_named_by_its_path() -> None:
    """``$OS`` falls back to the last component of the node's path when ``name()`` fails."""

    class Nameless(_Hom):
        """A node whose ``name()`` raises."""

        def name(self) -> str:
            raise RuntimeError("no name")

    top: Nameless = Nameless(
        "/obj/topnet1/cache_a", category="Top", parms=[_file("file", "/v", "$HIP/$OS/w`@i`")]
    )
    assert _prefixes({"t": top}) == {"file": "/proj/cache_a/w"}


_RAW_TEXT: st.SearchStrategy[str] = st.text(alphabet="ab/._ `@$HIP", min_size=1, max_size=24)


@settings(max_examples=300)
@given(
    raw=_RAW_TEXT,
    expander=st.sampled_from(["none", "raises", "brackets"]),
)
def test_a_static_prefix_is_the_expanded_text_before_the_first_backtick_or_at(
    raw: str, expander: str
) -> None:
    """For any authored text, the prefix is the expanded text before the first backtick or ``@``.

    It is ``""`` when the text has neither, its static part is blank, there is no
    expander, or the expander raises, so a prefix never stands for a path that was
    not evaluated.
    """

    def brackets(text: str) -> str:
        """An expander that marks what it was given, so the test sees exactly that text."""
        return f"<{text}>"

    def raises(text: str) -> str:
        """An expander that knows no variable."""
        raise KeyError(text)

    expanders: Dict[str, Optional[Callable[[str], str]]] = {
        "none": None,
        "raises": raises,
        "brackets": brackets,
    }
    top: _Hom = _Hom("/obj/topnet1/t", category="Top", parms=[_file("file", "/v", raw)])
    (read,) = collect_scene_reads({"t": top}, expand=expanders[expander]).reads
    # The model: split once at the first backtick or ``@``.
    parts: List[str] = re.split("[`@]", raw, maxsplit=1)
    evaluated: bool = len(parts) == 2 and bool(parts[0].strip()) and expander == "brackets"
    expected: str = f"<{parts[0]}>" if evaluated else ""
    assert read.static_prefix == expected


# -- describe_work_item -------------------------------------------------------


class _Unnamed:
    """A neighbouring item whose ``name`` raises, so it is described by its printed form."""

    @property
    def name(self) -> str:
        raise RuntimeError("deleted")

    def __str__(self) -> str:
        return "unnamed"


def test_a_work_item_is_described_as_json() -> None:
    """A work item's description holds its fields, files, attributes, and neighbours, and is JSON.

    The debug directory writes it as JSON, so a value that cannot be serialized
    would lose the whole record.
    """
    node: _PdgNode = _PdgNode("cache")
    item: _WorkItem = _WorkItem(
        node,
        3,
        outputs=[
            _File("__PDG_DIR__/a", local_path="/proj/a"),
            _File("/same", local_path="/same"),
            _File("/untagged", tag=None),  # type: ignore[arg-type]
            object(),
        ],
        inputs=[_File("/in")],
        environment={"wedgeindex": 2},
        frame=1.5,
        isBatch=False,
        outputDir=_Enum("/proj/out"),
        state=_Enum("workItemState.Scheduled"),
        dependencies=[type("Dep", (), {"name": "up_0"})(), "raw", _Unnamed()],
        dependents=lambda: [],
    )
    item.attribNames = lambda: ["b", "a", "c"]  # type: ignore[attr-defined]
    described: Dict[str, Any] = describe_work_item(item, attribute_limit=2)
    json.dumps(described)
    assert described["name"] == "cache_3"
    assert described["index"] == 3 and described["frame"] == 1.5
    assert described["is_batch"] is False and described["output_dir"] == "/proj/out"
    assert described["node_name"] == "cache"
    assert described["state"] == "workItemState.Scheduled"
    assert described["expected_output_files"] == [
        {"path": "__PDG_DIR__/a", "tag": "file/geo", "local_path": "/proj/a"},
        {"path": "/same", "tag": "file/geo"},
        {"path": "/untagged", "tag": ""},
    ]
    assert described["input_files"] == [{"path": "/in", "tag": "file/geo"}]
    assert described["attribute_names"] == ["a", "b"] and described["attribute_count"] == 3
    assert described["environment"] == {"wedgeindex": "2"}
    assert described["dependencies"] == ["raw", "unnamed", "up_0"]
    assert "dependents" not in described
    assert "priority" not in described


def test_describing_never_raises() -> None:
    """Describing an unreadable or odd work item returns what it could read, and never raises."""
    assert describe_work_item(_Raises()) == {
        "input_files": [],
        "expected_input_files": [],
        "output_files": [],
        "expected_output_files": [],
    }
    assert isinstance(describe_work_item(None), dict)

    class Odd:
        """A work item whose attributes have the wrong types."""

        environment: int = 5
        dependencies: int = 7
        name: str = "odd_0"

        def attribNames(self) -> List[str]:
            raise RuntimeError

    described: Dict[str, Any] = describe_work_item(Odd())
    assert "environment" not in described and "dependencies" not in described
    assert described["name"] == "odd_0"
    # A negative limit lists no attribute names but still counts them.
    item: _WorkItem = _WorkItem(_PdgNode("n"), 0)
    item.environment = None  # type: ignore[assignment]
    item.attribNames = lambda: ["a"]  # type: ignore[attr-defined]
    capped: Dict[str, Any] = describe_work_item(item, attribute_limit=-1)
    assert (capped["attribute_names"], capped["attribute_count"]) == ([], 1)
    assert "environment" not in capped


# -- what validation reads from a node that is not a step ------------------------


class _CodeTemplate(_Template):
    """A string parm template carrying HOM's tags, such as ``editorlang``."""

    def __init__(self, tags: Dict[str, str], string_kind: str = "Regular") -> None:
        super().__init__("String", string_kind)
        self._tags = tags

    def tags(self) -> Dict[str, str]:
        return dict(self._tags)


class _ExpressionParm(_Parm):
    """A parm driven by a channel expression, or a code parm when ``tags`` is given.

    ``kind`` is the string type, so a file-reference parm can be driven too.
    """

    def __init__(
        self,
        name: str,
        *,
        expression: str = "",
        raw: str = "",
        tags: Optional[Dict[str, str]] = None,
        kind: str = "Regular",
    ) -> None:
        super().__init__(name, raw, raw, kind=kind)
        self._expression = expression
        self._template = _CodeTemplate(tags or {}, kind)

    def expression(self) -> str:
        # HOM raises when the parm has no channel expression.
        if not self._expression:
            raise RuntimeError("no expression")
        return self._expression


class _LockedHom(_Hom):
    """A TOP node inside a locked digital asset."""

    def isInsideLockedHDA(self) -> bool:
        return True


def _middle_parms() -> List[Any]:
    """Expression, backtick, Python code and plain parms, and four kinds of file parm."""
    return [
        _ExpressionParm("intvalue11", expression='npoints(pdginput(0, "", 0))'),
        _ExpressionParm("label", raw="`@pdg_name`"),
        _ExpressionParm("generate", raw="open(path)\n", tags={"editorlang": "python"}),
        _ExpressionParm("note", raw="plain text"),
        # A file parm that evaluates to nothing outside a work item, one driven by
        # an expression, one the scene walk ignores, and an empty one.
        _file("filepath", "", raw="`@pdg_input`"),
        _ExpressionParm("outfile", expression='pdgoutput(0, "", 0)', kind="FileReference"),
        _file("pdg_workingdir", "/proj", raw="$HIP"),
        _file("unused", ""),
    ]


def test_expressions_file_parms_and_python_code_are_read_only_for_a_node_with_no_job_script() -> (
    None
):
    """A node with no job script carries its expressions, file parms and Python code, and a step none.

    The in-process check judges only nodes that are not steps, so a step's parms
    would be read for nothing. A file parm is kept by its authored text, so one
    that evaluates to nothing outside a work item is still seen.
    """
    # The same four parms on a node with no job script and on a step.
    middle: _PdgNode = _PdgNode("middle")
    step: _PdgNode = _PdgNode("step", command="hython rop.py")
    _topnet(
        _wire(middle, "/obj/topnet1/middle", parms=_middle_parms()),
        _wire(step, "/obj/topnet1/step", parms=_middle_parms()),
    )
    infos: Dict[str, NodeInfo] = _by_name(node_infos([middle, step], {}))
    assert infos["middle"].expressions == (
        ("intvalue11", 'npoints(pdginput(0, "", 0))'),
        ("label", "`@pdg_name`"),
        ("filepath", "`@pdg_input`"),
        ("outfile", 'pdgoutput(0, "", 0)'),
    )
    assert infos["middle"].file_parms == (
        ("filepath", "`@pdg_input`"),
        ("outfile", 'pdgoutput(0, "", 0)'),
    )
    assert infos["middle"].python_code == (("generate", "open(path)\n"),)
    assert (infos["step"].expressions, infos["step"].file_parms, infos["step"].python_code) == (
        (),
        (),
        (),
    )


def test_a_node_inside_a_locked_asset_is_marked() -> None:
    """A node inside a locked asset is marked, and an ordinary node or one with no TOP node is not."""
    # A sort asset's inner switch, a plain merge, and a PDG node with no TOP node.
    inner: _PdgNode = _PdgNode("sort1_switch")
    plain: _PdgNode = _PdgNode("merge")
    loose: _PdgNode = _PdgNode("loose")
    hom: _LockedHom = _LockedHom("/obj/topnet1/sort1/switch", category="Top", pdg=inner)
    inner.hom = hom
    _topnet(hom, _wire(plain, "/obj/topnet1/merge"))
    infos: Dict[str, NodeInfo] = _by_name(node_infos([inner, plain, loose], {}))
    assert [infos[n].in_locked_asset for n in ("sort1_switch", "merge", "loose")] == [
        True,
        False,
        False,
    ]
