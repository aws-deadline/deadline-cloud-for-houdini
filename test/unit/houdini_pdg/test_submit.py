# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler node's Submit button, against small fakes of ``hou`` and ``pdg``."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from .houdini_fakes import (
    PYRO_BONFIRE,
    COOKING_EXAMPLE,
    FakeHou,
    FakeNetwork,
    FakeNode,
    FakePdgNode,
    OperationFailed,
    build_network,
    install_hou,
)

from deadline.houdini_pdg.houdini import host
from deadline.houdini_pdg.houdini.host import submit


@pytest.fixture
def scene(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHou:
    """A saved scene at ``<tmp>/scene/shot.hip``, with a UI."""
    hip: str = os.path.join(str(tmp_path), "scene", "shot.hip")
    os.makedirs(os.path.dirname(hip))
    monkeypatch.delenv("DEADLINE_PDG_DEBUG_LEVEL", raising=False)
    monkeypatch.setenv("HIP", os.path.dirname(hip))
    # Houdini runs a deferred call once the button callback returns. Here it runs at once.
    monkeypatch.setitem(
        sys.modules, "hdefereval", types.SimpleNamespace(executeDeferred=lambda call: call())
    )
    # A stop no cook start took would otherwise reach the next test's cook.
    host.submit_stops.clear()
    return install_hou(monkeypatch, hip)


def _workstation(name: str) -> str:
    """A workstation with a default farm and queue."""
    values: Dict[str, str] = {"defaults.farm_id": "farm-1", "defaults.queue_id": "queue-1"}
    return values.get(name, "")


def _with_items(network: FakeNetwork) -> FakeNetwork:
    """Give each node one item, as a finished generate leaves it, so Submit judges the graph."""
    for node in network.pdg_nodes:
        node.workItems = [None]
    return network


def _network() -> FakeNetwork:
    """A Pyro Bonfire network whose scheduler node has every parm Submit reads."""
    return _with_items(
        build_network(
            PYRO_BONFIRE,
            scheduler_parms={
                "working_dir": "$HIP",
                "debug_level": 0,
                "validate_graph": 1,
                "auto_save_hip": 0,
                "name": "bonfire",
                "priority": 50,
            },
        )
    )


def _cooks(network: FakeNetwork) -> List[Dict[str, Any]]:
    """The cooks the display node was asked for."""
    display: Any = network.topnet.displayNode()
    return list(display.cook_calls)


def _parm_writes(network: FakeNetwork) -> List[Any]:
    """Every value written to any scheduler node parm."""
    return [value for parm in network.scheduler_node.parms() for value in parm.sets]


@pytest.mark.parametrize("answers", [[2], [0, 1], [0, None]])
def test_cancel_cooks_nothing_and_writes_nothing(
    scene: FakeHou, answers: List[Optional[int]]
) -> None:
    """Cancel at the save prompt or at the confirmation leaves the scene and the node as they were.

    Closing the confirmation is a cancel too, so a stray click never submits a job. With no
    advisory, a cancel sends no empty warning to the Warning tab and asks for no UI update.
    """
    scene.hip_file.unsaved = True
    scene.messages.answers = list(answers)
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    assert _cooks(network) == []
    assert _parm_writes(network) == []
    scheduler: Any = network.context.schedulers["deadlinecloudscheduler1"]
    assert [c for c in scheduler.calls if c[0] == "cookWarning"] == [] and scene.updates == 0


def test_enter_picks_save_on_the_save_prompt_and_submit_on_the_confirmation(
    scene: FakeHou,
) -> None:
    """Enter must never pick Always Save, which writes a parm, or Cancel instead of Submit."""
    # Submit an unsaved scene, then name the button Enter and the close box pick on each dialog.
    scene.hip_file.unsaved = True
    network: FakeNetwork = _network()
    submit(network.scheduler_node, get_setting=_workstation)
    picked: List[Tuple[str, str]] = [
        (buttons[options["default_choice"]], buttons[options["close_choice"]])
        for (_, buttons), options in zip(scene.messages.calls, scene.messages.options)
    ]
    assert picked == [("Save", "Cancel"), ("Submit", "Cancel")]


@pytest.mark.parametrize("default_scheduler", ["localscheduler", None])
def test_a_network_that_does_not_cook_on_this_scheduler_is_refused(
    scene: FakeHou, default_scheduler: Optional[str]
) -> None:
    """Cooking the display node uses the network's default scheduler, which could be another one.

    A scheduler inside a TOP subnet has a parent with no such parm, and Submit must say so, not crash.
    """
    # The network names another default scheduler, or has no such parm.
    network: FakeNetwork = _network()
    if default_scheduler is None:
        network.topnet.remove_parm("topscheduler")
    else:
        network.topnet.set_parm("topscheduler", default_scheduler)
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    assert _cooks(network) == []
    assert "Default TOP Scheduler" in scene.messages.calls[0][0]


def test_the_confirmation_shows_the_jobs_settings_and_any_warning(scene: FakeHou) -> None:
    """The artist confirms what will run, and a waived refusal is shown, not hidden.

    Priority 0 is a valid value, so it must show as 0 and not fall back to the default 50.
    """
    network: FakeNetwork = _with_items(
        build_network(
            COOKING_EXAMPLE,
            scheduler_parms={"working_dir": "$HIP", "validate_graph": 0, "priority": 0},
        )
    )
    assert submit(network.scheduler_node, get_setting=_workstation) is True
    text: str = scene.messages.calls[0][0]
    # With no name parm the job takes the scene's file name.
    assert "Job name: shot.hip" in text and "Priority: 0\n" in text
    assert "Farm: farm-1" in text and "Queue: queue-1" in text
    assert "python1" in scene.messages.options[0]["details"]


class _Item:
    """A generated ``pdg.WorkItem`` with a command, so its node becomes a step."""

    def __init__(self, node: str, index: int, command: str) -> None:
        # The fields the graph read takes from a work item.
        self.name: str = f"{node}_{index}"
        self.index: int = index
        self.id: int = 1000 + index
        self.node: Any = types.SimpleNamespace(name=node)
        self.command: str = command
        self.state: Any = types.SimpleNamespace(name="Uncooked")


def _generated(network: FakeNetwork, counts: Dict[str, int]) -> None:
    """Give the scheduler the items a static generate makes, ``counts[node]`` for each step."""
    # PDG keeps the items on the scheduler, which Submit finds by its name.
    commands: Dict[str, str] = {node.name: node.command for node in network.pdg_nodes}
    items: Dict[Any, List[Any]] = {
        _Item(name, i, commands[name]): [] for name, count in counts.items() for i in range(count)
    }
    scheduler: Any = network.scheduler()
    scheduler.dependency_graph = (items, {}, [])
    network.context.schedulers["deadlinecloudscheduler1"] = scheduler


def test_a_network_over_a_shape_quota_is_refused_before_it_is_generated(scene: FakeHou) -> None:
    """Generating a graph the farm can't take costs time and memory for nothing."""
    # 201 steps in a row, one more than the service's 200 steps per job.
    spec: List[Tuple[Any, ...]] = [
        (f"step{i}", "hython run.py", "Processor", [f"step{i - 1}"] if i else [])
        for i in range(201)
    ]
    network: FakeNetwork = build_network(spec, scheduler_parms={"working_dir": "$HIP"})
    display: Any = network.topnet.displayNode()
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    assert ("generate", True) not in display.work_item_calls and _cooks(network) == []
    assert "201 steps per job" in scene.messages.calls[0][0]
    assert scene.messages.options[0]["severity"] == "Error"


def test_a_step_over_the_task_quota_stops_submits_generate_and_is_shown(scene: FakeHou) -> None:
    """The generate stops at the first step that's too big, so no confirmation follows it."""
    # sim generates one task more than a step may have, while Submit generates.
    network: FakeNetwork = _network()
    display: Any = network.topnet.displayNode()

    def generate() -> None:
        network.node("sim").workItems = [None] * 10_001
        network.context.emit("NodeGenerated", "sim")

    display.on_generate = generate
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    ((text, buttons),) = scene.messages.calls
    assert "sim: 10,001 tasks per step" in text and buttons == ()
    assert scene.messages.options[0]["severity"] == "Error"
    assert network.context.canceled.wait(5) and _cooks(network) == []
    # The count stops with the generate, so a later cook isn't counted by it.
    assert network.context.handlers == []


def _with_requirements(network: FakeNetwork, **parms: Any) -> None:
    """Set the Host Requirements tab to custom with ``parms``, the rest at the HDA's defaults."""
    # Every number at -1 and both lists empty, as a new node has them.
    values: Dict[str, Any] = {"hr_mode": "custom", "hr_os": "", "hr_cpu_arch": ""}
    for field in ("vcpu", "memory", "gpu", "gpu_memory", "scratch"):
        values[f"hr_{field}_min"] = -1
        values[f"hr_{field}_max"] = -1
    values.update({"hr_amounts": 0, "hr_attributes": 0}, **parms)
    for name, value in values.items():
        network.scheduler_node.set_parm(name, value)


def test_the_confirmation_lists_the_host_requirements(scene: FakeHou) -> None:
    """The artist sees which workers can take the job before choosing Submit."""
    # Linux workers with at least one GPU.
    network: FakeNetwork = _network()
    _with_requirements(network, hr_os="linux", hr_gpu_min=1)
    assert submit(network.scheduler_node, get_setting=_workstation) is True
    text: str = scene.messages.calls[0][0]
    assert "Host requirements: attr.worker.os.family any of linux, amount.worker.gpu min 1" in text


def test_the_confirmation_counts_the_extra_attachments(scene: FakeHou, tmp_path: Path) -> None:
    """The artist sees that the listed files and folders go with the job before choosing Submit."""
    # One file, one folder and two output folders on the tab.
    network: FakeNetwork = _network()
    node: Any = network.scheduler_node
    lut: Path = tmp_path / "lut.cube"
    lut.write_text("")
    (tmp_path / "library").mkdir()
    for list_parm, entry, values in (
        ("input_filenames", "input_filenames", [str(lut)]),
        ("input_directories", "input_dir", [str(tmp_path / "library")]),
        ("output_directories", "output_dir", [str(tmp_path / "a"), str(tmp_path / "b")]),
    ):
        node.set_parm(list_parm, len(values))
        for index, value in enumerate(values, start=1):
            node.set_parm(f"{entry}{index}", value, kind="FileReference")
    assert submit(node, get_setting=_workstation) is True
    text: str = scene.messages.calls[0][0]
    assert "Extra attachments: 1 input file(s), 1 input folder(s), 2 output folder(s)" in text


def test_an_output_folder_in_another_top_level_folder_stops_submit_before_the_generate(
    scene: FakeHou,
) -> None:
    """On the farm the job got "/" as its root, so every task failed to find its node.

    The workstation names no storage profile here, so nothing can keep the two apart. On
    Windows the two paths share a drive, which isn't refused, so no popup names the folder.
    """
    # The working directory under /opt and the extra output folder under /tmp.
    network: FakeNetwork = _network()
    node: Any = network.scheduler_node
    node.set_parm("working_dir", "/opt/studio/cooks/shot")
    node.set_parm("output_directories", 1)
    node.set_parm("output_dir1", "/tmp/pdg-extra", kind="FileReference")
    if os.name == "nt":
        submit(node, get_setting=_workstation)
        assert all("output_dir1" not in text for text, _buttons in scene.messages.calls)
        return
    assert submit(node, get_setting=_workstation) is False
    ((text, buttons),) = scene.messages.calls
    assert f"{node.path()}/output_dir1" in text and "top-level folder" in text and buttons == ()
    display: Any = network.topnet.displayNode()
    assert ("generate", True) not in display.work_item_calls and _cooks(network) == []


def test_one_extra_input_file_alone_is_counted(scene: FakeHou, tmp_path: Path) -> None:
    """With no output folder listed, the line must still count the input the artist added."""
    # One listed file, and nothing else on the tab.
    network: FakeNetwork = _network()
    node: Any = network.scheduler_node
    lut: Path = tmp_path / "lut.cube"
    lut.write_text("")
    node.set_parm("input_filenames", 1)
    node.set_parm("input_filenames1", str(lut), kind="FileReference")
    assert submit(node, get_setting=_workstation) is True
    text: str = scene.messages.calls[0][0]
    assert "Extra attachments: 1 input file(s), 0 input folder(s), 0 output folder(s)" in text


def test_after_generating_a_read_no_step_writes_and_no_disk_holds_is_refused(
    scene: FakeHou,
) -> None:
    """With the items generated, a missing file outside the job is known, so Submit names it."""
    # render reads a literal file that no step writes and the disk doesn't hold.
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    network.node("render").hom.set_parm("file", "/elsewhere/tree.bgeo.sc", kind="FileReference")
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    assert "/obj/topnet1/render parm 'file'" in scene.messages.calls[0][0]
    assert _cooks(network) == []


class _Unprintable:
    """A value Houdini hands back that can't be turned into text, as a broken node might."""

    def __str__(self) -> str:
        raise RuntimeError("unreadable")


def test_a_scene_walk_that_fails_after_the_generate_still_gets_a_verdict(
    scene: FakeHou,
) -> None:
    """With no scene reads, Submit must judge the generated items alone, not stop on the missing reads.

    One step's node can't be read, so the scene walk reports itself unavailable.
    """
    # render's node gives a name that can't be turned into text, after a full generate.
    network: FakeNetwork = _network()
    network.node("render").hom.name = lambda: _Unprintable()  # type: ignore[method-assign]
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    assert submit(network.scheduler_node, get_setting=_workstation) is True
    assert "Steps: 3, tasks: 3" in scene.messages.calls[0][0]


def test_submit_reads_the_items_with_partitions_expanded(scene: FakeHou) -> None:
    """Submit must read the same expanded graph the cook reads, or the two could judge different jobs.

    In Houdini 21.0 the expanded graph gave every step its items with no false refusal.
    """
    # Generate one item per step, Submit, and read which graph the scheduler was asked for.
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    scheduler: Any = network.context.schedulers["deadlinecloudscheduler1"]
    assert submit(network.scheduler_node, get_setting=_workstation) is True
    assert scheduler.calls == [("dependencyGraph", True)]


class _ErrorTab:
    """The scheduler node's error and Warning tabs, as a refused cook start leaves them.

    The scheduler's ``onStartCook`` sends Submit's stop inside that cook, and Submit's dirty of
    the display node's items clears both tabs. ``events`` keeps the order of those and the popups.
    """

    def __init__(self, network: FakeNetwork, scene: FakeHou) -> None:
        self.shown: List[str] = []
        self.warned: List[str] = []
        self.events: List[str] = []
        self.scheduler: Any = network.context.schedulers.get("deadlinecloudscheduler1")
        scene.messages.on_show = lambda: self.events.append("popup")
        # Record the dirty, which clears the tabs, and each refused cook start, which sets them.
        display: Any = network.topnet.displayNode()
        dirty: Any = display.dirtyAllWorkItems
        cook: Any = display.cookWorkItems
        stops: List[Dict[str, Tuple[str, str]]] = display.stop_cooks

        def dirty_and_clear(remove_outputs: bool) -> None:
            dirty(remove_outputs)
            self.events.append("dirty")
            self.shown.clear()
            self.warned.clear()

        def cook_and_show(**kwargs: Any) -> None:
            taken: int = len(stops)
            try:
                cook(**kwargs)
            finally:
                # Each cook start the scheduler refused replaces what the tabs show.
                for stop in stops[taken:]:
                    for error, advisories in stop.values():
                        self.events.append("stop")
                        self.shown[:] = [error] if error else []
                        self.warned[:] = [advisories] if advisories else []

        display.dirtyAllWorkItems = dirty_and_clear
        display.cookWorkItems = cook_and_show


def test_a_stop_before_the_dirty_goes_on_the_error_tab_with_no_generate(scene: FakeHou) -> None:
    """A bad tab value stops Submit before it dirties or generates the display node's items.

    The network has a PDG graph, so the stop still goes on the tab, through a refused cook start.
    """
    # PDG has the scheduler from an earlier generate, and the GPU minimum is above its maximum.
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    tab: _ErrorTab = _ErrorTab(network, scene)
    _with_requirements(network, hr_gpu_min=4, hr_gpu_max=2)
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    text: str = scene.messages.calls[0][0]
    assert "hr_gpu_min" in text and tab.shown == [text] and tab.events == ["popup", "stop"]
    assert network.scheduler_node.generate_calls == [] and tab.scheduler.calls == []
    assert scene.updates == 0


def test_a_quota_stop_during_the_display_generate_is_shown_in_the_popup_only(
    scene: FakeHou,
) -> None:
    """A task quota cancels the display node's generate after Submit's dirty.

    In Houdini the cook start after that cancel failed before onStartCook, so the stop never
    reached the tab and stayed stored, where it could refuse a TOP Cook up to 30 s later.
    """
    # PDG has the scheduler, and sim generates one task more than a step may have.
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    tab: _ErrorTab = _ErrorTab(network, scene)
    display: Any = network.topnet.displayNode()

    def generate() -> None:
        network.node("sim").workItems = [None] * 10_001
        network.context.emit("NodeGenerated", "sim")
        # PDG's generate ends when the cancel lands.
        network.context.canceled.wait(5)

    # Submit dirties, its generate stops at the quota, and the popup names the step.
    display.on_generate = generate
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    ((text, buttons),) = scene.messages.calls
    assert "sim: 10,001 tasks per step" in text and buttons == ()
    # No stop is stored and no cook start is asked for, so the tab stays as the dirty left it.
    assert host.submit_stops == {} and display.stop_cooks == [] and display.cook_calls == []
    assert tab.events == ["dirty", "popup"] and tab.shown == []
    assert display.work_item_calls[0] == ("dirty", False)


@pytest.mark.parametrize(
    "cook_type, work_item_calls",
    [(1, [("dirty", False), ("generate", True)]), (3, [("dirty", False)])],
    ids=["in-process", "service"],
)
def test_with_no_scheduler_the_stop_still_goes_on_the_error_tab(
    scene: FakeHou,
    cook_type: int,
    work_item_calls: List[Tuple[str, bool]],
) -> None:
    """PDG names no scheduler in a fresh session, and may name none even after a generate.

    The cook start creates the scheduler, so the stop still goes on the tab.
    """
    # PDG names no scheduler, and render is an In-Process step or a Service node.
    network: FakeNetwork = _network()
    tab: _ErrorTab = _ErrorTab(network, scene)
    network.node("render").hom.set_parm("pdg_cooktype", cook_type)
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    text: str = scene.messages.calls[0][0]
    assert "The cook can't start." in text and tab.shown == [text]
    display: Any = network.topnet.displayNode()
    assert display.work_item_calls == work_item_calls
    assert network.scheduler_node.generate_calls == [] and scene.updates == 0


def test_each_refused_submit_shows_its_own_text_on_the_error_tab(scene: FakeHou) -> None:
    """A cookError sent outside a cook didn't stay on the tab in Houdini's UI.

    Each refused Submit starts its own cook start, so each sends its text again, after the popup.
    """
    # PDG has the scheduler, and render is an In-Process step, which is refused after the generate.
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    tab: _ErrorTab = _ErrorTab(network, scene)
    network.node("render").hom.set_parm("pdg_cooktype", 1)
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    first: str = scene.messages.calls[-1][0]
    assert tab.shown == [first] and tab.events[-2:] == ["popup", "stop"]
    assert network.scheduler_node.generate_calls == [] and scene.updates == 0
    # A second refusal with different text replaces the first on the tab.
    network.node("montage").hom.set_parm("pdg_cooktype", 1)
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    second: str = scene.messages.calls[-1][0]
    assert second != first and tab.shown == [second]
    assert network.scheduler_node.generate_calls == []


def test_a_later_quota_stop_clears_the_tab_of_an_earlier_stop(
    scene: FakeHou,
) -> None:
    """A stop before the dirty goes on the tab, and a later quota stop leaves the tab cleared.

    Submit itself sends no cookError and asks for no UI update, because the cook start does it.
    """
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    tab: _ErrorTab = _ErrorTab(network, scene)
    display: Any = network.topnet.displayNode()
    # First, a bad tab value stops Submit before the dirty.
    _with_requirements(network, hr_gpu_min=4, hr_gpu_max=2)
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    first: str = scene.messages.calls[-1][0]
    assert "hr_gpu_min" in first and display.work_item_calls == [] and tab.shown == [first]

    # Second, with the tab fixed, sim passes the task quota during the display generate.
    def generate() -> None:
        network.node("sim").workItems = [None] * 10_001
        network.context.emit("NodeGenerated", "sim")
        network.context.canceled.wait(5)

    _with_requirements(network, hr_gpu_min=-1, hr_gpu_max=-1)
    display.on_generate = generate
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    second: str = scene.messages.calls[-1][0]
    assert "sim: 10,001 tasks per step" in second and tab.shown == []
    # Neither stop generated the scheduler node or sent anything to it from Submit.
    assert network.scheduler_node.generate_calls == [] and tab.scheduler.calls == []
    assert scene.updates == 0


@pytest.mark.parametrize("confirmed", [True, False], ids=["submit", "cancel"])
def test_submits_advisories_go_on_the_warning_tab_once(scene: FakeHou, confirmed: bool) -> None:
    """An advisory showed only under Show Details, so it was gone once the confirmation closed.

    A confirmed cook sends the same advisories from its snapshot, so Submit sends none then. A
    cancel starts no cook, so Submit sends them through a cook start the scheduler refuses.
    """
    # Validation is off, so an In-Process render step is an advisory, found after the generate.
    scene.messages.choice = 0 if confirmed else 1
    network: FakeNetwork = _network()
    network.scheduler_node.set_parm("validate_graph", 0)
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    tab: _ErrorTab = _ErrorTab(network, scene)
    network.node("render").hom.set_parm("pdg_cooktype", 1)
    assert submit(network.scheduler_node, get_setting=_workstation) is confirmed
    details: str = scene.messages.options[-1]["details"]
    assert details.startswith("render: ")
    sent: List[Any] = [c for c in tab.scheduler.calls if c[0] in ("cookError", "cookWarning")]
    assert sent == [] and scene.updates == 0
    if confirmed:
        assert tab.events == ["dirty", "popup"] and _cooks(network) == [{"block": False}]
    else:
        assert tab.events == ["dirty", "popup", "stop"] and _cooks(network) == []
        assert tab.warned == [details] and tab.shown == []


def test_a_refused_submit_sends_its_advisories_to_the_warning_tab(scene: FakeHou) -> None:
    """A refused Submit showed only its refusals, so the advisory naming their cause was lost.

    With the working directory away from the scene, sim's $HIP output is refused, and the
    scene-directory advisory must go on the Warning tab in the same cook start as the error.
    """
    # The working directory is not the scene's folder, and sim writes under $HIP.
    network: FakeNetwork = _network()
    network.scheduler_node.set_parm("working_dir", "/tmp/elsewhere")
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    graph: Any = network.context.schedulers["deadlinecloudscheduler1"].dependency_graph[0]
    (sim_item,) = [item for item in graph if item.node.name == "sim"]
    sim_item.expectedOutputFiles = [(os.environ["HIP"] + "/geo/sim.bgeo.sc", "file/geo")]
    tab: _ErrorTab = _ErrorTab(network, scene)
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    text: str = scene.messages.calls[-1][0]
    assert "sim:" in text and "outside the working directory" in text
    (warning,) = tab.warned
    assert warning.startswith(f"{os.environ['HIP']}: The scene is in ")
    # Windows puts the current drive on a rooted path that has none, and uses backslashes.
    drive: str = os.path.splitdrive(os.getcwd())[0]
    assert f"'{drive}{os.sep}tmp{os.sep}elsewhere'" in warning
    assert tab.shown == [text] and tab.events == ["dirty", "popup", "stop"]


_ALL: Tuple[str, ...] = ("wedge1", "sim", "render", "partition", "montage")


_UNFINISHED: str = (
    "The generate didn't finish, so Submit can't check the graph. "
    "Fix the node errors, then submit again."
)


#: The end of Submit's popup for empty steps with no node error, which gives both causes.
_BOTH_CAUSES: str = (
    "Either the generate didn't finish (check the node errors), "
    "or they make no items with these settings."
)


@pytest.mark.parametrize(
    "empty, dynamic, error_shown, expected",
    [
        (_ALL, (), True, _UNFINISHED),
        (_ALL, (), False, "These steps have no work items: sim, render, montage. " + _BOTH_CAUSES),
        (
            ("render", "partition", "montage"),
            (),
            False,
            "These steps have no work items: render, montage. " + _BOTH_CAUSES,
        ),
        (
            ("sim", "render", "partition", "montage"),
            ("render", "partition", "montage"),
            False,
            "These steps have no work items: sim. " + _BOTH_CAUSES,
        ),
    ],
    ids=[
        "wedge1-node-error",
        "error-not-set-yet",
        "empty-mid-chain-step",
        "only-static-step-empty",
    ],
)
@pytest.mark.parametrize("found", [True, False], ids=["scheduler", "no-scheduler"])
def test_a_generate_that_did_not_finish_stops_submit_on_the_popup_and_the_error_tab(
    scene: FakeHou,
    caplog: pytest.LogCaptureFixture,
    empty: Tuple[str, ...],
    dynamic: Tuple[str, ...],
    error_shown: bool,
    expected: str,
    found: bool,
) -> None:
    """PDG's blocking generate returns normally when it is canceled or a node fails to generate.

    Submit must stop rather than judge part of the graph. In Houdini 21.0 the node error came
    0.7 s after the generate returned, so with no error the empty items alone must stop it:
    every static step empty, or an empty step below an empty node, as a cancel leaves it. With
    no error the steps may also make nothing, so the popup names them and both causes. The
    text also goes on the error tab, even when PDG has no scheduler, because the cook start makes one.
    """
    # The generate leaves the steps empty, and may set wedge1's error or lose the scheduler.
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    tab: _ErrorTab = _ErrorTab(network, scene)
    display: Any = network.topnet.displayNode()

    for name in dynamic:
        network.node(name).isDynamic = True

    def generate() -> None:
        # The nodes in ``empty`` have no items, and the error may not be set yet.
        for name in empty:
            network.node(name).workItems = []
        tab.scheduler.dependency_graph = ({}, {}, [])
        if error_shown:
            network.node("wedge1").hom.node_errors = ["Unable to evaluate expression."]
        if not found:
            del network.context.schedulers["deadlinecloudscheduler1"]

    # Submit stops with no cook and no generate of the scheduler node.
    caplog.set_level("INFO")
    display.on_generate = generate
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    ((text, buttons),) = scene.messages.calls
    assert text == expected
    assert buttons == () and _cooks(network) == []
    assert network.scheduler_node.generate_calls == []
    # With or without the scheduler, the same text is on the tab.
    assert tab.events == ["dirty", "popup", "stop"] and tab.shown == [text]


def test_an_error_on_a_node_the_display_node_does_not_cook_does_not_stop_submit(
    scene: FakeHou,
) -> None:
    """Submit generates only the display node and the nodes above it, so only their errors count."""
    # Display render, so montage below it is outside the cook, and give montage an error.
    network: FakeNetwork = _network()
    network.topnet.display = network.node("render").hom
    _generated(network, {"sim": 1, "render": 1})
    network.node("montage").hom.node_errors = ["An error from an earlier cook."]
    assert submit(network.scheduler_node, get_setting=_workstation) is True


def _add_inner_step(
    network: FakeNetwork, subnet: FakeNode, name: str, *inputs: FakeNode
) -> FakeNode:
    """Add a step called ``name`` inside ``subnet``, wired to ``inputs``, with its own PDG node."""
    # PDG names a node inside a subnet by the subnet's name and its own.
    inner: FakeNode = subnet.add(FakeNode(f"{subnet.path()}/{name}"), *inputs)
    pdg_inner: FakePdgNode = FakePdgNode(
        name=f"{subnet.name()}_{name}", command=f"hython {name}.py", hom=inner
    )
    inner.pdg_node = pdg_inner
    network.context.nodes.append(pdg_inner)
    return inner


def test_a_displayed_subnet_cooks_its_display_node_and_the_steps_above_it(
    scene: FakeHou,
) -> None:
    """A subnet has no PDG node, so the checks start from its own display node.

    Starting from nothing would check every node, and refuse the steps the cook never
    reaches for having no items: ``stray`` inside the subnet and ``side`` outside it.
    """
    # side hangs off the wedge, and the subnet outputs inner, wired to montage, beside stray.
    network: FakeNetwork = _network()
    side: FakeNode = network.topnet.add(FakeNode("/obj/topnet1/side"), network.node("wedge1").hom)
    side.pdg_node = FakePdgNode(name="side", command="hython side.py", hom=side)
    network.context.nodes.append(side.pdg_node)
    subnet: FakeNode = network.topnet.add(FakeNode("/obj/topnet1/subnet1"))
    subnet.display = _add_inner_step(network, subnet, "inner", network.node("montage").hom)
    network.node("subnet1_inner").workItems = [None]
    _add_inner_step(network, subnet, "stray")
    network.topnet.display = subnet
    _generated(network, {"sim": 1, "render": 1, "montage": 1, "subnet1_inner": 1})
    # Submit counts the four cooked steps and never names side or stray.
    assert submit(network.scheduler_node, get_setting=_workstation) is True
    text: str = scene.messages.calls[0][0]
    assert "side" not in text and "stray" not in text
    assert "Steps: 4, tasks: 4" in text


def _add_service_node(network: FakeNetwork, source: str, *inputs: FakeNode) -> FakePdgNode:
    """Add a node called ``server``, wired to ``inputs``, whose cook type is Service.

    ``source`` says where Service shows: PDG's own cook type, or only the Cook Type parm,
    as just after the artist sets it.
    """
    # A TOP node and its PDG node, with Service set in one of the two places.
    hom: FakeNode = network.topnet.add(FakeNode("/obj/topnet1/server"), *inputs)
    pdg_node: FakePdgNode = FakePdgNode(name="server", command="hython serve.py", hom=hom)
    if source == "pdg":
        pdg_node.cookType = 3
    else:
        hom.set_parm("pdg_cooktype", 3)
    hom.pdg_node = pdg_node
    network.context.nodes.append(pdg_node)
    return pdg_node


@pytest.mark.parametrize("source", ["pdg", "parm"])
def test_a_service_node_the_display_node_does_not_cook_does_not_stop_submit(
    scene: FakeHou, source: str
) -> None:
    """The cook never generates it, so no service process starts, and refusing it blocked the job."""
    # server is wired to nothing, so the display node montage doesn't cook it.
    network: FakeNetwork = _network()
    _add_service_node(network, source)
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    assert submit(network.scheduler_node, get_setting=_workstation) is True
    text: str = scene.messages.calls[0][0]
    assert text.startswith("Submit this TOP network") and "server" not in text


def test_a_service_node_above_the_display_node_stops_submit_before_the_generate(
    scene: FakeHou,
) -> None:
    """The display node's generate would start its service processes, so Submit stops first.

    Only the Cook Type parm says Service, as just after the artist sets it.
    """
    # server sits between render and the display node, so the cook reaches it.
    network: FakeNetwork = _network()
    _add_service_node(network, "parm", network.node("render").hom)
    network.topnet.display = network.topnet.add(
        FakeNode("/obj/topnet1/out"), network.node("server").hom
    )
    display: Any = network.topnet.displayNode()
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    text: str = scene.messages.calls[0][0]
    assert "server: This node's work items cook in a long-running service process" in text
    assert ("generate", True) not in display.work_item_calls and _cooks(network) == []


def test_a_step_with_no_items_below_nodes_with_items_is_still_refused_as_too_early(
    scene: FakeHou,
) -> None:
    """Only a step whose nodes above it have items is the early read, so it keeps its refusal."""
    # montage alone has no items, and every node above it has some.
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1})
    network.node("montage").workItems = []
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    text: str = scene.messages.calls[-1][0]
    assert "montage: This node runs on the farm and has generated no work items yet" in text


class _Queue:
    """``hdefereval``: keeps what Submit queues for after its button callback, until the test runs it."""

    def __init__(self) -> None:
        self.queued: List[Callable[[], Any]] = []

    def executeDeferred(self, function: Callable[[], Any]) -> None:
        self.queued.append(function)

    def run(self) -> None:
        """Run and clear the queue, as Houdini does once the callback returns."""
        queued: List[Callable[[], Any]] = self.queued
        self.queued = []
        for function in queued:
            function()


@pytest.fixture
def queue(scene: FakeHou, monkeypatch: pytest.MonkeyPatch) -> _Queue:
    """A main-thread queue the test runs by hand, in place of the one that runs at once."""
    deferred: _Queue = _Queue()
    monkeypatch.setitem(sys.modules, "hdefereval", deferred)
    return deferred


def _record_cook_kwargs(node: FakeNode) -> List[Dict[str, Any]]:
    """Record the arguments of every ``cookWorkItems`` call on ``node``, refused or not."""
    seen: List[Dict[str, Any]] = []
    cook: Callable[..., None] = node.cookWorkItems

    def record(**kwargs: Any) -> None:
        seen.append(kwargs)
        cook(**kwargs)

    node.cookWorkItems = record  # type: ignore[method-assign]
    return seen


def test_a_stopped_submit_stores_its_stop_and_queues_a_cook_of_the_output_node(
    scene: FakeHou, queue: _Queue
) -> None:
    """A cook started inside the button callback never reached onStartCook in the GUI.

    So the cook start waits for the main-thread queue, cooks the node Cook Output Node cooks,
    and asks for no second save prompt.
    """
    # render is an In-Process step and the network's Output node, and montage is the display node.
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    network.node("render").hom.set_parm("pdg_cooktype", 1)
    output: FakeNode = network.node("render").hom
    network.topnet.outputNode = lambda: output  # type: ignore[attr-defined]
    kwargs: List[Dict[str, Any]] = _record_cook_kwargs(output)
    path: str = network.scheduler_node.path()
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    # The popup has shown, the stop waits for onStartCook, and nothing is cooked yet.
    text: str = scene.messages.calls[-1][0]
    assert "The cook can't start." in text and host.submit_stops[path][1:] == (text, "")
    assert len(queue.queued) == 1 and kwargs == []
    queue.run()
    assert kwargs == [
        {"block": False, "generate_only": False, "save_prompt": False, "nodes": [output]}
    ]
    assert output.stop_cooks == [{path: (text, "")}] and _cooks(network) == []


@pytest.mark.parametrize("stop", ["default-scheduler", "no-display-node", "no-pdg-graph"])
def test_a_stop_before_the_network_has_a_pdg_graph_starts_no_cook(
    scene: FakeHou, queue: _Queue, stop: str
) -> None:
    """These stops come before Submit has a PDG graph, so they stay popup only.

    A cook start there could run on another scheduler, or build the graph as a side effect.
    """
    # Each case breaks the network before Submit gets its graph context.
    network: FakeNetwork = _network()
    display: Any = network.topnet.displayNode()
    if stop == "default-scheduler":
        network.topnet.set_parm("topscheduler", "localscheduler")
    elif stop == "no-display-node":
        network.topnet.display = None
    else:
        network.scheduler_node.context = None
        network.scheduler_node.cook_creates = None
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    # One error popup, and no stop stored, queued or cooked.
    ((_, buttons),) = scene.messages.calls
    assert buttons == () and host.submit_stops == {} and queue.queued == []
    assert display.stop_cooks == [] and display.cook_calls == []


@pytest.mark.parametrize(
    "start, warning",
    [("refused", ""), ("ignored", "no error"), ("failed", "No cook to start")],
)
def test_a_cook_start_that_leaves_the_stop_waiting_is_logged(
    scene: FakeHou, queue: _Queue, caplog: pytest.LogCaptureFixture, start: str, warning: str
) -> None:
    """When the cook start never reaches onStartCook, the tab stays empty and only the log says why.

    A start that onStartCook refused took the stop, so it logs nothing.
    """
    # A refused Submit, then a cook start that takes the stop, does nothing, or fails on its own.
    caplog.set_level("WARNING")
    network: FakeNetwork = _network()
    _generated(network, {"sim": 1, "render": 1, "montage": 1})
    network.node("render").hom.set_parm("pdg_cooktype", 1)
    display: Any = network.topnet.displayNode()
    if start != "refused":

        def cook(**_kwargs: Any) -> None:
            if start == "failed":
                raise OperationFailed("No cook to start")

        display.cookWorkItems = cook
    assert submit(network.scheduler_node, get_setting=_workstation) is False
    queue.run()
    path: str = network.scheduler_node.path()
    waiting: List[str] = [
        r.getMessage() for r in caplog.records if "still waiting" in r.getMessage()
    ]
    expected: List[str] = (
        [
            f"The cook start of {display.path()} hasn't reached {path}'s onStartCook yet "
            f"({warning}), so Submit's stop is still waiting."
        ]
        if warning
        else []
    )
    assert waiting == expected and (path in host.submit_stops) is bool(warning)
