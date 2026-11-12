# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Small stand-ins for ``hou`` and ``pdg``, for the tests of the Houdini host and the registration module.

Neither module exists outside Houdini. Importing this module installs a fake
``pdg`` when there is none, and the ROP tests' fake ``hou``. Each test then sets
the scene it needs on :class:`FakeHou`, which writes onto that shared ``hou``.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

# The ROP tests' fake ``hou``. The submitter modules bind ``hou`` when they are
# first imported, so every test in a process must share this one module.
import deadline_submitter_for_houdini.mock_hou  # type: ignore[import-not-found]  # noqa: F401


def _install_pdg() -> types.ModuleType:
    """The fake ``pdg`` module, with the enums and base class the scheduler code names."""
    # Reuse a fake another test module installed, so every test sees one module.
    existing: Optional[types.ModuleType] = sys.modules.get("pdg")
    if existing is not None and hasattr(existing, "scheduleResult"):
        return existing
    module: types.ModuleType = types.ModuleType("pdg")
    module.scheduleResult = types.SimpleNamespace(  # type: ignore[attr-defined]
        Succeeded="Succeeded", Deferred="Deferred", CookFailed="CookFailed"
    )
    module.tickResult = types.SimpleNamespace(  # type: ignore[attr-defined]
        SchedulerReady="SchedulerReady", SchedulerCancelCook="SchedulerCancelCook"
    )
    module.EventType = types.SimpleNamespace(  # type: ignore[attr-defined]
        NodeGenerated="NodeGenerated"
    )
    scheduler: types.ModuleType = types.ModuleType("pdg.scheduler")
    scheduler.PyScheduler = FakePyScheduler  # type: ignore[attr-defined]
    module.scheduler = scheduler  # type: ignore[attr-defined]
    sys.modules["pdg"] = module
    sys.modules["pdg.scheduler"] = scheduler
    return module


class FakePyScheduler:
    """PDG's ``PyScheduler`` base: what the scheduler forwards to PDG, recorded in ``calls``."""

    def __init__(self, scheduler: Any = None, name: str = "deadlinecloudscheduler1") -> None:
        self.name: str = name
        self.calls: List[Tuple[Any, ...]] = []
        self.context: Any = None
        self.node: Any = None
        self.dependency_graph: Any = ({}, {}, [])

    def topNode(self) -> Any:
        return self.node

    def dependencyGraph(self, expand: bool) -> Any:
        self.calls.append(("dependencyGraph", expand))
        return self.dependency_graph

    def setWorkingDir(self, local: str, remote: str) -> None:
        self.calls.append(("setWorkingDir", local, remote))

    def cookError(self, message: str) -> None:
        self.calls.append(("cookError", message))

    def cookWarning(self, message: str) -> None:
        self.calls.append(("cookWarning", message))

    def onWorkItemStartCook(self, *args: Any) -> None:
        self.calls.append(("onWorkItemStartCook",) + args)

    def onWorkItemAddOutput(self, *args: Any) -> None:
        self.calls.append(("onWorkItemAddOutput",) + args)

    def onWorkItemSucceeded(self, *args: Any) -> None:
        self.calls.append(("onWorkItemSucceeded",) + args)

    def onWorkItemFailed(self, *args: Any) -> None:
        self.calls.append(("onWorkItemFailed",) + args)


PDG: types.ModuleType = _install_pdg()


# -- HOM ----------------------------------------------------------------------


class _Named:
    """Anything HOM exposes through ``name()``, such as a category or a parm type."""

    def __init__(self, name: str) -> None:
        self._name: str = name

    def name(self) -> str:
        return self._name


class _Template:
    """A ``hou.ParmTemplate``: String with a string type, or another type."""

    def __init__(self, kind: str) -> None:
        self._kind: str = kind

    def type(self) -> _Named:
        return _Named("String" if self._kind else "Int")

    def stringType(self) -> _Named:
        return _Named(self._kind)


class FakeParm:
    """A ``hou.Parm``. ``kind`` is its string type, such as ``FileReference``, or ``""`` for a number."""

    def __init__(
        self,
        node: "FakeNode",
        name: str,
        value: Any,
        kind: str = "",
        menu: Sequence[str] = (),
        labels: Sequence[str] = (),
    ) -> None:
        self._node: FakeNode = node
        self._name: str = name
        self.value: Any = value
        self.kind: str = kind
        self.sets: List[Any] = []
        #: A menu parm's tokens. Houdini's eval gives the chosen index, and evalAsString the token.
        self.menu: Tuple[str, ...] = tuple(menu)
        #: The menu's labels, in token order, for a parm read by its label.
        self.labels: Tuple[str, ...] = tuple(labels)

    def name(self) -> str:
        return self._name

    def path(self) -> str:
        return f"{self._node.path()}/{self._name}"

    def node(self) -> "FakeNode":
        return self._node

    def parmTemplate(self) -> _Template:
        return _Template(self.kind)

    def eval(self) -> Any:
        # A parm set to an exception stands for one whose expression fails.
        if isinstance(self.value, Exception):
            raise self.value
        if self.menu:
            return self.menu.index(self.value)
        return expand(str(self.value)) if self.kind else self.value

    def evalAsString(self) -> str:
        return self.value if self.menu else expand(str(self.eval()))

    def menuItems(self) -> Tuple[str, ...]:
        return self.menu

    def menuLabels(self) -> Tuple[str, ...]:
        return self.labels

    def unexpandedString(self) -> str:
        return str(self.value)

    def set(self, value: Any) -> None:
        self.sets.append(value)
        self.value = value


class FakeNode:
    """A ``hou.Node`` with only what the host, the readers, and the ROP's scan call."""

    def __init__(
        self,
        path: str,
        *,
        parms: Optional[Dict[str, Any]] = None,
        category: str = "Top",
        type_name: str = "node",
    ) -> None:
        self._path: str = path
        self._parent: Optional[FakeNode] = None
        self._children: List[FakeNode] = []
        self._inputs: List[FakeNode] = []
        self._category: str = category
        self._type_name: str = type_name
        self._parms: Dict[str, FakeParm] = {
            name: FakeParm(self, name, value) for name, value in (parms or {}).items()
        }
        self.pdg_node: Any = None
        self.context: Any = None
        self.child_category: str = "Top"
        self.cook_calls: List[Dict[str, Any]] = []
        #: The Submit stops each refused cook start took, by scheduler node path.
        self.stop_cooks: List[Dict[str, Tuple[str, str]]] = []
        self.spare: List[FakeParm] = []
        self.display: Optional[FakeNode] = None
        self.cook_creates: Any = None
        self.pdg_name: Optional[str] = None
        #: Each static generate, with its ``block`` argument.
        self.generate_calls: List[Dict[str, Any]] = []
        #: Run during a static generate, as PDG sends its events while it generates.
        self.on_generate: Optional[Callable[[], None]] = None
        #: Each dirty and generate on the node's work items, in order, with its argument.
        self.work_item_calls: List[Tuple[str, bool]] = []
        #: The node's errors, as ``errors()`` reports them.
        self.node_errors: List[str] = []

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def parent(self) -> Optional["FakeNode"]:
        return self._parent

    def add(self, child: "FakeNode", *inputs: "FakeNode") -> "FakeNode":
        # Parent and wire ``child`` the way Houdini does.
        child._parent = self
        child._inputs = list(inputs)
        self._children.append(child)
        return child

    def children(self) -> List["FakeNode"]:
        return list(self._children)

    def allSubChildren(self) -> List["FakeNode"]:
        # Every node inside this one, at any depth, as Houdini lists them.
        return [node for child in self._children for node in (child, *child.allSubChildren())]

    def inputs(self) -> List["FakeNode"]:
        return list(self._inputs)

    def inputAncestors(self) -> List["FakeNode"]:
        # Every node wired above this one, each once, nearest first, as Houdini lists them.
        found: List[FakeNode] = []
        queue: List[Optional[FakeNode]] = list(self._inputs)
        while queue:
            node: Optional[FakeNode] = queue.pop(0)
            if node is not None and node not in found:
                found.append(node)
                queue.extend(node._inputs)
        return found

    def parms(self) -> List[FakeParm]:
        return list(self._parms.values())

    def parm(self, name: str) -> Optional[FakeParm]:
        return self._parms.get(name)

    def set_parm(
        self,
        name: str,
        value: Any,
        kind: str = "",
        menu: Sequence[str] = (),
        labels: Sequence[str] = (),
    ) -> FakeParm:
        """Add or replace a parm, and return it. A ``menu`` makes it an ordinal menu set to ``value``."""
        parm: FakeParm = FakeParm(self, name, value, kind, menu, labels)
        self._parms[name] = parm
        return parm

    def remove_parm(self, name: str) -> None:
        """Remove a parm, as on a node type that has no such parm."""
        self._parms.pop(name, None)

    def spareParms(self) -> List[FakeParm]:
        return list(self.spare)

    def childTypeCategory(self) -> _Named:
        return _Named(self.child_category)

    def type(self) -> Any:
        return types.SimpleNamespace(
            category=lambda: _Named(self._category), name=lambda: self._type_name
        )

    def node(self, ref: str) -> Any:
        """The node ``ref`` names, absolute from the root or relative to this node, or None."""
        if not ref:
            return None
        if not ref.startswith("/"):
            return self._find_child(ref.split("/"))
        # The fake's root is ``/obj``, so an absolute path starts with its name.
        root: FakeNode = self._root()
        parts: List[str] = ref.split("/")
        return root._find_child(parts[2:]) if parts[1:2] == [root.name()] else None

    def _root(self) -> "FakeNode":
        """The node at the top of this node's parents."""
        return self if self._parent is None else self._parent._root()

    def _find_child(self, parts: List[str]) -> Optional["FakeNode"]:
        """Follow ``parts`` from this node, where ``..`` is the parent, as Houdini does."""
        if not parts:
            return self
        # Take one step: stay, go up, or go to the child of that name.
        head: str = parts[0]
        step: Optional[FakeNode] = (
            self
            if head in ("", ".")
            else (
                self._parent
                if head == ".."
                else next((c for c in self._children if c.name() == head), None)
            )
        )
        return step._find_child(parts[1:]) if step is not None else None

    def displayNode(self) -> Optional["FakeNode"]:
        return self.display

    def isInsideLockedHDA(self) -> bool:
        return False

    def errors(self) -> List[str]:
        return list(self.node_errors)

    def getPDGNode(self) -> Any:
        return self.pdg_node

    def getPDGNodeName(self) -> Optional[str]:
        return self.pdg_name

    def getPDGGraphContext(self) -> Any:
        return self.context

    def generateStaticWorkItems(self, block: bool = False) -> None:
        if self.on_generate is not None:
            self.on_generate()
        self.generate_calls.append({"block": block})
        self.work_item_calls.append(("generate", block))

    def dirtyAllWorkItems(self, remove_outputs: bool) -> None:
        self.work_item_calls.append(("dirty", remove_outputs))

    def cookWorkItems(self, **kwargs: Any) -> None:
        # A waiting Submit stop is taken and the cook refused, as the scheduler's onStartCook does.
        from deadline.houdini_pdg.houdini.host import submit_stops

        if submit_stops:
            self.stop_cooks.append({path: stop[1:] for path, stop in submit_stops.items()})
            submit_stops.clear()
            raise OperationFailed("Failed to start scheduler")
        self.cook_calls.append(kwargs)
        if self.cook_creates is not None and kwargs.get("tops_only"):
            self.context = self.cook_creates


class OperationFailed(Exception):
    """``hou.OperationFailed``, which a refused cook start raises."""


class FakeHipFile:
    """``hou.hipFile``: the scene's path, whether it is new or unsaved, and how often it was saved."""

    def __init__(self, path: str) -> None:
        self._path: str = path
        self.new: bool = False
        self.unsaved: bool = False
        self.saves: int = 0

    def path(self) -> str:
        return self._path

    def isNewFile(self) -> bool:
        return self.new

    def hasUnsavedChanges(self) -> bool:
        return self.unsaved

    def save(self) -> None:
        self.saves += 1
        self.unsaved = False


#: The scene folder ``$HIP`` expands to, set by :class:`FakeHou`.
_HIP_DIR: Dict[str, str] = {"value": os.sep}


def expand(text: str) -> str:
    """Houdini's expansion, for the variables the tests use: ``$HIP``, ``$F4``, and the environment."""
    # Frame 1, as a scene at its first frame evaluates ``$F4``.
    hip: str = text.replace("${HIP}", _HIP_DIR["value"]).replace("$HIP", _HIP_DIR["value"])
    return os.path.expandvars(hip.replace("$F4", "0001"))


class FakeMessages:
    """``hou.ui.displayMessage``: answers from ``answers`` in turn, then ``choice``, and records each call.

    An answer of None closes the window, which returns the call's ``close_choice``. A test can
    set ``on_show`` to run while the window is open, as Houdini handles its queue then.
    """

    def __init__(self) -> None:
        self.on_show: Optional[Callable[[], None]] = None
        self.choice: Optional[int] = 0
        self.answers: List[Optional[int]] = []
        self.calls: List[Tuple[str, Tuple[str, ...]]] = []
        self.options: List[Dict[str, Any]] = []

    def __call__(self, text: str, **kwargs: Any) -> int:
        # Record the text, the buttons, and every other option, then answer.
        self.calls.append((text, tuple(kwargs.get("buttons", ()))))
        self.options.append(kwargs)
        if self.on_show is not None:
            self.on_show()
        answer: Optional[int] = self.answers.pop(0) if self.answers else self.choice
        return int(kwargs.get("close_choice", -1)) if answer is None else answer


@dataclass
class FakeHou:
    """The scene a test sets up, written onto the shared fake ``hou`` module."""

    hip_file: FakeHipFile
    messages: FakeMessages
    ui: bool = True
    file_references: List[Tuple[Any, str]] = field(default_factory=list)
    instances: List[Any] = field(default_factory=list)
    libraries: Dict[str, str] = field(default_factory=dict)
    #: The ``hou.ui.triggerUpdate`` calls, which nothing should make.
    updates: int = 0

    def trigger_update(self) -> None:
        """``hou.ui.triggerUpdate``: count the call."""
        self.updates += 1


def install_hou(monkeypatch: pytest.MonkeyPatch, hip_file: str) -> FakeHou:
    """Point the shared fake ``hou`` at a scene saved as ``hip_file``, for one test."""
    hou: Any = sys.modules["hou"]
    _HIP_DIR["value"] = os.path.dirname(hip_file)
    state: FakeHou = FakeHou(hip_file=FakeHipFile(hip_file), messages=FakeMessages())

    def node_type(category: str, name: str) -> Any:
        # A node type's instances, and the library that defines it, when a test set one.
        path: Optional[str] = state.libraries.get(name)
        definition: Any = (
            types.SimpleNamespace(libraryFilePath=lambda: path) if path is not None else None
        )
        return types.SimpleNamespace(
            instances=lambda: tuple(state.instances), definition=lambda: definition
        )

    # Each attribute reads the state, so a test can change it after this call.
    attributes: Dict[str, Any] = {
        "hipFile": state.hip_file,
        "text": types.SimpleNamespace(expandString=expand),
        "ui": types.SimpleNamespace(
            displayMessage=state.messages, triggerUpdate=state.trigger_update
        ),
        "isUIAvailable": lambda: state.ui,
        "OperationFailed": OperationFailed,
        "fileReferences": lambda: list(state.file_references),
        "getenv": lambda name: os.environ.get(name),
        "nodeType": node_type,
        "topNodeTypeCategory": lambda: "Top",
        "ropNodeTypeCategory": lambda: "Driver",
        "applicationVersionString": lambda: "21.0.596",
        "ToggleParmTemplate": type("ToggleParmTemplate", (), {}),
        "severityType": types.SimpleNamespace(Error="Error", Message="Message"),
        "parmTemplateType": types.SimpleNamespace(Folder="Folder", String="String"),
    }
    for name, value in attributes.items():
        monkeypatch.setattr(hou, name, value, raising=False)
    return state


# -- PDG ----------------------------------------------------------------------


@dataclass
class FakePdgNode:
    """A ``pdg.Node``: its job script, type, scheduler, and its TOP node."""

    name: str
    command: str = ""
    node_type: str = "Processor"
    isDynamic: bool = False
    scheduler_name: str = "deadlinecloudscheduler1"
    hom: Any = None
    workItems: List[Any] = field(default_factory=list)
    #: None stands for the Automatic every node reports before generation.
    cookType: Any = None

    @property
    def scriptInfo(self) -> Any:
        return types.SimpleNamespace(command=self.command)

    @property
    def nodeType(self) -> Any:
        return types.SimpleNamespace(name=self.node_type)

    @property
    def scheduler(self) -> Any:
        return types.SimpleNamespace(name=self.scheduler_name)

    def topNode(self) -> Any:
        return self.hom


@dataclass
class FakeContext:
    """A PDG graph context: its nodes, the cook set, and its event handlers."""

    nodes: List[FakePdgNode]
    cookSet: Any = None
    handlers: List[Tuple[Any, Any]] = field(default_factory=list)
    items: Dict[int, Any] = field(default_factory=dict)
    #: The schedulers ``schedulerForName`` finds, by name.
    schedulers: Dict[str, Any] = field(default_factory=dict)
    #: PDG's cook options. Houdini prints the cook type as ``cookType.Node`` for a cook.
    cookOptions: Any = field(
        default_factory=lambda: types.SimpleNamespace(cookType="cookType.Node")
    )
    #: Set once anything cancels the cook, from any thread.
    canceled: threading.Event = field(default_factory=threading.Event)

    @property
    def graph(self) -> Any:
        return types.SimpleNamespace(
            nodes=lambda: list(self.nodes), workItemById=lambda item_id: self.items.get(item_id)
        )

    def schedulerForName(self, name: str) -> Any:
        return self.schedulers.get(name)

    def addEventHandler(self, function: Any, event_type: Any, is_python: bool) -> Any:
        # PDG calls only the handlers registered as Python back into Python.
        handler: Tuple[Any, Any] = (function, event_type)
        if is_python:
            self.handlers.append(handler)
        return handler

    def removeEventHandler(self, handler: Any) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)

    def cancelCook(self) -> None:
        self.canceled.set()

    def emit(self, event_type: Any, node_name: str) -> None:
        """Send ``event_type`` for the named node to every handler registered for it."""
        node: FakePdgNode = next(n for n in self.nodes if n.name == node_name)
        for function, kind in list(self.handlers):
            if kind == event_type:
                function(None, types.SimpleNamespace(type=kind, node=node))


SCHEDULER_PATH: str = "/obj/topnet1/deadlinecloudscheduler1"


@dataclass
class FakeNetwork:
    """A TOP network, its scheduler node, and PDG's view of both."""

    topnet: FakeNode
    scheduler_node: FakeNode
    pdg_nodes: List[FakePdgNode]
    context: FakeContext

    def scheduler(self) -> FakePyScheduler:
        """The PDG scheduler object for the network's scheduler node."""
        scheduler: FakePyScheduler = FakePyScheduler()
        scheduler.context = self.context
        scheduler.node = self.scheduler_node
        return scheduler

    def node(self, name: str) -> FakePdgNode:
        """The PDG node called ``name``."""
        return next(n for n in self.pdg_nodes if n.name == name)


def build_network(
    spec: Sequence[Tuple[Any, ...]],
    *,
    scheduler_parms: Optional[Dict[str, Any]] = None,
) -> FakeNetwork:
    """``spec`` rows are (name, command, PDG node type, input names[, cook type])."""
    obj: FakeNode = FakeNode("/obj", category="Object")
    obj.child_category = "Object"
    topnet: FakeNode = obj.add(FakeNode("/obj/topnet1", category="Object"))
    homs: Dict[str, FakeNode] = {}
    pdg_nodes: List[FakePdgNode] = []
    # One TOP node and one PDG node per row, wired to the rows it names.
    for row in spec:
        name, command, node_type, inputs = row[:4]
        cook_type: Any = row[4] if len(row) > 4 else None
        hom: FakeNode = topnet.add(FakeNode(f"/obj/topnet1/{name}"), *[homs[i] for i in inputs])
        node: FakePdgNode = FakePdgNode(
            name=name, command=command, node_type=node_type, hom=hom, cookType=cook_type
        )
        hom.pdg_node = node
        homs[name] = hom
        pdg_nodes.append(node)
    parms: Dict[str, Any] = (
        {"working_dir": "$HIP", "debug_level": 0, "validate_graph": 1}
        if scheduler_parms is None
        else scheduler_parms
    )
    scheduler_node: FakeNode = topnet.add(FakeNode(SCHEDULER_PATH, parms=parms))
    # The network cooks on this scheduler by default, and its last node is displayed.
    topnet.set_parm("topscheduler", "deadlinecloudscheduler1")
    topnet.display = homs[spec[-1][0]] if spec else None
    context: FakeContext = FakeContext(nodes=pdg_nodes)
    scheduler_node.context = context
    # PDG creates the graph context when a never-cooked network cooks its TOP nodes only.
    scheduler_node.cook_creates = context
    return FakeNetwork(topnet, scheduler_node, pdg_nodes, context)


#: cooking_example's shape: an in-process Python Script between two steps.
COOKING_EXAMPLE: List[Tuple[Any, ...]] = [
    ("subdivide", "hython subdivide.py", "Processor", []),
    ("python1", "", "Processor", ["subdivide"], "InProcess"),
    ("render", "hython render.py", "Processor", ["python1"]),
]

#: Pyro Bonfire's shape: a wedge root, then steps, joined by a partitioner.
PYRO_BONFIRE: List[Tuple[Any, ...]] = [
    ("wedge1", "", "Processor", []),
    ("sim", "hython sim.py", "Processor", ["wedge1"]),
    ("render", "hython render.py", "Processor", ["sim"]),
    ("partition", "", "Partitioner", ["render"]),
    ("montage", "magick montage", "Processor", ["partition"]),
]
