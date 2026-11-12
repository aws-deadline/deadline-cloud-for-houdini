# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The Houdini host and the pre-cook checks, against small fakes of ``hou`` and ``pdg``."""

from __future__ import annotations

import json
import logging
import ntpath
import os
import posixpath
import re
import sys
import threading
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

import pytest

from .houdini_fakes import (
    COOKING_EXAMPLE,
    PYRO_BONFIRE,
    SCHEDULER_PATH,
    FakeHou,
    FakeNode,
    FakePdgNode,
    FakePyScheduler,
    FakeNetwork,
    build_network,
    install_hou,
)

from deadline.houdini_pdg.errors import CookSetupError, PayloadTransferError
from deadline.houdini_pdg.graph import NodeInfo
from deadline.houdini_pdg.houdini import host as host_module
from deadline.houdini_pdg.houdini.host import (
    HoudiniPdgHost,
    cook_set_names,
    prompt_to_save,
    read_host_requirements,
    read_scheduler_settings,
    run_pre_cook_checks,
)
from deadline.houdini_pdg.houdini.settings import SchedulerSettings
from deadline.houdini_pdg.ports import (
    ItemOutcome,
    ItemState,
    OutputFile,
    SceneFiles,
    WorkItemHandle,
)
from deadline.houdini_pdg.scene_reads import SceneReads
from deadline.houdini_pdg.validation import RefusalScenario, Verdict


@pytest.fixture
def scene(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHou:
    """A saved scene at ``<tmp>/scene/shot.hip``, with a UI, and no debug override."""
    hip: str = os.path.join(str(tmp_path), "scene", "shot.hip")
    os.makedirs(os.path.dirname(hip))
    monkeypatch.delenv("DEADLINE_PDG_DEBUG_LEVEL", raising=False)
    monkeypatch.setenv("HIP", os.path.dirname(hip))
    return install_hou(monkeypatch, hip)


def _raise(error: Exception) -> Callable[..., Any]:
    """A ``hou`` function that raises ``error``, as Houdini does when it can't answer."""

    def call(*args: Any) -> Any:
        raise error

    return call


def handle(item_id: int = 7, native: Any = None) -> WorkItemHandle:
    """A work item handle for item 7 of ``render``."""
    return WorkItemHandle(
        item_id=item_id, index=3, name="render_3", node_name="render", native=native
    )


class FakeNativeItem:
    """A ``pdg.WorkItem`` that counts ``clearOutputFiles`` calls, or raises from it."""

    def __init__(self, raises: bool = False) -> None:
        self.cleared: int = 0
        self.raises: bool = raises

    def clearOutputFiles(self) -> None:
        if self.raises:
            raise RuntimeError("no such method on this PDG build")
        self.cleared += 1

    def saveJSONString(self, global_attributes: bool) -> str:
        return json.dumps({"name": "render_3", "global": global_attributes})


# -- notify, serialize, working directory -------------------------------------


def test_a_started_item_has_its_earlier_outputs_cleared_before_it_starts(scene: FakeHou) -> None:
    """An earlier cook's output records are cleared, so this cook's are the only ones.

    PDG keeps the old record through the start flag and adds the new one beside
    it, so a consumer would see each file twice.
    """
    order: List[str] = []
    native: FakeNativeItem = FakeNativeItem()
    scheduler: FakePyScheduler = build_network(PYRO_BONFIRE).scheduler()
    # Record the clear and the start in one list, so their order is checked.
    native.clearOutputFiles = lambda: order.append("clear")  # type: ignore[method-assign]
    scheduler.onWorkItemStartCook = lambda *args: order.append(  # type: ignore[method-assign]
        f"start{args}"
    )
    HoudiniPdgHost(scheduler).notify(handle(native=native), ItemOutcome(ItemState.STARTED))
    assert order == ["clear", "start(7, -1, True)"]


@pytest.mark.parametrize("native", [None, FakeNativeItem(raises=True)])
def test_an_item_that_cannot_be_cleared_still_starts_cooking(scene: FakeHou, native: Any) -> None:
    """A record left behind repeats an old duplicate, while raising would lose the task."""
    scheduler: FakePyScheduler = build_network(PYRO_BONFIRE).scheduler()
    HoudiniPdgHost(scheduler).notify(handle(native=native), ItemOutcome(ItemState.STARTED))
    assert scheduler.calls == [("onWorkItemStartCook", 7, -1, True)]


def test_a_succeeded_item_reports_each_output_unchanged_then_success(scene: FakeHou) -> None:
    """PDG routes outputs by path and tag, so both must reach it as the worker reported them."""
    scheduler: FakePyScheduler = build_network(PYRO_BONFIRE).scheduler()
    outputs: Tuple[OutputFile, ...] = (
        OutputFile("/proj/geo/a.bgeo.sc", "file/geo"),
        OutputFile("/proj/b.exr", ""),
    )
    HoudiniPdgHost(scheduler).notify(
        handle(), ItemOutcome(ItemState.SUCCEEDED, outputs, duration_seconds=12.5)
    )
    assert scheduler.calls == [
        ("onWorkItemAddOutput", 7, -1, "/proj/geo/a.bgeo.sc", "file/geo", 0),
        ("onWorkItemAddOutput", 7, -1, "/proj/b.exr", "", 0),
        ("onWorkItemSucceeded", 7, -1, 12.5),
    ]


def test_a_failed_item_is_reported_failed(scene: FakeHou) -> None:
    """A failed task fails its work item, so PDG stops what depends on it."""
    scheduler: FakePyScheduler = build_network(PYRO_BONFIRE).scheduler()
    HoudiniPdgHost(scheduler).notify(handle(), ItemOutcome(ItemState.FAILED))
    assert scheduler.calls == [("onWorkItemFailed", 7, -1)]


def _host_with_item_log(log: Any) -> HoudiniPdgHost:
    """A host whose PDG graph holds work item 7, with ``log`` as its log."""
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    network.context.items[7] = types.SimpleNamespace(logMessages=log)
    return HoudiniPdgHost(network.scheduler())


def test_a_failure_reason_is_empty_when_pdg_has_no_log_or_no_item(scene: FakeHou) -> None:
    """A missing item or log must not raise, because the cook is reporting a failure already."""

    class Unreadable:
        def __str__(self) -> str:
            raise RuntimeError("deleted")

    assert _host_with_item_log(None).failure_reason(7) == ("", "")
    assert _host_with_item_log("x").failure_reason(8) == ("", "")
    assert _host_with_item_log(Unreadable()).failure_reason(7) == ("", "")
    broken: Any = types.SimpleNamespace(context=None)
    assert HoudiniPdgHost(broken).failure_reason(7) == ("", "")


def test_a_failure_reason_keeps_an_error_line_whole_after_its_first_error_word(
    scene: FakeHou,
) -> None:
    """Only the first "ERROR:" starts the reason, so a message that quotes another keeps its end."""
    log: str = "[01:00:00.000] ERROR: render failed: ERROR: out of memory\n"
    assert _host_with_item_log(log).failure_reason(7)[0] == "render failed: ERROR: out of memory"


def test_serialize_returns_the_json_pdg_writes_for_a_job(scene: FakeHou) -> None:
    """The payload is PDG's own JSON, with the global attributes a worker needs."""
    host: HoudiniPdgHost = HoudiniPdgHost(build_network(PYRO_BONFIRE).scheduler())
    assert json.loads(host.serialize(handle(native=FakeNativeItem()))) == {
        "name": "render_3",
        "global": True,
    }


def test_an_item_without_its_pdg_object_cannot_be_serialized(scene: FakeHou) -> None:
    """With no PDG object there is no payload, and the error names the item."""
    host: HoudiniPdgHost = HoudiniPdgHost(build_network(PYRO_BONFIRE).scheduler())
    with pytest.raises(PayloadTransferError, match="render_3"):
        host.serialize(handle())


# -- generation ---------------------------------------------------------------


def _start_watching_host(cook_set: Any = None) -> Tuple[HoudiniPdgHost, FakeNetwork]:
    """A host watching the generation of a Pyro Bonfire network."""
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    host: HoudiniPdgHost = HoudiniPdgHost(network.scheduler())
    host.watch_generation(cook_set)
    return host, network


def _start_counting_host() -> Tuple[HoudiniPdgHost, FakeNetwork, List[Verdict]]:
    """A host watching generation, recording each quota report and agreeing to stop."""
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    host: HoudiniPdgHost = HoudiniPdgHost(network.scheduler())
    reports: List[Verdict] = []

    def stop(verdict: Verdict) -> bool:
        reports.append(verdict)
        return True

    host.watch_generation(None, stop)
    return host, network, reports


@pytest.mark.parametrize(
    "counts, subject, words",
    [
        ({"sim": 10_001}, "sim", "10,001 tasks per step"),
        ({"sim": 6_000, "render": 6_000}, "this cook", "12,000 tasks per job"),
    ],
)
def test_a_task_quota_crossed_while_generating_stops_generation_once(
    scene: FakeHou, counts: Dict[str, int], subject: str, words: str
) -> None:
    """Without the stop, PDG generated every item of a huge wedge before anything was refused.

    A step can pass the per-step limit alone, and two steps that each fit can pass the
    per-job limit together. Either way the report comes once, at the node that crosses.
    """
    # Generate each step in turn, then read the one report and the cancel.
    host, network, reports = _start_counting_host()
    for name, count in counts.items():
        assert reports == []
        network.node(name).workItems = [None] * count
        network.context.emit("NodeGenerated", name)
    (verdict,) = reports
    by_subject: Dict[str, str] = {r.subject: r.detail for r in verdict.refusals}
    assert words in by_subject[subject]
    assert network.context.canceled.wait(5)
    # A node that generates after the stop is not reported again.
    network.node("montage").workItems = [None] * 10_001
    network.context.emit("NodeGenerated", "montage")
    assert len(reports) == 1


def test_a_quota_the_cook_does_not_stop_for_is_reported_once(scene: FakeHou) -> None:
    """With validation off, each later step used to add the same quota warning again."""
    # The cook declines to stop, as with validation off, and three steps pass the quota.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    host: HoudiniPdgHost = HoudiniPdgHost(network.scheduler())
    reports: List[Verdict] = []

    def decline(verdict: Verdict) -> bool:
        reports.append(verdict)
        return False

    host.watch_generation(None, decline)
    for name in ("sim", "render", "montage"):
        network.node(name).workItems = [None] * 10_001
        network.context.emit("NodeGenerated", name)
    assert len(reports) == 1 and not network.context.canceled.is_set()


def test_items_of_a_node_that_is_not_a_step_are_not_counted(scene: FakeHou) -> None:
    """A wedge's items cook here and are never tasks, so they can't pass a task quota."""
    # A wedge with twice the job's limit, which isn't a step.
    host, network, reports = _start_counting_host()
    network.node("wedge1").workItems = [None] * 20_000
    network.context.emit("NodeGenerated", "wedge1")
    assert reports == [] and not network.context.canceled.is_set()


def test_stopping_the_watch_removes_its_handler_and_is_safe_twice(scene: FakeHou) -> None:
    """A handler left behind would count the next cook's items against this one.

    A stop after a failed start, or a second stop, must not raise either.
    """
    host, network, reports = _start_counting_host()
    assert network.context.handlers
    host.stop_watching_generation()
    host.stop_watching_generation()
    assert network.context.handlers == []


def test_only_the_steps_pdg_asked_for_are_awaited(scene: FakeHou) -> None:
    """A step outside the cook set never generates, so waiting for it would stall the cook."""
    host, network = _start_watching_host(frozenset({"wedge1", "sim"}))
    assert host.pending_generation() == ("sim",)
    network.context.emit("NodeGenerated", "sim")
    assert host.pending_generation() == ()


def test_a_step_with_a_queued_item_has_generated_without_announcing_it(scene: FakeHou) -> None:
    """A step generated before this cook may not announce itself again, but a queued item shows it is done."""
    host, network = _start_watching_host()
    queued: Any = types.SimpleNamespace(state=types.SimpleNamespace(name="Waiting"))
    stale: Any = types.SimpleNamespace(state=types.SimpleNamespace(name="Uncooked"))
    network.node("sim").workItems = [stale, queued]
    network.node("render").workItems = [stale]
    assert host.pending_generation() == ("montage", "render")


@pytest.mark.parametrize(
    "dynamic, pending",
    [
        (("sim", "render", "montage"), ()),
        (("render", "montage"), ("sim",)),
        (("montage",), ("render", "sim")),
    ],
)
def test_a_step_pdg_reports_dynamic_is_not_awaited(
    scene: FakeHou, dynamic: Tuple[str, ...], pending: Tuple[str, ...]
) -> None:
    """A dynamic step generates only after its upstream items cook, which the wait holds back.

    So the cook would stall until the timeout. PDG flags the steps below one as dynamic too.
    """
    host, network = _start_watching_host()
    for name in dynamic:
        network.node(name).isDynamic = True
    assert host.pending_generation() == pending


class _PdgNodeWithoutDynamicFlag(FakePdgNode):
    """A ``pdg.Node`` as Houdini 19.5 has it, with no ``isDynamic``."""

    def __getattribute__(self, name: str) -> Any:
        if name == "isDynamic":
            raise AttributeError(name)
        return super().__getattribute__(name)


@pytest.mark.parametrize(
    "token, pending",
    [("0", ("sim",)), ("1", ("montage", "render", "sim"))],
)
def test_without_pdgs_dynamic_flag_the_generate_when_parm_decides_what_is_awaited(
    scene: FakeHou, token: str, pending: Tuple[str, ...]
) -> None:
    """Houdini 19.5 has no ``isDynamic``, so render's Generate When says it is dynamic.

    Every step below it is dynamic too, so the wait skips them rather than stall until
    the timeout. A static Generate When still waits for all three steps.
    """
    host, network = _start_watching_host()
    for node in network.pdg_nodes:
        node.__class__ = _PdgNodeWithoutDynamicFlag
    # The menu's tokens are out of label order, as Houdini has them.
    network.node("render").hom.set_parm(
        "pdg_workitemgeneration",
        token,
        menu=("1", "3", "0", "2"),
        labels=(
            "All Upstream Items are Generated",
            "All Upstream Items are Cooked",
            "Each Upstream Item is Cooked",
            "Automatic",
        ),
    )
    assert host.pending_generation() == pending


# -- read_graph and read_scene ------------------------------------------------


def test_read_graph_reads_the_dependency_graph_once_and_keeps_the_cook_set(scene: FakeHou) -> None:
    """One read gives a consistent snapshot, and a node outside the cook set is marked so."""
    # The cook set names two of the five nodes.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    network.context.cookSet = [network.node("sim"), network.node("render")]
    scheduler: FakePyScheduler = network.scheduler()
    infos: Sequence[NodeInfo] = HoudiniPdgHost(scheduler).read_graph()
    assert scheduler.calls == [("dependencyGraph", True)]
    by_name: Dict[str, NodeInfo] = {info.name: info for info in infos}
    assert by_name["render"].upstream == ("sim",)
    assert by_name["sim"].is_schedulable and not by_name["wedge1"].is_schedulable
    assert by_name["sim"].in_cook_set is True and by_name["montage"].in_cook_set is False


def test_a_pdg_node_whose_job_script_cannot_be_read_is_not_a_step(scene: FakeHou) -> None:
    """A node being deleted raises on every read, and must not be waited for as a step."""

    class Deleted(FakePdgNode):
        @property
        def scriptInfo(self) -> Any:
            raise RuntimeError("deleted")

    network: FakeNetwork = build_network(PYRO_BONFIRE)
    network.pdg_nodes.append(Deleted(name="gone", command="hython x.py"))
    host: HoudiniPdgHost = HoudiniPdgHost(network.scheduler())
    host.watch_generation(None)
    assert "gone" not in host.pending_generation()


def test_an_empty_or_unreadable_cook_set_is_unknown() -> None:
    """An empty set would leave every node out, so it reads as unknown, like an unreadable one."""
    assert cook_set_names([]) is None
    assert cook_set_names(None) is None
    assert cook_set_names(42) is None
    assert cook_set_names([types.SimpleNamespace(name="a"), "b"]) == {"a", "b"}


def test_a_reads_static_prefix_has_its_variables_expanded(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read that depends on a work item is matched by its folder, so ``$HIP`` there must be expanded.

    An unknown variable expands to nothing in Houdini, which would shorten the path, so it gives no prefix.
    """
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    hom: Any = network.node("render").hom
    hom.set_parm("file", "$HIP/geo/`@pdg_index`.bgeo", kind="FileReference")
    hom.set_parm("other", "${NO_SUCH_VARIABLE_X}/geo/`@pdg_index`.bgeo", kind="FileReference")
    reads: SceneReads = HoudiniPdgHost(network.scheduler()).read_scene()
    prefixes: Dict[str, str] = {read.parm: read.static_prefix for read in reads.reads}
    # Houdini puts $HIP's value in place and keeps the authored "/geo/" as typed.
    assert prefixes["file"] == str(tmp_path / "scene") + "/geo/"
    assert prefixes["other"] == ""


# -- scene files --------------------------------------------------------------


def _touch(*parts: str) -> str:
    """Create an empty file, and its folders, and return its path."""
    path: str = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return path


def _reference(node: FakeNode, parm: str, value: str, scene: FakeHou) -> None:
    """Give ``node`` a file parm holding ``value``, and list it in ``hou.fileReferences``."""
    scene.file_references.append((node.set_parm(parm, value, kind="FileReference"), value))


def test_work_item_expressions_and_relative_paths_are_not_globbed(
    scene: FakeHou, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """With a work item active, the ROP's scan keeps `` `@pdg_input` ``; it must not become ``cwd/*``.

    A relative path would resolve against Houdini's current directory, which has
    nothing to do with the scene. A token folder parm that evaluates to home is left
    out too, and the home guard doesn't fire on it, because it is never attached.
    """
    folder: str = str(tmp_path / "scene")
    _touch(folder, "frame.0001.exr")
    # A file whose name spells an attribute, which only a work item can name.
    _touch(folder, "@pdg_name.exr")
    # A step's earlier output, which the wedge expression would glob into.
    _touch(folder, "render", "v0", "f.0001.jpg")
    # Houdini's current directory, standing in for the artist's home directory.
    cwd: Path = tmp_path / "home"
    (cwd / "Library").mkdir(parents=True)
    (cwd / "frame.0001.exr").write_text("")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(cwd))
    monkeypatch.setenv("USERPROFILE", str(cwd))
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    sop: FakeNode = network.topnet.add(FakeNode("/obj/geo1/file1", category="Sop"))
    # A token folder parm that evaluates to home while a work item is active.
    home_token: Any = sop.set_parm("dir", "`@pdg_input`", kind="FileReference")
    home_token.evalAsString = lambda: str(cwd)
    scene.file_references.append((home_token, "`@pdg_input`"))
    # The scan keeps a reference only when it evaluates to a file, so each one does.
    for index, value in enumerate(
        (
            "`@pdg_input`",
            "$HIP/render/v`@wedgeindex`/f.$F4.jpg",
            "frame.$F4.exr",
            "$HIP/@pdg_name.exr",
            "`chs('../file')`",
        )
    ):
        parm: Any = sop.set_parm(f"f{index}", value, kind="FileReference")
        parm.evalAsString = lambda: os.path.join(folder, "frame.0001.exr")
        scene.file_references.append((parm, value))
    _reference(sop, "real", "$HIP/frame.$F4.exr", scene)
    host: HoudiniPdgHost = HoudiniPdgHost(network.scheduler())

    with caplog.at_level(logging.INFO):
        files: SceneFiles = host.scene_files()

    assert files.assets == (os.path.join(folder, "frame.0001.exr"),)
    assert files.directories == ()
    assert "Not attaching '`@pdg_input`'" in caplog.text


def test_a_failed_asset_scan_still_returns_the_hip(
    scene: FakeHou, caplog: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cook can still run without the assets, so it goes on, and the warning says what may be missing."""
    # A scene Houdini can't scan for file references.
    monkeypatch.setattr(sys.modules["hou"], "fileReferences", _raise(RuntimeError("no scene")))
    host: HoudiniPdgHost = HoudiniPdgHost(build_network(PYRO_BONFIRE).scheduler())
    with caplog.at_level(logging.WARNING):
        files: SceneFiles = host.scene_files()
    assert files.hip_file.endswith("shot.hip") and files.assets == ()
    assert "may be missing on the workers" in caplog.text


def test_the_two_warnings_an_artist_may_read_carry_the_traceback(
    scene: FakeHou, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning without its traceback hides which Houdini call failed, so it can't be fixed."""
    # An item whose outputs can't be cleared, and a scene Houdini can't scan for references.
    monkeypatch.setattr(sys.modules["hou"], "fileReferences", _raise(RuntimeError("no scene")))
    host: HoudiniPdgHost = HoudiniPdgHost(build_network(PYRO_BONFIRE).scheduler())
    with caplog.at_level(logging.WARNING):
        host.notify(handle(native=FakeNativeItem(raises=True)), ItemOutcome(ItemState.STARTED))
        host.scene_files()
    # Each warning keeps the RuntimeError that caused it.
    prefixes: Tuple[str, str] = (
        "Could not clear the earlier outputs",
        "The scene's assets could not be scanned",
    )
    causes: Dict[str, Any] = {
        prefix: record.exc_info[0] if record.exc_info else None
        for record in caplog.records
        for prefix in prefixes
        if record.getMessage().startswith(prefix)
    }
    assert causes == dict.fromkeys(prefixes, RuntimeError)


def test_the_integrations_own_libraries_are_not_attached_and_expanded_libraries_are_directories(
    scene: FakeHou, tmp_path: Path
) -> None:
    """A worker never cooks the scheduler or the ROP, and job attachments refuses a folder listed as a file."""
    # Both integration libraries are stored expanded, as folders, next to two scene libraries.
    folder: str = str(tmp_path / "scene")
    plugin: str = str(tmp_path / "plugin")
    for library in ("deadline_cloud_scheduler.hda", "deadline_cloud.hda"):
        os.makedirs(os.path.join(plugin, library))
    os.makedirs(os.path.join(folder, "expanded.hda"))
    _touch(folder, "packed.hda")
    scene.file_references = [
        (None, os.path.join(plugin, "deadline_cloud_scheduler.hda")),
        (None, os.path.join(plugin, "deadline_cloud.hda")),
        (None, "$HIP/expanded.hda"),
        (None, "$HIP/packed.hda"),
        # A reference to a definition inside a library is no file on disk.
        (None, "opdef:/Sop/x"),
    ]
    scene.libraries = {
        "deadlinecloudscheduler": os.path.join(plugin, "deadline_cloud_scheduler.hda"),
        "deadline_cloud": os.path.join(plugin, "deadline_cloud.hda"),
    }
    files: SceneFiles = HoudiniPdgHost(build_network(PYRO_BONFIRE).scheduler()).scene_files()
    assert files.assets == (os.path.join(folder, "packed.hda"),)
    assert files.directories == (os.path.join(folder, "expanded.hda"),)


# -- the home guard -----------------------------------------------------------


def _home_scene(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parm_value: str
) -> Tuple[HoudiniPdgHost, str]:
    """A scene saved directly in a fake home directory, with one folder parm holding ``parm_value``."""
    # Both home variables name the folder, as on Windows and in a shell on any system.
    home: str = str(tmp_path / "home")
    os.makedirs(home)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    hip: str = os.path.join(home, "shot.hip")
    open(hip, "w").close()
    monkeypatch.setenv("HIP", home)
    scene: FakeHou = install_hou(monkeypatch, hip)
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    sop: FakeNode = network.topnet.add(FakeNode("/obj/geo1/file1", category="Sop"))
    _reference(sop, "cachedir", parm_value, scene)
    return HoudiniPdgHost(network.scheduler()), home


@pytest.mark.parametrize("reach", ["hip", "home", "above", "root"])
def test_a_directory_parm_that_reaches_home_is_refused_naming_the_path_and_the_parm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reach: str
) -> None:
    """Attaching the home directory would hash all of it before submitting, so the cook would hang.

    ``$HIP`` reaches home when the ``.hip`` is saved directly in it, and a folder
    above home, or a filesystem root, holds the home directory too.
    """
    # The parm's value, and the path the refusal must name, for each way to reach home.
    home: str = str(tmp_path / "home")
    root: str = os.path.abspath(os.sep)
    value, named = {
        "hip": ("$HIP", home),
        "home": ("$HOME", home),
        "above": (str(tmp_path), str(tmp_path)),
        "root": (root, root),
    }[reach]
    host, _ = _home_scene(monkeypatch, tmp_path, value)
    with pytest.raises(CookSetupError) as raised:
        host.scene_files()
    assert named in str(raised.value)
    assert "/obj/geo1/file1/cachedir" in str(raised.value)


# -- settings -----------------------------------------------------------------


def _settings_node(**parms: Any) -> FakeNode:
    """A scheduler node with the given parms."""
    return FakeNode(SCHEDULER_PATH, parms=parms)


@pytest.mark.parametrize(
    "parms, named",
    [
        ({"working_dir": ""}, "'working_dir'"),
        ({"working_dir": "   "}, "'working_dir'"),
        # Houdini leaves an unknown variable in place, which no worker can resolve.
        ({"working_dir": "$NO_SUCH_VARIABLE_X/out"}, "'working_dir'.*NO_SUCH_VARIABLE_X"),
        # A broken expression is a setting problem, not a crash.
        ({"working_dir": RuntimeError("bad expression")}, "'working_dir'.*bad expression"),
        # The node offers two debug levels only, so any other value is a damaged scene.
        *[
            ({"working_dir": "/proj", "debug_level": v}, "'debug_level'.*0 or 1")
            for v in (2, -1, "high", None)
        ],
    ],
)
def test_a_bad_setting_names_its_parm(scene: FakeHou, parms: Dict[str, Any], named: str) -> None:
    """The artist needs to know which parm to fix."""
    with pytest.raises(CookSetupError, match=named):
        read_scheduler_settings(_settings_node(**parms))


def test_absent_parms_take_their_defaults_and_are_logged_once(
    scene: FakeHou, tmp_path: Path, caplog: Any
) -> None:
    """A node saved before a parm existed still cooks, validating, and says once how to get the parm."""
    # Read twice: the second read must not log again.
    node: FakeNode = FakeNode("/obj/topnet1/old_scheduler")
    with caplog.at_level(logging.WARNING):
        first: SchedulerSettings = read_scheduler_settings(node)
        second: SchedulerSettings = read_scheduler_settings(node)
    assert first == second == SchedulerSettings(working_dir=str(tmp_path / "scene"))
    missing: List[str] = [r.getMessage() for r in caplog.records if "has no" in r.getMessage()]
    assert len(missing) == 4
    for parm in ("'working_dir'", "'debug_level'", "'validate_graph'", "'output_directories'"):
        assert any(parm in m for m in missing), parm


def test_a_missing_parm_is_logged_at_its_first_read_naming_the_node(
    scene: FakeHou, caplog: Any
) -> None:
    """The artist reads the first warning to learn which node to recreate, and later reads add none."""
    node: FakeNode = FakeNode("/obj/topnet1/scheduler_from_an_old_scene")
    with caplog.at_level(logging.WARNING):
        read_scheduler_settings(node)
    first: List[str] = [r.getMessage() for r in caplog.records if "has no" in r.getMessage()]
    assert len(first) == 4
    assert all(m.startswith("Node /obj/topnet1/scheduler_from_an_old_scene has no") for m in first)
    # A second read of the same node logs nothing new.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        read_scheduler_settings(node)
    assert not [r for r in caplog.records if "has no" in r.getMessage()]


def test_a_valid_debug_level_variable_wins_and_only_an_invalid_one_is_reported(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """The variable debugs a scene without editing it, so a valid value wins over the node's.

    A warning on a valid value would cry wolf, and none on a typo would hide why it's ignored.
    """
    # The node says 0, and each value either wins or is ignored with a warning.
    node: FakeNode = _settings_node(working_dir="/proj", debug_level=0)
    for value, warned, level in (
        ("1", False, 1),
        ("0", False, 0),
        ("debug", True, 0),
        ("2", True, 0),
    ):
        caplog.clear()
        monkeypatch.setenv("DEADLINE_PDG_DEBUG_LEVEL", value)
        with caplog.at_level(logging.WARNING):
            assert read_scheduler_settings(node).debug_level == level, value
        assert ("Ignoring DEADLINE_PDG_DEBUG_LEVEL" in caplog.text) is warned, value


def test_the_scheduler_node_is_found_by_name_when_pdg_gives_none(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDG doesn't always link the scheduler to its node, so the node of this type is matched by name."""
    other: FakeNode = FakeNode("/obj/topnet2/deadlinecloudscheduler2")
    mine: FakeNode = FakeNode(SCHEDULER_PATH)
    scheduler: FakePyScheduler = FakePyScheduler()
    scene.instances = [other, mine]
    assert HoudiniPdgHost(scheduler).scheduler_node() is mine
    # The only node of the type is used even when its name differs.
    scene.instances = [other]
    assert HoudiniPdgHost(scheduler).scheduler_node() is other
    scene.instances = []
    with pytest.raises(CookSetupError, match="deadlinecloudscheduler1"):
        HoudiniPdgHost(scheduler).scheduler_node()
    # A session that can't list node types gives the same error, not a crash.
    monkeypatch.setattr(sys.modules["hou"], "nodeType", _raise(RuntimeError("no such category")))
    with pytest.raises(CookSetupError, match="deadlinecloudscheduler1"):
        HoudiniPdgHost(scheduler).scheduler_node()


def test_the_houdini_version_and_queue_values_come_from_the_scheduler_node(
    scene: FakeHou,
) -> None:
    """The Conda packages are pinned to the running Houdini, and the queue values are the node's own."""
    # A queue parameter is a spare parm with the shared prefix, as the ROP makes it.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    spare: Any = network.scheduler_node.set_parm(
        "queue_env_do_not_use_CondaChannels", "deadline-cloud"
    )
    network.scheduler_node.spare = [spare]
    host: HoudiniPdgHost = HoudiniPdgHost(network.scheduler())
    assert host.houdini_version() == "21.0.596"
    assert host.queue_parameter_values() == [{"name": "CondaChannels", "value": "deadline-cloud"}]


# -- the main thread ----------------------------------------------------------


def test_scene_reads_from_a_pdg_thread_run_on_the_main_thread(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Houdini crashes when a PDG thread reads the scene while its UI runs."""
    # An ``hdefereval.executeInMainThreadWithResult`` that records the thread it was called from.
    ran_on: List[str] = []

    def record_thread(function: Callable[..., Any], *args: Any) -> Any:
        ran_on.append(threading.current_thread().name)
        return function(*args)

    monkeypatch.setitem(
        sys.modules,
        "hdefereval",
        types.SimpleNamespace(executeInMainThreadWithResult=record_thread),
    )
    host: HoudiniPdgHost = HoudiniPdgHost(build_network(PYRO_BONFIRE).scheduler())
    results: List[Any] = []
    # Every read the cook and the submitter make, from one PDG thread.
    worker: threading.Thread = threading.Thread(
        target=lambda: results.extend(
            [host.read_graph(), host.read_scene(), host.read_settings(), host.houdini_version()]
        ),
        name="pdg",
    )
    worker.start()
    worker.join()
    assert len(results) == 4
    assert len(ran_on) >= 4 and set(ran_on) == {"pdg"}


def test_the_generation_wait_reads_generate_when_on_the_main_thread(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without PDG's dynamic flag, as on Houdini 19.5, the wait reads each node's parms.

    That read from PDG's thread would crash Houdini while its UI runs.
    """
    # An ``hdefereval.executeInMainThreadWithResult`` that records the thread it was called from.
    ran_on: List[str] = []

    def record_thread(function: Callable[..., Any], *args: Any) -> Any:
        ran_on.append(threading.current_thread().name)
        return function(*args)

    monkeypatch.setitem(
        sys.modules,
        "hdefereval",
        types.SimpleNamespace(executeInMainThreadWithResult=record_thread),
    )
    # Hide the flag on every node, then ask for the pending steps from a PDG thread.
    host, network = _start_watching_host()
    for node in network.pdg_nodes:
        node.__class__ = _PdgNodeWithoutDynamicFlag
    pending: List[Sequence[str]] = []
    worker: threading.Thread = threading.Thread(
        target=lambda: pending.append(host.pending_generation()), name="pdg"
    )
    worker.start()
    worker.join()
    assert pending == [("montage", "render", "sim")]
    assert ran_on == ["pdg"]


# -- pre-cook checks ----------------------------------------------------------


def _verdict(network: FakeNetwork, prompt: bool = False) -> Verdict:
    """The scheduler node's pre-cook verdict, after the save prompt when ``prompt``."""
    if prompt:
        prompt_to_save(network.scheduler_node)
    return run_pre_cook_checks(network.scheduler_node)[0]


def _refused(verdict: Verdict) -> List[Tuple[RefusalScenario, str]]:
    """The refusals as (scenario, subject), in order."""
    return [(r.scenario, r.subject) for r in verdict.refusals]


def test_before_generation_a_read_of_a_file_a_step_will_write_is_not_refused(
    scene: FakeHou,
) -> None:
    """No step has declared its outputs yet, so every read would look like a missing file."""
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    network.node("render").hom.set_parm("file", "/elsewhere/sim/out.bgeo.sc", kind="FileReference")
    assert RefusalScenario.LOCAL_FILE_UNREACHABLE not in {
        r.scenario for r in _verdict(network).refusals
    }


@pytest.mark.parametrize(
    "override, pdg_answer, sibling, refused",
    [
        # Names this scheduler by path, so the override changes nothing.
        ("../deadlinecloudscheduler1", "deadlinecloudscheduler1", False, False),
        (SCHEDULER_PATH, "deadlinecloudscheduler1", False, False),
        # Names another scheduler, and PDG agrees.
        ("localscheduler", "localscheduler", False, True),
        # A new override to a real sibling, while PDG still names this scheduler until the next cook.
        ("../localscheduler", "deadlinecloudscheduler1", True, True),
        # PDG resolves a bare name from the step, so a sibling's bare name falls back to this one.
        ("localscheduler", "deadlinecloudscheduler1", True, False),
        # The authored parm now names this scheduler, while PDG still names the old one.
        ("../deadlinecloudscheduler1", "localscheduler", False, False),
        # A parm that names no node, such as a deleted one, is judged by PDG's answer.
        ("../renamed_scheduler", "deadlinecloudscheduler1", False, False),
        ("../renamed_scheduler", "localscheduler", False, True),
        # The scheduler node's name or its PDG name, with no answer from PDG.
        ("deadlinecloudscheduler1", "", False, False),
        ("dc_pdg_name", "", False, False),
    ],
)
def test_a_scheduler_override_is_refused_only_when_it_resolves_to_another_scheduler(
    scene: FakeHou, override: str, pdg_answer: str, sibling: bool, refused: bool
) -> None:
    """The verdict follows the scheduler the next cook will use, as PDG resolves the override.

    PDG keeps a node's old scheduler until the next cook, so trusting it alone let Submit
    start a cook that then failed, and an override naming this scheduler is no refusal.
    """
    # Set render's override and PDG's current answer for it, with an optional sibling scheduler.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    network.scheduler_node.pdg_name = "dc_pdg_name"
    if sibling:
        network.topnet.add(FakeNode("/obj/topnet1/localscheduler"))
    network.node("render").hom.set_parm("topscheduler", override)
    network.node("render").scheduler_name = pdg_answer
    assert _refused(_verdict(network)) == (
        [(RefusalScenario.MULTIPLE_SCHEDULERS, "render")] if refused else []
    )


@pytest.mark.parametrize(
    "new, unsaved, words",
    [(True, False, "never been saved"), (False, True, "unsaved changes"), (True, True, "never")],
)
def test_an_unsaved_or_new_scene_is_refused(
    scene: FakeHou, new: bool, unsaved: bool, words: str
) -> None:
    """Workers load the saved file, so they would cook a different scene than the snapshot."""
    scene.hip_file.new, scene.hip_file.unsaved = new, unsaved
    (refusal,) = _verdict(build_network(PYRO_BONFIRE)).refusals
    assert refusal.scenario is RefusalScenario.UNSAVED_SCENE
    assert words in refusal.detail
    assert refusal.remedy == "Save the scene, then cook again."
    assert refusal.subject.endswith("shot.hip")
    # The Cook button's path never prompts and never saves.
    assert scene.messages.calls == [] and scene.hip_file.saves == 0


@pytest.mark.parametrize("new, refused", [(False, False), (True, True)])
def test_with_no_ui_only_a_never_saved_scene_is_refused(
    scene: FakeHou, new: bool, refused: bool
) -> None:
    """hython reports unsaved changes even right after a save, and no artist is editing the scene.

    A scene that was never saved has no file for a worker to load, UI or not.
    """
    # hython says the scene has changes, whether or not it was ever saved.
    scene.hip_file.new, scene.hip_file.unsaved, scene.ui = new, True, False
    assert [r.scenario for r in _verdict(build_network(PYRO_BONFIRE)).refusals] == (
        [RefusalScenario.UNSAVED_SCENE] if refused else []
    )


def _auto_save_network(auto_save: int) -> FakeNetwork:
    """A Pyro Bonfire network whose scheduler node has the auto-save toggle set to ``auto_save``."""
    return build_network(
        PYRO_BONFIRE,
        scheduler_parms={"working_dir": "$HIP", "validate_graph": 1, "auto_save_hip": auto_save},
    )


def test_always_save_saves_and_turns_auto_save_on_so_the_next_submit_does_not_ask(
    scene: FakeHou,
) -> None:
    """The artist answers once. The toggle is the one parm written, because its value changed."""
    scene.hip_file.unsaved = True
    scene.messages.choice = 1
    network: FakeNetwork = _auto_save_network(0)
    assert _verdict(network, prompt=True).accepted()
    assert scene.hip_file.saves == 1
    assert network.scheduler_node.parm("auto_save_hip").sets == [1]  # type: ignore[union-attr]
    # The next submit with changes saves without asking.
    scene.hip_file.unsaved = True
    assert _verdict(network, prompt=True).accepted()
    assert len(scene.messages.calls) == 1 and scene.hip_file.saves == 2


def test_a_network_without_a_pdg_graph_creates_its_nodes_first(scene: FakeHou) -> None:
    """A network never cooked has no PDG graph, so its nodes are created, without cooking, and judged."""
    network: FakeNetwork = build_network(COOKING_EXAMPLE)
    network.scheduler_node.context = None
    verdict: Verdict = _verdict(network)
    assert network.scheduler_node.cook_calls == [{"block": True, "tops_only": True}]
    assert _refused(verdict) == [(RefusalScenario.IN_PROCESS_NODE, "python1")]


def test_a_network_whose_pdg_graph_cannot_be_made_is_checked_when_it_cooks(
    scene: FakeHou, caplog: Any
) -> None:
    """Without a graph nothing can be judged yet, so the cook runs the checks when it starts."""
    network: FakeNetwork = build_network(COOKING_EXAMPLE)
    network.scheduler_node.context = None
    network.scheduler_node.cook_creates = None
    with caplog.at_level(logging.WARNING):
        verdict: Verdict = _verdict(network)
    assert verdict.accepted() and "no PDG graph yet" in caplog.text


# -- host requirements --------------------------------------------------------


def _requirements_node(**parms: Any) -> FakeNode:
    """A scheduler node with the Host Requirements tab at its defaults, plus ``parms``."""
    # Every number defaults to -1, which means not set, as on the HDA.
    defaults: Dict[str, Any] = {"hr_mode": "all", "hr_os": "", "hr_cpu_arch": ""}
    for field in ("vcpu", "memory", "gpu", "gpu_memory", "scratch"):
        defaults[f"hr_{field}_min"] = -1
        defaults[f"hr_{field}_max"] = -1
    defaults.update({"hr_amounts": 0, "hr_attributes": 0})
    defaults.update(parms)
    node: FakeNode = FakeNode(SCHEDULER_PATH, parms=defaults)
    # The two ordinal menus, so a read of the index instead of the token is caught.
    node.set_parm("hr_mode", defaults["hr_mode"], menu=("all", "custom"))
    for name in [name for name in defaults if name.startswith("hr_attr_option")]:
        node.set_parm(name, defaults[name], menu=("anyOf", "allOf"))
    return node


def test_the_tabs_values_become_the_block_the_rops_dialog_gives(scene: FakeHou) -> None:
    """Memory is typed in GiB and the service counts MiB, so a missed conversion asks for 16 MiB."""
    node: FakeNode = _requirements_node(
        hr_mode="custom", hr_os="linux windows", hr_vcpu_min=8, hr_memory_min=16, hr_memory_max=64
    )
    assert read_host_requirements(node) == {
        "attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux", "windows"]}],
        "amounts": [
            {"name": "amount.worker.vcpu", "min": 8},
            {"name": "amount.worker.memory", "min": 16384, "max": 65536},
        ],
    }


def test_gpu_memory_is_converted_from_gib_and_scratch_is_not(scene: FakeHou) -> None:
    """The service counts GPU memory in MiB and scratch space in GiB, as the ROP's dialog sends them."""
    node: FakeNode = _requirements_node(hr_mode="custom", hr_gpu_memory_min=8, hr_scratch_max=100)
    assert read_host_requirements(node) == {
        "amounts": [
            {"name": "amount.worker.gpu.memory", "min": 8192},
            {"name": "amount.worker.disk.scratch", "max": 100},
        ]
    }


def test_custom_amounts_and_attributes_are_read_from_each_multiparm_entry(scene: FakeHou) -> None:
    """Each entry is its own requirement, and an attribute's values are split at the commas."""
    node: FakeNode = _requirements_node(
        hr_mode="custom",
        hr_amounts=2,
        hr_amount_name1="licenses",
        hr_amount_min1=2,
        hr_amount_max1=5,
        hr_amount_name2="cores",
        hr_amount_min2=4,
        hr_amount_max2=-1,
        hr_attributes=1,
        hr_attr_name1="studio",
        hr_attr_option1="allOf",
        hr_attr_values1="vfx, fire",
    )
    assert read_host_requirements(node) == {
        "amounts": [
            {"name": "amount.licenses", "min": 2, "max": 5},
            {"name": "amount.cores", "min": 4},
        ],
        "attributes": [{"name": "attr.studio", "allOf": ["vfx", "fire"]}],
    }


@pytest.mark.parametrize(
    "parms, named",
    [
        ({"hr_gpu_min": 4, "hr_gpu_max": 2}, "hr_gpu_min"),
        ({"hr_os": "beos"}, "hr_os"),
        (
            {
                "hr_attributes": 1,
                "hr_attr_name1": "studio",
                "hr_attr_option1": "anyOf",
                "hr_attr_values1": " ",
            },
            "hr_attr_values1",
        ),
        (
            {"hr_amounts": 1, "hr_amount_name1": "", "hr_amount_min1": 1, "hr_amount_max1": -1},
            "hr_amount_name1",
        ),
    ],
)
def test_a_value_the_library_refuses_names_the_parm(
    scene: FakeHou, parms: Dict[str, Any], named: str
) -> None:
    """The service would refuse the job after the upload, without naming what to fix."""
    with pytest.raises(CookSetupError, match=named):
        read_host_requirements(_requirements_node(hr_mode="custom", **parms))


# -- the node's Job Attachments tab -------------------------------------------


def _set_list_parm(node: FakeNode, list_parm: str, entry: str, values: Sequence[str]) -> None:
    """Fill one of the tab's lists, as Houdini numbers a multiparm's entries from 1."""
    node.set_parm(list_parm, len(values))
    for index, value in enumerate(values, start=1):
        node.set_parm(f"{entry}{index}", value, kind="FileReference")


def test_listed_files_and_directories_join_the_scenes_own(scene: FakeHou, tmp_path: Path) -> None:
    """They join the scan and never replace it, so the scene's own textures still travel."""
    # A scene texture, a listed file outside the scene, a listed frame sequence and a listed folder.
    folder: str = str(tmp_path / "scene")
    tex: str = _touch(folder, "tex.exr")
    extra: str = _touch(str(tmp_path), "extra", "lut.cube")
    frames: List[str] = [_touch(folder, f"plate.000{i}.exr") for i in (1, 2)]
    os.makedirs(str(tmp_path / "library"))
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    sop: FakeNode = network.topnet.add(FakeNode("/obj/geo1/file1", category="Sop"))
    _reference(sop, "tex", "$HIP/tex.exr", scene)
    _set_list_parm(
        network.scheduler_node, "input_filenames", "input_filenames", [extra, "$HIP/plate.$F4.exr"]
    )
    _set_list_parm(
        network.scheduler_node, "input_directories", "input_dir", [str(tmp_path / "library")]
    )
    files: SceneFiles = HoudiniPdgHost(network.scheduler()).scene_files()
    assert files.assets == tuple(sorted([tex, extra, *frames]))
    assert files.directories == (str(tmp_path / "library"),)


@pytest.mark.parametrize(
    "list_parm, entry", [("input_filenames", "input_filenames"), ("input_directories", "input_dir")]
)
def test_a_listed_path_that_does_not_exist_names_its_parm(
    scene: FakeHou, tmp_path: Path, list_parm: str, entry: str
) -> None:
    """The artist typed the path, so a quiet skip would hide a typo until the worker failed."""
    # List a path that isn't on disk, then read the scene files.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    _set_list_parm(network.scheduler_node, list_parm, entry, [str(tmp_path / "nowhere")])
    with pytest.raises(CookSetupError) as raised:
        HoudiniPdgHost(network.scheduler()).scene_files()
    kind: str = "file" if entry == "input_filenames" else "folder"
    parm_path: str = f"{network.scheduler_node.path()}/{entry}1"
    assert f"'{parm_path}'" in str(raised.value) and f"not on disk as a {kind}" in str(raised.value)


def test_a_listed_home_directory_is_refused_naming_its_parm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Listing home by hand would hash all of it, the same as a scene parm that expands to it."""
    # A scene that is fine on its own, with $HOME added to the tab's folders.
    host, home = _home_scene(monkeypatch, tmp_path, "$HIP/caches")
    os.makedirs(os.path.join(home, "caches"))
    node: Any = host.scheduler_node()
    _set_list_parm(node, "input_directories", "input_dir", ["$HOME"])
    with pytest.raises(CookSetupError, match=f"{re.escape(node.path())}/input_dir1"):
        host.scene_files()


def test_the_output_directories_are_read_into_the_cooks_settings(
    scene: FakeHou, tmp_path: Path
) -> None:
    """Every check that asks whether a file reaches the next step needs the whole list."""
    node: FakeNode = _settings_node(working_dir="$HIP")
    _set_list_parm(
        node, "output_directories", "output_dir", [str(tmp_path / "renders") + "/./", ""]
    )
    assert read_scheduler_settings(node).output_directories == (str(tmp_path / "renders"),)


@pytest.mark.parametrize("relative", ["", "sub", ".."])
def test_an_output_directory_that_overlaps_the_working_directory_is_refused(
    scene: FakeHou, relative: str
) -> None:
    """A task's scan walks the working directory and the folder, so an overlap records a file twice."""
    node: FakeNode = _settings_node(working_dir="$HIP")
    _set_list_parm(node, "output_directories", "output_dir", [os.path.join("$HIP", relative)])
    with pytest.raises(CookSetupError, match=f"{SCHEDULER_PATH}/output_dir1 .*working directory"):
        read_scheduler_settings(node)


@pytest.mark.parametrize("order", [("out", "out/frames"), ("out/frames", "out")])
def test_two_output_directories_that_hold_one_another_are_refused(
    scene: FakeHou, tmp_path: Path, order: Tuple[str, str]
) -> None:
    """A task's scan walks both, so it would record each file in the inner one twice."""
    node: FakeNode = _settings_node(working_dir="$HIP")
    _set_list_parm(
        node, "output_directories", "output_dir", [str(tmp_path / path) for path in order]
    )
    with pytest.raises(CookSetupError, match="output_dir1.*output_dir2.*hold one another"):
        read_scheduler_settings(node)


def test_an_output_directory_at_home_is_refused_naming_its_parm(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each task would upload everything it finds there, which is the whole home directory."""
    # Home is the only output folder on the tab.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    node: FakeNode = _settings_node(working_dir="$HIP")
    _set_list_parm(node, "output_directories", "output_dir", [str(tmp_path / "home")])
    with pytest.raises(CookSetupError, match="output_dir1"):
        read_scheduler_settings(node)


class _StorageProfile:
    """The queue's LOCAL storage profile locations, counting how often they are read."""

    def __init__(self, *locations: str) -> None:
        self.locations: Tuple[str, ...] = locations
        self.reads: int = 0

    def __call__(self) -> Tuple[str, ...]:
        self.reads += 1
        return self.locations


def _folder_node(working_dir: str, folder: str) -> FakeNode:
    """A scheduler node with ``working_dir`` and one extra output folder."""
    node: FakeNode = _settings_node(working_dir=working_dir)
    _set_list_parm(node, "output_directories", "output_dir", [folder])
    return node


def test_an_output_folder_in_another_top_level_folder_is_refused_naming_the_fix(
    scene: FakeHou,
) -> None:
    """Job attachments would make "/" the job's root, so a worker maps every path, node paths too.

    On Windows the two paths land on one drive, which isn't refused, so the folder passes there.
    """
    # The working directory under /opt and the folder under /tmp, with no storage profile.
    profile: _StorageProfile = _StorageProfile()
    node: FakeNode = _folder_node("/opt/studio/cooks/shot", "/tmp/pdg-extra")
    if os.name == "nt":
        settings: SchedulerSettings = read_scheduler_settings(node, read_local_locations=profile)
        assert settings.output_directories == (os.path.abspath("/tmp/pdg-extra"),)
        assert profile.reads == 0
        return
    with pytest.raises(CookSetupError) as raised:
        read_scheduler_settings(node, read_local_locations=profile)
    text: str = str(raised.value)
    assert f"{SCHEDULER_PATH}/output_dir1" in text
    assert os.path.abspath("/tmp/pdg-extra") in text
    assert "only '/'" in text and "same top-level folder as the working directory" in text
    assert "storage profile" in text and profile.reads == 1


def test_an_output_folder_under_the_working_directorys_top_level_folder_passes(
    scene: FakeHou,
) -> None:
    """They share /opt, so the job's root is /opt, and the queue's storage profile isn't read."""
    # Both under /opt, so the folder is kept and the farm is never asked.
    profile: _StorageProfile = _StorageProfile()
    node: FakeNode = _folder_node("/opt/studio/cooks/shot", "/opt/studio/pdg-extra")
    settings: SchedulerSettings = read_scheduler_settings(node, read_local_locations=profile)
    assert settings.output_directories == (os.path.abspath("/opt/studio/pdg-extra"),)
    assert profile.reads == 0


@pytest.mark.parametrize(
    "locations, passes",
    [
        (("/opt/studio", "/tmp"), True),
        # The longest location wins, as in the library, so these still split the two.
        (("/opt", "/opt/studio", "/tmp/pdg-extra"), True),
        # One side alone gives that side its own root, and the other side then gets its own.
        (("/opt/studio",), True),
        (("/tmp",), True),
        # A "/" location holds only the folder, because the working directory's is longer.
        (("/opt/studio", "/"), True),
        # One location over both still leaves "/" as the root.
        (("/",), False),
        ((), False),
    ],
)
def test_a_storage_profile_location_for_either_side_lets_the_folder_through(
    scene: FakeHou, locations: Tuple[str, ...], passes: bool
) -> None:
    """A LOCAL location for either side, and not over both, makes job attachments split the roots.

    On Windows the two paths share a drive, which isn't refused, so every row passes there.
    """
    node: FakeNode = _folder_node("/opt/studio/cooks/shot", "/tmp/pdg-extra")

    def read() -> SchedulerSettings:
        profile: _StorageProfile = _StorageProfile(*(os.path.abspath(p) for p in locations))
        return read_scheduler_settings(node, read_local_locations=profile)

    if passes or os.name == "nt":
        assert read().output_directories == (os.path.abspath("/tmp/pdg-extra"),)
    else:
        with pytest.raises(CookSetupError, match="output_dir1"):
            read()


@pytest.mark.parametrize(
    "folders, locations, passes",
    [
        # Job attachments put both folders in one group with no location, rooted at "/".
        (("/tmp/pdg-extra", "/var/pdg-extra"), ("/opt/studio",), False),
        # The location holds only the working directory, so the other two share only "/".
        (("/opt/pdg-near", "/tmp/pdg-extra"), ("/opt/studio",), False),
        # Both folders under one "/" location, and the working directory in its own.
        (("/tmp/pdg-extra", "/var/pdg-extra"), ("/opt/studio", "/"), False),
        # A location for each folder gives each its own group, so nothing is rooted at "/".
        (("/tmp/pdg-extra", "/var/pdg-extra"), ("/opt/studio", "/tmp", "/var"), True),
    ],
)
def test_two_output_folders_grouped_at_slash_are_refused(
    scene: FakeHou, folders: Tuple[str, ...], locations: Tuple[str, ...], passes: bool
) -> None:
    """Job attachments group paths by LOCAL location, so two folders can root a group at "/".

    On Windows the paths share a drive, which isn't refused, so every row passes there.
    """
    node: FakeNode = _settings_node(working_dir="/opt/studio/cooks/shot")
    _set_list_parm(node, "output_directories", "output_dir", list(folders))
    profile: _StorageProfile = _StorageProfile(*(os.path.abspath(p) for p in locations))
    if passes or os.name == "nt":
        settings: SchedulerSettings = read_scheduler_settings(node, read_local_locations=profile)
        assert settings.output_directories == tuple(sorted(os.path.abspath(f) for f in folders))
        return
    # Both folders are in the "/" group, so the refusal names both.
    with pytest.raises(CookSetupError, match="output_dir1.*output_dir2"):
        read_scheduler_settings(node, read_local_locations=profile)


@pytest.mark.parametrize(
    "paths, working_dir, folder, apart",
    [
        # Only "/" in common, which job attachments would make the job's root.
        (posixpath, "/opt/studio/shot", "/tmp/pdg-extra", True),
        (posixpath, "/opt/studio/shot", "/opt/pdg-extra", False),
        # Two drives get a root each, and a shared drive or share root isn't refused.
        (ntpath, r"C:\proj\shot", r"D:\pdg-extra", False),
        (ntpath, r"C:\proj\shot", r"C:\temp\pdg-extra", False),
        (ntpath, r"\\server\share\shot", r"\\server\share\pdg-extra", False),
        (ntpath, "/proj/shot", "/temp/pdg-extra", False),
    ],
)
def test_only_a_shared_posix_root_counts_as_apart(
    paths: types.ModuleType, working_dir: str, folder: str, apart: bool
) -> None:
    """A worker's "/" rule maps node paths such as /obj/pyro, and a drive or share rule can't."""
    assert host_module._shares_only_the_posix_root(working_dir, folder, paths) is apart


def test_the_cook_start_refuses_a_folder_in_another_top_level_folder(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cook reads its settings through the host, so a TOP Cook is refused like Submit.

    On Windows the two paths share a drive, which isn't refused, so the cook starts there.
    """
    # A workstation config with no storage profile, so nothing is asked of the farm.
    monkeypatch.setenv("DEADLINE_CONFIG_FILE_PATH", str(tmp_path / "deadline" / "config"))
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    network.scheduler_node.set_parm("working_dir", "/opt/studio/cooks/shot")
    _set_list_parm(network.scheduler_node, "output_directories", "output_dir", ["/tmp/pdg-extra"])
    if os.name == "nt":
        settings: SchedulerSettings = HoudiniPdgHost(network.scheduler()).read_settings()
        assert settings.output_directories == (os.path.abspath("/tmp/pdg-extra"),)
        return
    with pytest.raises(CookSetupError, match="output_dir1.*top-level folder"):
        HoudiniPdgHost(network.scheduler()).read_settings()


def test_the_host_reads_the_requirements_from_its_scheduler_node(scene: FakeHou) -> None:
    """The submitter asks the host, so the host must read the node the cook runs on."""
    # Copy a tab asking for one GPU onto the network's own scheduler node.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    node: FakeNode = _requirements_node(hr_mode="custom", hr_gpu_min=1)
    for parm in node.parms():
        network.scheduler_node.set_parm(parm.name(), parm.value, menu=parm.menu)
    assert HoudiniPdgHost(network.scheduler()).host_requirements() == {
        "amounts": [{"name": "amount.worker.gpu", "min": 1}]
    }
