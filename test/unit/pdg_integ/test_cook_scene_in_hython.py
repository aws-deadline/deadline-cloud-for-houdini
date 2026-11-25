# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the parts of the hython cook script that can run without Houdini.

They cover how the script reads the cook's refusal lines, finds the cook's debug
directory, sets up the scheduler node, reads the node's errors, and copies PDG's work
items. Houdini's node and PDG objects are small stand-ins with only what the script uses.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Tuple

import pytest

from test.integ.pdg.cook_scene_in_hython import (
    RefusalLineHandler,
    capture_refusal_lines,
    find_debug_dir,
    find_or_create_scheduler,
    read_pdg_work_items,
    read_scheduler_errors,
    set_scheduler_parms,
)

# The format both the scheduler and the cook log a refusal with.
_REFUSAL_LINE: str = "Refused, %s (%s): %s %s"


@pytest.fixture
def refusal_lines() -> Iterator[RefusalLineHandler]:
    """A handler collecting refusal lines, removed from the package's logger afterwards."""
    handler: RefusalLineHandler = capture_refusal_lines()
    yield handler
    logging.getLogger("deadline.houdini_pdg").removeHandler(handler)


def test_refusals_from_the_scheduler_and_the_cook_are_both_collected(
    refusal_lines: RefusalLineHandler,
) -> None:
    """A refusal is caught whichever module logs it, at the cook's start or inside the cook."""
    logging.getLogger("deadline.houdini_pdg.registration").error(
        _REFUSAL_LINE, "/obj/topnet1/rop1", "rop_batch", "detail", "remedy"
    )
    logging.getLogger("deadline.houdini_pdg.cook").error(
        _REFUSAL_LINE, "service1", "service_node", "detail", "remedy"
    )
    assert refusal_lines.refusals == [
        {"scenario": "rop_batch", "subject": "/obj/topnet1/rop1"},
        {"scenario": "service_node", "subject": "service1"},
    ]


def test_a_subject_with_parentheses_keeps_its_scenario(refusal_lines: RefusalLineHandler) -> None:
    """The scenario comes from the line's arguments, so a subject like "a (b)" can't be misread."""
    logging.getLogger("deadline.houdini_pdg.cook").error(
        _REFUSAL_LINE, "/obj/geo (copy) parm 'file'", "local_file_unreachable", "d", "r"
    )
    assert refusal_lines.refusals == [
        {"scenario": "local_file_unreachable", "subject": "/obj/geo (copy) parm 'file'"}
    ]


def test_other_lines_are_not_refusals(refusal_lines: RefusalLineHandler) -> None:
    """Errors and advisories that aren't refusal lines are left out."""
    # Two errors in other formats, and an advisory, which is logged as a warning.
    logger: logging.Logger = logging.getLogger("deadline.houdini_pdg.cook")
    logger.error("onSchedule failed")
    logger.error("Refused, but not in the refusal format")
    logger.warning("%s (%s): %s", "write_summary", "in_process_node", "an advisory")
    assert refusal_lines.refusals == []


def test_no_debug_directory_is_none(tmp_path: Path) -> None:
    """A cook refused at its start writes no debug directory."""
    assert find_debug_dir(str(tmp_path)) is None
    (tmp_path / "pdg_debug").mkdir()
    assert find_debug_dir(str(tmp_path)) is None


def test_the_one_debug_directory_is_found(tmp_path: Path) -> None:
    """The cook's own directory is found, and stray files beside it are ignored."""
    # One cook's directory and a file next to it.
    cook_dir: Path = tmp_path / "pdg_debug" / "2026-11-10-17-09-52-1ad65a51"
    cook_dir.mkdir(parents=True)
    (tmp_path / "pdg_debug" / "notes.txt").write_text("not a cook")
    assert find_debug_dir(str(tmp_path)) == str(cook_dir)


def test_two_debug_directories_are_refused(tmp_path: Path) -> None:
    """Two cooks in one working directory can't be told apart, so the test says how to fix it."""
    names: List[str] = ["2026-11-10-17-09-52-1ad65a51", "2026-11-10-18-00-00-2be76b62"]
    for name in names:
        (tmp_path / "pdg_debug" / name).mkdir(parents=True)
    with pytest.raises(ValueError, match="holds 2 cooks' debug directories"):
        find_debug_dir(str(tmp_path))


# Stand-ins for the few Houdini node, parm and PDG objects the script reads and sets.


class _Parm:
    """A parm that keeps the last value set."""

    def __init__(self, value: Any = "") -> None:
        self.value: Any = value

    def set(self, value: Any) -> None:
        """Set the value."""
        self.value = value

    def eval(self) -> Any:
        """The value."""
        return self.value


class _NodeType:
    """A node type, known by its name."""

    def __init__(self, name: str) -> None:
        self._name: str = name

    def name(self) -> str:
        """The type's name."""
        return self._name


class _Node:
    """A node with a type, a name and parms."""

    def __init__(self, type_name: str, name: str, parms: Iterable[str] = ()) -> None:
        self._type: _NodeType = _NodeType(type_name)
        self._name: str = name
        self.parms: Dict[str, _Parm] = {p: _Parm() for p in parms}

    def type(self) -> _NodeType:
        """The node's type."""
        return self._type

    def name(self) -> str:
        """The node's name."""
        return self._name

    def parm(self, name: str) -> _Parm:
        """One of the node's parms."""
        return self.parms[name]


class _TopNetwork(_Node):
    """A TOP network that holds nodes and can create more."""

    def __init__(self, children: List[_Node]) -> None:
        super().__init__("topnet", "topnet1", parms=["topscheduler"])
        self._children: List[_Node] = children

    def children(self) -> Tuple[_Node, ...]:
        """The nodes in the network."""
        return tuple(self._children)

    def createNode(self, type_name: str) -> _Node:
        """A new node of the type, named as Houdini names a first one."""
        node: _Node = _Node(type_name, f"{type_name}1")
        self._children.append(node)
        return node


def test_the_networks_own_scheduler_node_is_kept_and_made_its_scheduler() -> None:
    """A scene's own Deadline Cloud Scheduler node keeps its settings, and the network uses it.

    The network may have named another scheduler, such as a Local Scheduler.
    """
    # The network names its Local Scheduler, and also holds a Deadline Cloud Scheduler node.
    own: _Node = _Node("deadlinecloudscheduler", "farm")
    topnet: _TopNetwork = _TopNetwork([_Node("localscheduler", "localscheduler"), own])
    topnet.parm("topscheduler").set("localscheduler")

    # That node is used and made the network's scheduler, and no node is added.
    assert find_or_create_scheduler(topnet) is own
    assert topnet.parm("topscheduler").eval() == "farm"
    assert len(topnet.children()) == 2


def test_a_network_without_the_scheduler_node_gets_one() -> None:
    """A scene with only a Local Scheduler, or a scheduler whose type isn't installed, gets a new
    node to cook with.
    """
    # The network holds only a scheduler node of a type this Houdini doesn't have.
    topnet: _TopNetwork = _TopNetwork([_Node("uninstalledscheduler", "other")])

    scheduler: Any = find_or_create_scheduler(topnet)

    assert scheduler.type().name() == "deadlinecloudscheduler"
    assert topnet.parm("topscheduler").eval() == scheduler.name()


_SCHEDULER_PARMS: Tuple[str, ...] = (
    "name",
    "debug_level",
    "include_adaptor_wheels",
    "adaptor_wheels",
)


def test_the_job_name_debug_level_and_wheels_are_set_on_the_scheduler_node() -> None:
    """The workers install the wheels, so they run this checkout's code, not a released adaptor."""
    scheduler: _Node = _Node("deadlinecloudscheduler", "farm", parms=_SCHEDULER_PARMS)
    set_scheduler_parms(scheduler, job_name="pdg-integ-a-0a1b2c3d", debug_level=1, wheels="/w")
    assert {name: parm.value for name, parm in scheduler.parms.items()} == {
        "name": "pdg-integ-a-0a1b2c3d",
        "debug_level": 1,
        "include_adaptor_wheels": 1,
        "adaptor_wheels": "/w",
    }


def test_without_wheels_the_scenes_own_wheel_settings_are_kept() -> None:
    """With no wheels folder, the node's wheel parms are left as the scene has them."""
    scheduler: _Node = _Node("deadlinecloudscheduler", "farm", parms=_SCHEDULER_PARMS)
    set_scheduler_parms(scheduler, job_name="pdg-integ-a-0a1b2c3d", debug_level=0, wheels="")
    assert (
        scheduler.parms["include_adaptor_wheels"].value,
        scheduler.parms["adaptor_wheels"].value,
    ) == (
        "",
        "",
    )


class _File:
    """A PDG output file."""

    def __init__(self, path: str, tag: str) -> None:
        self.path: str = path
        self.tag: str = tag


class _WorkItem:
    """A PDG work item whose paths may hold PDG's __PDG_DIR__ token."""

    def __init__(self, name: str, index: int, state: str, outputs: List[_File]) -> None:
        self.name: str = name
        self.index: int = index
        self.state: str = state
        self.outputFiles: List[_File] = outputs

    def localizePath(self, path: str) -> str:
        """The path with the token replaced by this machine's working directory."""
        return path.replace("__PDG_DIR__", "/cooks/pyro")


class _PdgNode:
    """A PDG node and its work items."""

    def __init__(self, name: str, work_items: List[_WorkItem]) -> None:
        self.name: str = name
        self.workItems: List[_WorkItem] = work_items


class _CookNode:
    """A TOP node, whose graph context holds every PDG node of its network."""

    def __init__(self, pdg_nodes: List[_PdgNode]) -> None:
        self._pdg_nodes: List[_PdgNode] = pdg_nodes

    def getPDGGraphContext(self) -> Any:
        """The graph context, with the graph's nodes."""
        graph: Any = type("Graph", (), {"nodes": lambda _self: self._pdg_nodes})()
        return type("Context", (), {"graph": graph})()


def test_every_work_item_is_recorded_with_its_state_name_and_local_outputs() -> None:
    """PDG's states are named as the comparison names them, and each output is a local path.

    A path left with __PDG_DIR__ in it would never match the uploaded file's path.
    """
    # Two nodes: one with an output under the working directory, one with none.
    render: _WorkItem = _WorkItem(
        "render_3", 0, "workItemState.CookedSuccess", [_File("__PDG_DIR__/r/a.png", "file/image")]
    )
    wedge: _WorkItem = _WorkItem("wedge_1", 0, "workItemState.CookedFail", [])
    cook_node: _CookNode = _CookNode([_PdgNode("render", [render]), _PdgNode("wedge", [wedge])])

    assert read_pdg_work_items(cook_node) == [
        {
            "node": "render",
            "name": "render_3",
            "index": 0,
            "state": "CookedSuccess",
            "outputs": [{"path": "/cooks/pyro/r/a.png", "tag": "file/image"}],
        },
        {"node": "wedge", "name": "wedge_1", "index": 0, "state": "CookedFail", "outputs": []},
    ]


class _EventsContext:
    """A graph context holding events that hand errors to the scheduler node until processed."""

    def __init__(self, scheduler: "_SchedulerWithEvents", pending: List[str]) -> None:
        self._scheduler: _SchedulerWithEvents = scheduler
        self._pending: List[str] = pending

    def waitAllEvents(self) -> None:
        """Process the events, which puts their errors on the node."""
        self._scheduler.shown += self._pending
        self._pending = []


class _SchedulerWithEvents:
    """A scheduler node that shows only the errors whose events were processed."""

    def __init__(self) -> None:
        self.shown: List[str] = []

    def errors(self) -> Tuple[str, ...]:
        """The errors on the node now."""
        return tuple(self.shown)


def test_the_scheduler_nodes_errors_are_read_after_pdgs_events() -> None:
    """A refusal at the cook's start reaches the node only through an event, so the error is
    read after the events are processed. Reading it straight away would find no error and
    fail every test of a scene refused at the start.
    """
    # The refusal's error is still an event when the cook returns.
    scheduler: _SchedulerWithEvents = _SchedulerWithEvents()
    context: _EventsContext = _EventsContext(scheduler, ["The graph was refused (1 refusal(s))"])
    cook_node: Any = type("CookNode", (), {"getPDGGraphContext": lambda _self: context})()

    assert read_scheduler_errors(cook_node, scheduler) == ["The graph was refused (1 refusal(s))"]
