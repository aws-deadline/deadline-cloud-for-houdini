# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler node's Farm and Queue menus, against fakes of deadline-cloud and ``hou``."""

from __future__ import annotations

import logging
import os
import sys
import threading
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

# The fakes come first, because they install the fake ``hou`` the host module imports.
from .houdini_fakes import FakeHou, install_hou

from deadline.houdini_pdg.houdini.host import FarmAndQueueMenus

_NONE_SELECTED: str = "<none selected>"
_LOG_IN: str = "Log in with Deadline Cloud Monitor or `deadline auth login`"
#: The entries a menu shows after the current value while it has no list, token then label.
_LOADING_ENTRY: List[str] = ["__loading__", "Loading..."]
_CHOOSE_A_FARM_ENTRY: List[str] = ["__choose_a_farm__", "Choose a farm first"]


class FakeAuthenticationStatus:
    """deadline-cloud's ``AwsAuthenticationStatus``, whose values a test compares by name."""

    AUTHENTICATED: str = "AUTHENTICATED"
    NEEDS_LOGIN: str = "NEEDS_LOGIN"


class FakeApi:
    """deadline-cloud's ``api`` module: the login check and the two list calls, each recorded."""

    AwsAuthenticationStatus: Any = FakeAuthenticationStatus

    def __init__(self) -> None:
        # A working login, two farms in two regions, and their queues, all unsorted. Two queues
        # share a name, given in reverse ID order. farm-c has no queues.
        self.status: str = FakeAuthenticationStatus.AUTHENTICATED
        self.farms: List[Dict[str, str]] = [
            {"farmId": "farm-b", "displayName": "Beta", "region": "us-west-2"},
            {"farmId": "farm-a", "displayName": "alpha", "region": "us-east-1"},
        ]
        self.queues: Dict[str, List[Dict[str, str]]] = {
            "farm-a": [{"queueId": "queue-a", "displayName": "Renders"}],
            "farm-b": [
                {"queueId": "queue-z", "displayName": "Sims"},
                {"queueId": "queue-y", "displayName": "Comp"},
                {"queueId": "queue-x", "displayName": "Sims"},
            ],
            "farm-c": [],
        }
        self.error: Optional[Exception] = None
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def check_authentication_status(self) -> str:
        self.calls.append(("check_authentication_status", {}))
        return self.status

    def list_farms(self) -> Dict[str, Any]:
        self.calls.append(("list_farms", {}))
        if self.error is not None:
            raise self.error
        return {"farms": list(self.farms)}

    def list_queues(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("list_queues", kwargs))
        return {"queues": list(self.queues[kwargs["farmId"]])}


class FakeConfig:
    """deadline-cloud's ``config`` module: settings read from a dict, and each write recorded."""

    def __init__(self, settings: Dict[str, str]) -> None:
        self.settings: Dict[str, str] = dict(settings)
        self.writes: List[Tuple[str, str]] = []

    def get_setting(self, name: str) -> str:
        return self.settings.get(name, "")

    def set_setting(self, name: str, value: str) -> None:
        self.writes.append((name, value))
        self.settings[name] = value


class FakeParm:
    """A string ``hou.Parm`` with a menu, which holds the picked token."""

    def __init__(self, value: str) -> None:
        self.value: str = value
        # Each value set, since Houdini may mark the scene unsaved on any set.
        self.sets: List[str] = []

    def evalAsString(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.sets.append(value)
        self.value = value


class FakeNode:
    """A scheduler node with its Farm and Queue menu parms."""

    def __init__(self, farm: str, queue: str) -> None:
        self.parms: Dict[str, FakeParm] = {"farm": FakeParm(farm), "queue": FakeParm(queue)}

    def parm(self, name: str) -> FakeParm:
        return self.parms[name]


class Menus:
    """The menus under test, with the fakes they read and the background jobs they started."""

    def __init__(self, settings: Dict[str, str]) -> None:
        self.api: FakeApi = FakeApi()
        self.config: FakeConfig = FakeConfig(settings)
        # The jobs stand in for the threads, and the deferred ones for Houdini's main thread.
        self.jobs: List[Callable[[], None]] = []
        self.deferred: List[Callable[[], None]] = []
        self.menus: FarmAndQueueMenus = FarmAndQueueMenus(
            api=self.api,
            config=self.config,
            start_thread=self.jobs.append,
            run_later_on_main_thread=self.deferred.append,
        )

    def run_jobs(self) -> None:
        """Run the started jobs, as their threads would."""
        # A job may start no other job, so the list is copied once.
        jobs: List[Callable[[], None]] = list(self.jobs)
        self.jobs.clear()
        for job in jobs:
            job()

    def run_deferred(self) -> None:
        """Run what the jobs queued for Houdini's main thread, in the order they queued it."""
        deferred: List[Callable[[], None]] = list(self.deferred)
        self.deferred.clear()
        for job in deferred:
            job()


@pytest.fixture
def scene(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHou:
    """A Houdini session with a UI."""
    hip: str = os.path.join(str(tmp_path), "scene", "shot.hip")
    os.makedirs(os.path.dirname(hip))
    return install_hou(monkeypatch, hip)


_SAVED: Dict[str, str] = {"defaults.farm_id": "farm-a", "defaults.queue_id": "queue-a"}
_LOGGED_IN: str = FakeAuthenticationStatus.AUTHENTICATED
_LOGGED_OUT: str = FakeAuthenticationStatus.NEEDS_LOGIN


@pytest.mark.parametrize(
    "parm, settings, shown",
    [
        ("farm", _SAVED, ["farm-a", "farm-a", *_LOADING_ENTRY]),
        ("queue", _SAVED, ["queue-a", "queue-a", *_LOADING_ENTRY]),
        ("queue", {}, ["", _NONE_SELECTED, *_CHOOSE_A_FARM_ENTRY]),
    ],
    ids=["farm", "queue", "queue with no farm"],
)
def test_on_a_cold_load_a_menu_lists_the_saved_id_then_loading_and_calls_nothing(
    scene: FakeHou, parm: str, settings: Dict[str, str], shown: List[str]
) -> None:
    """A menu runs on Houdini's main thread, so an AWS call there would freeze the UI.

    With no name seen yet, the saved ID is its own label, so Houdini shows the ID, not Loading...
    Queues are listed per farm, so with no farm the queue menu asks for one and fetches nothing.
    """
    # A fresh session, as when a scene opens, reads the menu before any fetch has run.
    menus: Menus = Menus(settings)
    saved: str = settings.get(f"defaults.{parm}_id", "")
    assert menus.menus.list_menu_items(parm, saved) == shown
    assert menus.api.calls == []
    assert len(menus.jobs) == (1 if settings else 0)


class FakeDeferEval:
    """``hdefereval`` with only ``executeDeferred``, which keeps each job for the test to run."""

    def __init__(self) -> None:
        self.queued: List[Callable[[], None]] = []
        self.called: threading.Event = threading.Event()

    def executeDeferred(self, job: Callable[[], None]) -> None:
        self.queued.append(job)
        self.called.set()


def test_the_default_fetch_runs_in_a_daemon_thread_and_queues_the_queue_set_for_every_node(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch on the main thread freezes Houdini, and a non-daemon one can stop it from exiting.

    A parm set from the fetch thread is unsafe in Houdini, and a wait on the main thread can hang it.
    """
    # The fake login check records its thread. The fake hdefereval has no
    # executeInMainThreadWithResult, so a hand-off that waits fails here.
    threads: List[threading.Thread] = []

    class RecordingApi(FakeApi):
        def check_authentication_status(self) -> str:
            threads.append(threading.current_thread())
            return super().check_authentication_status()

    deferred: FakeDeferEval = FakeDeferEval()
    monkeypatch.setitem(sys.modules, "hdefereval", deferred)
    # Every scheduler node shares the saved farm and queue, so each one's Queue is set.
    nodes: List[FakeNode] = [FakeNode("farm-a", ""), FakeNode("farm-a", "")]
    scene.instances = list(nodes)
    menus: FarmAndQueueMenus = FarmAndQueueMenus(api=RecordingApi(), config=FakeConfig(_SAVED))
    menus.list_menu_items("queue", "")
    assert deferred.called.wait(timeout=10)
    assert threads[0] is not threading.main_thread() and threads[0].daemon
    # The fetch only queued the set, and the main thread's run of it sets both nodes.
    assert [node.parm("queue").evalAsString() for node in nodes] == ["", ""]
    assert len(deferred.queued) == 1
    deferred.queued[0]()
    assert [node.parm("queue").evalAsString() for node in nodes] == ["queue-a", "queue-a"]


def _loaded(settings: Dict[str, str]) -> Menus:
    """Menus with the farm list and the saved farm's queue list loaded."""
    # Each menu starts its list, the jobs fill them, and the main thread takes what they queued.
    menus: Menus = Menus(settings)
    menus.menus.list_menu_items("farm", "")
    menus.menus.list_menu_items("queue", "")
    menus.run_jobs()
    menus.run_deferred()
    return menus


def _shown_queue(menus: Menus, node: FakeNode) -> str:
    """The Queue label Houdini shows: the entry whose token the parm holds, else the first entry."""
    # Houdini's menu list alternates tokens and labels, and a value that is no token shows the
    # first label, which made Queue look set to the first queue when it held none.
    value: str = node.parm("queue").evalAsString()
    items: List[str] = menus.menus.list_menu_items("queue", value)
    labels: Dict[str, str] = dict(zip(items[::2], items[1::2]))
    return labels.get(value, items[1])


def _pick_farm(menus: Menus, node: FakeNode, farm_id: str) -> None:
    """Pick ``farm_id`` in the node's Farm menu, with the update putting the saved IDs back."""
    node.parm("farm").set(farm_id)
    menus.menus.save_pick(node, "farm", lambda: menus.menus.show_saved_ids(node))


def test_farm_and_queue_picks_save_as_deadline_cloud_does_and_queue_never_waits_on_loading(
    scene: FakeHou,
) -> None:
    """deadline-cloud's select_farm clears the old farm's queue and storage profile, in this order.

    Its select_queue clears the old queue's storage profile after saving the queue. After a farm
    pick, Queue stayed on Loading... once the new farm's queues landed, and showed Loading...
    until clicked, because nothing fetched the new farm's queues.
    """
    menus: Menus = _loaded(_SAVED)
    # Load farm-b's queues too, so the pick has a list to clear.
    menus.config.settings["defaults.farm_id"] = "farm-b"
    menus.menus.list_menu_items("queue", "")
    menus.run_jobs()
    # Pick farm-b. The update records what was saved when it runs, then puts the IDs back.
    menus.config.settings["defaults.farm_id"] = "farm-a"
    node: FakeNode = FakeNode("farm-b", "queue-a")
    updates: List[List[Tuple[str, str]]] = []

    def update() -> None:
        updates.append(list(menus.config.writes))
        menus.menus.show_saved_ids(node)

    menus.menus.save_pick(node, "farm", update)
    farm_writes: List[Tuple[str, str]] = [
        ("defaults.farm_id", "farm-b"),
        ("defaults.farm_region", "us-west-2"),
        ("defaults.queue_id", ""),
        ("settings.storage_profile_id", ""),
    ]
    assert menus.config.writes == farm_writes
    # The update ran once, after every write, and the new farm's queues load again at once.
    assert updates == [farm_writes]
    assert len(menus.jobs) == 1
    # Queue holds no queue, so it shows none selected while they load.
    queue_items: List[str] = menus.menus.list_menu_items("queue", node.parm("queue").evalAsString())
    assert queue_items == ["", _NONE_SELECTED, *_LOADING_ENTRY]
    assert _shown_queue(menus, node) == _NONE_SELECTED
    # Houdini runs a menu on every redraw, so those menu calls start no second fetch.
    assert len(menus.jobs) == 1
    # The fetch reads the new farm's queues in its own region. Once they load, pick queue-y.
    menus.run_jobs()
    assert menus.api.calls[-1] == ("list_queues", {"farmId": "farm-b", "region": "us-west-2"})
    node.parm("queue").set("queue-y")
    menus.menus.save_pick(node, "queue", update)
    queue_writes: List[Tuple[str, str]] = [
        ("defaults.queue_id", "queue-y"),
        ("settings.storage_profile_id", ""),
    ]
    assert menus.config.writes == farm_writes + queue_writes
    assert updates == [farm_writes, farm_writes + queue_writes]


_NO_QUEUE: Dict[str, str] = {"defaults.farm_id": "farm-a", "defaults.queue_id": ""}


@pytest.mark.parametrize(
    "parm, settings, status, error, token",
    [
        ("farm", _SAVED, _LOGGED_IN, None, "__loading__"),
        ("queue", _SAVED, _LOGGED_IN, None, "__loading__"),
        ("farm", _SAVED, _LOGGED_OUT, None, "__log_in__"),
        ("queue", _SAVED, _LOGGED_OUT, None, "__log_in__"),
        ("farm", _SAVED, _LOGGED_IN, RuntimeError("x"), "__list_error__"),
        ("queue", {}, _LOGGED_IN, None, "__choose_a_farm__"),
        ("farm", _SAVED, _LOGGED_IN, None, ""),
        ("queue", _SAVED, _LOGGED_IN, None, ""),
        ("farm", _NO_QUEUE, _LOGGED_IN, None, ""),
        ("queue", _NO_QUEUE, _LOGGED_IN, None, ""),
        ("queue", {"defaults.farm_id": "farm-c"}, _LOGGED_IN, None, ""),
        ("farm", _SAVED, _LOGGED_IN, None, "farm-a"),
    ],
    ids=[
        "farm loading",
        "queue loading",
        "farm login",
        "queue login",
        "error",
        "no farm",
        "farm none selected",
        "queue none selected",
        "farm none selected, no queue saved",
        "queue none selected, no queue saved",
        "queue none selected, a farm with no queues",
        "the saved farm",
    ],
)
def test_picking_a_placeholder_none_selected_or_the_saved_farm_saves_nothing(
    scene: FakeHou,
    parm: str,
    settings: Dict[str, str],
    status: str,
    error: Optional[Exception],
    token: str,
) -> None:
    """Those entries aren't a new farm or queue, so a pick must not save them or run the update.

    Saving the saved farm again would also clear its queue, as select_farm always does.
    """
    # Show the Loading entry, or load the list first to show the other entries.
    menus: Menus = Menus(settings)
    menus.api.status = status
    menus.api.error = error
    saved: str = settings.get(f"defaults.{parm}_id", "")
    menus.menus.list_menu_items(parm, saved)
    if token != "__loading__":
        menus.run_jobs()
    # A placeholder follows the saved value. A loaded list starts with a none selected entry
    # whose token is empty, since without it Houdini showed the first farm or queue as set.
    items: List[str] = menus.menus.list_menu_items(parm, saved)
    assert items[:2] == ["", _NONE_SELECTED] if token == "" else items[2] == token
    # Houdini sets the parm to the entry's token, then runs the callback.
    node: FakeNode = FakeNode(settings.get("defaults.farm_id", ""), saved)
    node.parm(parm).set(token)
    updates: List[str] = []
    menus.menus.save_pick(node, parm, lambda: updates.append("update"))
    # Nothing is saved or updated, and the parm holds the saved ID again.
    assert menus.config.writes == [] and updates == []
    assert node.parm(parm).evalAsString() == saved


@pytest.mark.parametrize(
    "then", [None, ("<refreshing>",) * 2, (_NONE_SELECTED,) * 2, ("farm-a", "queue-a")]
)
def test_after_a_reload_each_menu_names_the_saved_ids_before_the_new_lists_land(
    scene: FakeHou, then: Optional[Tuple[str, str]]
) -> None:
    """After Update Queue Parameters, both menus stayed on Loading... once their lists landed.

    Houdini doesn't run the menu script again when a list lands, so its first run must already
    show the saved farm and queue by name. A reload drops the lists for a new login, but a name
    seen for an ID, in a list or written by the ROP's update, is still that ID's name. A later
    update that writes the ROP's own text or the ID mustn't replace it.
    """
    # Name both IDs, then Update Queue Parameters reloads the lists, and they haven't landed.
    menus: Menus
    names: Tuple[str, str]
    if then is None:
        menus, names = _loaded(_SAVED), ("alpha", "Renders")
    else:
        menus, names = Menus(_SAVED), ("Main Farm", "Renders Queue")
        menus.menus.show_saved_ids(FakeNode(*names))
        menus.menus.show_saved_ids(FakeNode(*then))
    menus.menus.load_lists(reload=True)
    assert len(menus.jobs) == 2
    for parm, saved, name in (("farm", "farm-a", names[0]), ("queue", "queue-a", names[1])):
        assert menus.menus.list_menu_items(parm, saved) == [saved, name, *_LOADING_ENTRY], parm


@pytest.mark.parametrize(
    "farms", [["farm-b"], ["farm-b", "farm-a"], ["farm-a", "farm-b"]], ids=["b", "b, a", "a, b"]
)
def test_after_farm_picks_and_their_queues_landing_queue_is_empty_and_never_a_listed_queue(
    scene: FakeHou, farms: List[str]
) -> None:
    """After a farm pick, and after a farm and back, Queue showed the first queue or Loading...

    A farm pick clears the queue, and the ROP's none selected text is no menu token, so Queue
    must hold the empty token. A listed queue in the settings or in Queue would be wrong.
    """
    # Pick the farms in turn, with every queue list landing, and the saved queue listed at first.
    menus: Menus = _loaded(_SAVED)
    node: FakeNode = FakeNode("farm-a", "queue-a")
    scene.instances = [node]
    for farm_id in farms:
        _pick_farm(menus, node, farm_id)
        menus.run_jobs()
        menus.run_deferred()
    assert node.parm("queue").evalAsString() == ""
    assert _shown_queue(menus, node) == _NONE_SELECTED
    listed: List[str] = [
        queue["queueId"] for queues in menus.api.queues.values() for queue in queues
    ]
    saved_queues: List[str] = [
        value for name, value in menus.config.writes if name == "defaults.queue_id"
    ]
    assert saved_queues and not set(saved_queues) & set(listed)
    assert not set(node.parm("queue").sets) & set(listed)


def test_without_a_ui_nothing_is_fetched_or_watched(scene: FakeHou) -> None:
    """hython loads the scene on the farm too, where the lists have no use."""
    # With no UI, a load and a menu start no job and add no selection callback.
    scene.ui = False
    menus: Menus = Menus(_SAVED)
    menus.menus.load_lists()
    menus.menus.list_menu_items("farm", "farm-a")
    assert menus.jobs == [] and scene.selection_callbacks == []
    assert menus.deferred == []


def _item(type_name: str) -> Any:
    """A selected network item of the given node type."""
    return types.SimpleNamespace(type=lambda: types.SimpleNamespace(name=lambda: type_name))


def test_selecting_a_scheduler_node_starts_the_fetch(scene: FakeHou) -> None:
    """Selecting the node starts its lists, so they are often ready when its parms show.

    The list it loads is sorted by name, and two entries can share a name, so those are in ID
    order and don't change from one load to the next.
    """
    # Two loads and their jobs, which load both lists.
    menus: Menus = Menus(_SAVED)
    menus.menus.load_lists()
    menus.menus.load_lists()
    menus.run_jobs()
    # The selection callback is added once per session.
    assert len(scene.selection_callbacks) == 1
    # Another tool saves a new farm, so its queues aren't loaded yet.
    menus.config.settings["defaults.farm_id"] = "farm-b"
    scene.selection_callbacks[0]([_item("geo")])
    assert menus.jobs == []
    scene.selection_callbacks[0]([_item("geo"), _item("deadlinecloudscheduler")])
    assert len(menus.jobs) == 1
    menus.run_jobs()
    assert menus.menus.list_menu_items("queue", "")[2:] == [
        "queue-y",
        "Comp",
        "queue-x",
        "Sims",
        "queue-z",
        "Sims",
    ]


@pytest.mark.parametrize(
    "queue_id, status, shown, menu",
    [
        ("queue-a", _LOGGED_IN, "queue-a", ["", _NONE_SELECTED, "queue-a", "Renders"]),
        ("queue-gone", _LOGGED_IN, "", ["", _NONE_SELECTED, "queue-a", "Renders"]),
        ("queue-a", _LOGGED_OUT, "queue-a", ["queue-a", "queue-a", "__log_in__", _LOG_IN]),
    ],
    ids=["listed", "unlisted", "logged out"],
)
def test_a_queue_that_already_shows_the_saved_queue_is_not_set_again(
    scene: FakeHou, queue_id: str, status: str, shown: str, menu: List[str]
) -> None:
    """Houdini may mark a scene unsaved on any parm set, so a scene that just opened stays saved.

    A saved queue the farm doesn't list must not stay in Queue, but a login entry is no list,
    so logged out Queue keeps the saved queue and its menu lists the login entry after it.
    """
    # One node already holds what Queue must show, and the other an old queue.
    menus: Menus = Menus({**_SAVED, "defaults.queue_id": queue_id})
    menus.api.status = status
    unchanged: FakeNode = FakeNode("farm-a", shown)
    changed: FakeNode = FakeNode("farm-a", "queue-old")
    scene.instances = [unchanged, changed]
    menus.menus.list_menu_items("queue", queue_id)
    menus.run_jobs()
    menus.run_deferred()
    assert unchanged.parm("queue").sets == []
    assert changed.parm("queue").sets == [shown]
    assert menus.menus.list_menu_items("queue", shown)[:4] == menu


# -- the menus' log lines --------------------------------------------------------

#: The package logger the artist sets to INFO in Houdini's Python Shell to see these lines.
_PACKAGE_LOGGER: str = "deadline.houdini_pdg"
_PREFIX: str = "Farm and Queue menus: "


def _menu_lines(
    caplog: pytest.LogCaptureFixture, part: str = "", level: int = logging.INFO
) -> List[str]:
    """The menus' lines at ``level``, without their prefix, that contain ``part``."""
    # Only lines from the host module's logger count, as the console would show them.
    lines: List[str] = [
        record.getMessage()[len(_PREFIX) :]
        for record in caplog.records
        if record.name == "deadline.houdini_pdg.houdini.host"
        and record.levelno == level
        and record.getMessage().startswith(_PREFIX)
    ]
    return [line for line in lines if part in line]


@pytest.mark.parametrize(
    "status, error, farms, queues",
    [
        (_LOGGED_IN, None, "2 item(s)", "1 item(s)"),
        (_LOGGED_OUT, None, "the login entry", "the login entry"),
        (_LOGGED_IN, RuntimeError("no network"), "the error entry (no network)", "1 item(s)"),
    ],
    ids=["lists", "login", "error"],
)
def test_a_fetch_logs_one_info_line_naming_what_landed_and_its_start_only_at_debug(
    scene: FakeHou,
    caplog: pytest.LogCaptureFixture,
    status: str,
    error: Optional[Exception],
    farms: str,
    queues: str,
) -> None:
    """The console must show each fetch that landed, without a line for every step around it.

    A fetch that lands with one entry is no list, so its line says which entry it shows. Setting
    only the package logger to INFO must be enough, as in the Python Shell.
    """
    # Both lists start and land, with the package logger at DEBUG and nothing else set.
    caplog.set_level(logging.DEBUG, logger=_PACKAGE_LOGGER)
    menus: Menus = Menus(_SAVED)
    menus.api.status = status
    menus.api.error = error
    menus.menus.load_lists()
    menus.run_jobs()
    assert _menu_lines(caplog) == [
        f"loaded the farms: {farms}.",
        f"loaded the queues of farm farm-a: {queues}.",
    ]
    assert _menu_lines(caplog, "loading the", logging.DEBUG) == [
        "started loading the farms.",
        "started loading the queues of farm farm-a.",
    ]


@pytest.mark.parametrize(
    "then, saved, line",
    [
        ("nothing", "'farm-b', queue 'queue-y'", "Queue shows 'queue-y', set on 1 node(s)."),
        (
            "pick farm-a",
            "'farm-a', queue ''",
            "The queues of farm 'farm-b' aren't the saved farm's, so no node is set.",
        ),
        (
            "reload",
            "'farm-b', queue 'queue-y'",
            "The queues of farm 'farm-b' aren't loaded, so no node is set.",
        ),
    ],
)
def test_a_landed_queue_list_logs_what_it_set_or_why_it_set_nothing_at_debug(
    scene: FakeHou, caplog: pytest.LogCaptureFixture, then: str, saved: str, line: str
) -> None:
    """The line must tell a Queue left unset apart from one set, and from a list that was skipped.

    A list for another farm, or one a reload dropped, sets nothing, and the line says which.
    """
    # Pick farm-b, save its queue-y, and let the list land: one node changes, one already shows it.
    menus: Menus = _loaded(_SAVED)
    changed: FakeNode = FakeNode("farm-a", "queue-a")
    unchanged: FakeNode = FakeNode("farm-b", "queue-y")
    scene.instances = [changed, unchanged]
    _pick_farm(menus, changed, "farm-b")
    menus.config.settings["defaults.queue_id"] = "queue-y"
    unchanged.parm("queue").set("queue-y")
    menus.run_jobs()
    # Then pick farm-a or reload before the main thread runs, or neither.
    if then == "reload":
        menus.menus.load_lists(reload=True)
    elif then == "pick farm-a":
        _pick_farm(menus, changed, "farm-a")
    caplog.set_level(logging.DEBUG, logger=_PACKAGE_LOGGER)
    caplog.clear()
    menus.run_deferred()
    assert _menu_lines(caplog, "saved farm", logging.DEBUG) == [f"saved farm {saved}. {line}"]
    assert _menu_lines(caplog, "saved farm") == []


def _logged_menu(
    menus: Menus, parm: str, current: str, caplog: pytest.LogCaptureFixture
) -> List[str]:
    """The INFO lines that one call of ``parm``'s menu logs."""
    caplog.clear()
    menus.menus.list_menu_items(parm, current)
    return _menu_lines(caplog)


def test_each_menu_call_logs_one_line_naming_its_entries_and_the_shown_label(
    scene: FakeHou, caplog: pytest.LogCaptureFixture
) -> None:
    """A menu that shows the wrong label for its value would show here, with what it listed."""
    caplog.set_level(logging.INFO, logger=_PACKAGE_LOGGER)
    # Before and after the lists load, with a value no entry has, and with no farm saved.
    menus: Menus = Menus(_SAVED)
    assert _logged_menu(menus, "farm", "farm-a", caplog) == [
        "the farm menu shows \"Loading...\", and its value 'farm-a' shows as 'farm-a'."
    ]
    menus.run_jobs()
    assert _logged_menu(menus, "farm", "farm-a", caplog) == [
        "the farm menu shows the loaded list of 2 item(s), and its value 'farm-a' shows as 'alpha'."
    ]
    assert _logged_menu(menus, "farm", "farm-gone", caplog) == [
        "the farm menu shows the loaded list of 2 item(s), and its value 'farm-gone' shows as"
        " no entry."
    ]
    menus.config.settings["defaults.farm_id"] = ""
    assert _logged_menu(menus, "queue", "", caplog) == [
        "the queue menu shows \"Choose a farm first\", and its value '' shows as"
        " '<none selected>'."
    ]
    # Logged out, the farm list lands as the login entry, which the line names like a placeholder.
    menus.api.status = _LOGGED_OUT
    menus.menus.load_lists(reload=True)
    menus.run_jobs()
    assert _logged_menu(menus, "farm", "farm-a", caplog) == [
        f"the farm menu shows \"{_LOG_IN}\", and its value 'farm-a' shows as 'alpha'."
    ]
