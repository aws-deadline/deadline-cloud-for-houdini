# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The registration module, which builds a cook's objects, against fakes of ``pdg``, ``hou`` and Deadline Cloud."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from deadline.client.api import _telemetry

from .houdini_fakes import (
    COOKING_EXAMPLE,
    PDG,
    PYRO_BONFIRE,
    FakeHou,
    FakeNetwork,
    build_network,
    install_hou,
)

import deadline.houdini_pdg
from deadline.houdini_pdg.cook import CookServices, ScheduleResult, SystemClock
from deadline.houdini_pdg.errors import CookSetupError
from deadline.houdini_pdg.houdini import host as host_module
from deadline.houdini_pdg.houdini.host import HoudiniPdgHost
from deadline.houdini_pdg.houdini.settings import SchedulerSettings
from deadline.houdini_pdg.ports import CookPhase, CookSummary

#: The registration module's source, found from the installed package. PDG loads it by path, so the test does too.
_REGISTRATION: str = os.path.join(
    os.path.dirname(deadline.houdini_pdg.__file__),
    *("houdini_plugin", "pdg", "types", "deadlinecloud", "__init__.py"),
)


def _load() -> ModuleType:
    """The registration module, loaded from its file the way PDG loads it."""
    spec: Any = importlib.util.spec_from_file_location("deadlinecloud_under_test", _REGISTRATION)
    assert spec is not None and spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGISTRATION: ModuleType = _load()

#: The client library's cache of telemetry clients, by package name.
_TELEMETRY_CLIENTS: Dict[str, Any] = getattr(_telemetry, "__cached_telemetry_clients")


def _client_error(code: str) -> ClientError:
    """A botocore error with the service code ``code``."""
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


class FakeS3:
    """An S3 client whose ``get_object`` raises ``error``, and that records the keys it was asked for."""

    def __init__(self, error: Optional[Exception]) -> None:
        self.error: Optional[Exception] = error
        self.keys: List[Tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> Dict[str, Any]:
        self.keys.append((Bucket, Key))
        if self.error is not None:
            raise self.error
        return {"Body": b""}


class FakeDeadlineClient:
    """A Deadline Cloud client whose ``get_queue`` gives each answer in turn: a queue or an error."""

    def __init__(self, answers: List[Any]) -> None:
        self.answers: List[Any] = answers

    def get_queue(self, *, farmId: str, queueId: str) -> Dict[str, Any]:
        answer: Any = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


_QUEUE: Dict[str, Any] = {
    "displayName": "Render",
    "jobAttachmentSettings": {"s3BucketName": "bucket-a", "rootPrefix": "DeadlineCloud"},
}


class FakeCloud(REGISTRATION._DeadlineCloudCalls):  # type: ignore[misc, name-defined]
    """Clients with a fixed farm, queue, S3 answer, and telemetry, and no waiting."""

    def __init__(
        self,
        *,
        settings: Optional[Dict[str, str]] = None,
        queue: Optional[List[Any]] = None,
        s3_error: Optional[Exception] = _client_error("NoSuchKey"),
        login_needed: bool = False,
    ) -> None:
        self.settings: Dict[str, str] = (
            settings
            if settings is not None
            else {"defaults.farm_id": "farm-1", "defaults.queue_id": "queue-1"}
        )
        self.client: FakeDeadlineClient = FakeDeadlineClient(queue or [_QUEUE])
        self.s3: FakeS3 = FakeS3(s3_error)
        self.events: List[Tuple[str, Dict[str, Any]]] = []
        self.sleeps: List[float] = []
        self.login_needed: bool = login_needed
        self.session_names: List[str] = []

    def get_setting(self, name: str) -> str:
        return self.settings.get(name, "")

    def deadline_client(self) -> Any:
        return self.client

    def queue_session(self, client: Any, farm_id: str, queue_id: str, display_name: str) -> Any:
        self.session_names.append(display_name)
        return type("Session", (), {"client": lambda _self, name: self.s3})()

    def needs_login(self) -> bool:
        return self.login_needed

    def telemetry_recorder(self) -> Callable[[str, Dict[str, Any]], Any]:
        return lambda event_type, details: self.events.append((event_type, details))

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture
def scene(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHou:
    """A saved scene at ``<tmp>/scene/shot.hip``, in hython, with no UI."""
    hip: str = os.path.join(str(tmp_path), "scene", "shot.hip")
    os.makedirs(os.path.dirname(hip))
    monkeypatch.delenv("DEADLINE_PDG_DEBUG_LEVEL", raising=False)
    monkeypatch.setenv("HIP", os.path.dirname(hip))
    # A stop an earlier test left behind would turn this test's cook into a refusal.
    host_module.submit_stops.clear()
    state: FakeHou = install_hou(monkeypatch, hip)
    state.ui = False
    return state


def _scheduler(network: FakeNetwork, cloud: Optional[FakeCloud] = None) -> Any:
    """The registered scheduler for ``network``, using ``cloud`` for every Deadline Cloud call."""
    chosen: FakeCloud = cloud or FakeCloud()

    class Scheduler(REGISTRATION.DeadlineCloudPDGScheduler):  # type: ignore[misc, name-defined]
        def _clients(self) -> Any:
            return chosen

    scheduler: Any = Scheduler(None, "deadlinecloudscheduler1")
    scheduler.context = network.context
    scheduler.node = network.scheduler_node
    return scheduler


def _connect(network: FakeNetwork, cloud: FakeCloud, tmp_path: Path) -> CookServices:
    """The services the composition root builds for one cook."""
    host: HoudiniPdgHost = HoudiniPdgHost(_scheduler(network, cloud))
    settings: SchedulerSettings = SchedulerSettings(working_dir=str(tmp_path / "scene"))
    services: CookServices = REGISTRATION._connect(
        settings, "cook-1", host=host, clock=SystemClock(), clients=cloud
    )
    return services


# -- start, tick and stop -----------------------------------------------------


@pytest.mark.parametrize(
    "cook_type, text",
    [
        ("Service", "render: This node's work items cook in a long-running service process"),
        ("InProcess", "render: This node runs a command, but its cook type is In-Process"),
    ],
    ids=["Service", "InProcess"],
)
def test_a_refused_graph_stops_pdg_at_its_start(scene: FakeHou, cook_type: str, text: str) -> None:
    """PDG started a Service node's processes after a True start, and still said Out-of-Process.

    Every graph refusal returns False, so PDG starts nothing, and the node keeps the refusal.
    """
    # Service is set on the parm while PDG still says Out-of-Process. In-Process is PDG's own.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    network.node("render").cookType = "OutOfProcess" if cook_type == "Service" else cook_type
    network.node("render").hom.set_parm("pdg_cooktype", 3 if cook_type == "Service" else 1)
    scheduler: Any = _scheduler(network)
    assert scheduler.onStartCook(False, None) is False
    (error,) = [call[1] for call in scheduler.calls if call[0] == "cookError"]
    assert text in error
    assert not [call for call in scheduler.calls if call[0] == "setWorkingDir"]


def test_a_refused_start_sends_its_advisories_to_the_warning_tab(scene: FakeHou) -> None:
    """A refused start showed only its refusals, so an advisory naming a likely cause was lost.

    The working directory is away from the scene, so the start's verdict also has the
    scene-directory advisory, which must go to the Warning tab next to the refusal.
    """
    # render is In-Process, which is refused, and the working directory is not the scene's.
    network: FakeNetwork = build_network(
        PYRO_BONFIRE, scheduler_parms={"working_dir": "/tmp/elsewhere", "validate_graph": 1}
    )
    network.node("render").cookType = "InProcess"
    scheduler: Any = _scheduler(network)
    assert scheduler.onStartCook(False, None) is False
    (error,) = [call[1] for call in scheduler.calls if call[0] == "cookError"]
    (warning,) = [call[1] for call in scheduler.calls if call[0] == "cookWarning"]
    assert "render:" in error and "The scene is in" not in error
    assert warning.startswith(f"{os.environ['HIP']}: The scene is in ")
    # Windows puts the current drive on a rooted path that has none, and uses backslashes.
    drive: str = os.path.splitdrive(os.getcwd())[0]
    assert f"'{drive}{os.sep}tmp{os.sep}elsewhere'" in warning


def test_a_start_that_fails_on_a_bad_setting_still_returns_true(scene: FakeHou) -> None:
    """Only a graph refusal may return False. A setup error cancels on the first tick."""
    # A Debug Level of 5 raises a setup error after every check passed.
    network: FakeNetwork = build_network(
        PYRO_BONFIRE, scheduler_parms={"working_dir": "$HIP", "debug_level": 5, "validate_graph": 1}
    )
    scheduler: Any = _scheduler(network)
    assert scheduler.onStartCook(False, None) is True
    (error,) = [call[1] for call in scheduler.calls if call[0] == "cookError"]
    assert "debug_level" in error and scheduler.onTick() == PDG.tickResult.SchedulerCancelCook


#: The ``pdg.cookType`` members, with their values, as Houdini 21.0 lists them.
#: StaticDepsGen is left out: it has GenerateFull's value, and Houdini names it GenerateFull.
_GENERATE_TYPES: List[Tuple[str, int]] = [
    ("GenerateFull", 2),
    ("GenerateNode", 3),
    ("GenerateStatic", 4),
]
_COOK_TYPES: List[Tuple[str, int]] = [
    ("Full", 0),
    ("Node", 1),
    ("StaticDepsFull", 5),
    ("StaticDepsNode", 6),
]


def _printed_and_named(members: List[Tuple[str, int]]) -> List[Any]:
    """Each member in its printed form and as an enum with a ``name`` and a ``value``."""
    return [f"cookType.{name}" for name, _ in members] + [
        types.SimpleNamespace(name=name, value=value) for name, value in members
    ]


@pytest.mark.parametrize("cook_type", _printed_and_named(_GENERATE_TYPES), ids=str)
def test_a_generate_houdini_reports_in_the_cook_options_is_not_a_farm_cook(
    scene: FakeHou, tmp_path: Path, cook_type: Any
) -> None:
    """Houdini starts every generate with static False, so a farm cook would need a farm to generate."""
    network: FakeNetwork = build_network(COOKING_EXAMPLE)
    network.context.cookOptions = types.SimpleNamespace(cookType=cook_type)
    # No farm is set, so a farm cook would fail on the missing farm ID.
    scheduler: Any = _scheduler(network, FakeCloud(settings={}))
    assert scheduler.onStartCook(False, None) is True
    folder: str = str(tmp_path / "scene")
    assert ("setWorkingDir", folder, folder) in scheduler.calls
    assert not [call for call in scheduler.calls if call[0] == "cookError"]
    assert scheduler.onTick() == PDG.tickResult.SchedulerReady


@pytest.mark.parametrize("cook_type", [*_printed_and_named(_COOK_TYPES), None], ids=str)
def test_a_cook_houdini_reports_in_the_cook_options_is_checked_and_connected(
    scene: FakeHou, cook_type: Any
) -> None:
    """Only a generate skips the checks, so a cook of a refused graph still fails its start.

    A context with no cook options can't show a generate, so it is a cook too.
    """
    # The same refused graph, with Houdini's cook options saying it's a cook, or missing.
    network: FakeNetwork = build_network(COOKING_EXAMPLE)
    network.context.cookOptions = (
        None if cook_type is None else types.SimpleNamespace(cookType=cook_type)
    )
    scheduler: Any = _scheduler(network)
    scheduler.onStartCook(False, None)
    errors: List[str] = [call[1] for call in scheduler.calls if call[0] == "cookError"]
    assert len(errors) == 1 and "python1" in errors[0]


def test_a_cook_started_in_a_session_with_a_ui_never_opens_the_save_prompt(scene: FakeHou) -> None:
    """Only the node's Submit asks to save. A cook started any other way refuses unsaved changes."""
    # Start a cook of an unsaved scene in a session with a UI, and expect a refusal, not a prompt.
    scene.ui = True
    scene.hip_file.unsaved = True
    scheduler: Any = _scheduler(build_network(PYRO_BONFIRE))
    scheduler.onStartCook(False, None)
    assert scene.messages.calls == [] and scene.hip_file.saves == 0
    errors: List[str] = [call[1] for call in scheduler.calls if call[0] == "cookError"]
    assert len(errors) == 1 and "unsaved" in errors[0].lower()


def test_the_scheduler_uses_the_client_library_by_default() -> None:
    """Every cook's farm calls go through this class, so without it no cook could connect."""
    scheduler: Any = REGISTRATION.DeadlineCloudPDGScheduler(None, "deadlinecloudscheduler1")
    assert isinstance(scheduler._clients(), REGISTRATION._DeadlineCloudCalls)


class FakeCore:
    """The cook's ``on_schedule``: records each handle, then answers ``result`` or raises ``error``."""

    def __init__(self, result: Any, error: Optional[Exception]) -> None:
        self.result: Any = result
        self.error: Optional[Exception] = error
        self.handles: List[Any] = []

    def on_schedule(self, handle: Any) -> Any:
        self.handles.append(handle)
        if self.error is not None:
            raise self.error
        return self.result

    def on_stop_cook(self, cancel: bool) -> bool:
        return True


def test_a_work_item_reaches_the_cook_with_its_node_and_declared_outputs(scene: FakeHou) -> None:
    """The cook names a task by its node and checks its declared outputs, so neither may be lost."""
    # Schedule one item with two outputs and one output with no path, through a recording core.
    scheduler: Any = _scheduler(build_network(PYRO_BONFIRE))
    core: FakeCore = FakeCore(ScheduleResult.SUCCEEDED, None)
    scheduler._core = core
    outputs: List[Any] = [
        types.SimpleNamespace(path="/out/a.bgeo", tag="file/geo"),
        types.SimpleNamespace(path="", tag="file/geo"),
        types.SimpleNamespace(path="/out/b.exr"),
    ]
    item: Any = types.SimpleNamespace(
        id=7,
        index=2,
        name="sim_7",
        node=types.SimpleNamespace(name="sim"),
        expectedOutputFiles=outputs,
    )
    scheduler.onSchedule(item)
    ((handle,),) = [core.handles]
    assert (handle.item_id, handle.index, handle.name, handle.node_name) == (7, 2, "sim_7", "sim")
    assert [(f.path, f.tag) for f in handle.declared_outputs] == [
        ("/out/a.bgeo", "file/geo"),
        ("/out/b.exr", ""),
    ]


class _CachedItem:
    """A ``pdg.WorkItem`` PDG cooked from cache, with the file it found as its result."""

    def __init__(self, node: str, index: int, path: str) -> None:
        # The fields the graph read takes from a work item.
        self.name: str = f"{node}_{index}"
        self.index: int = index
        self.id: int = 100 + index
        self.node: Any = types.SimpleNamespace(name=node)
        self.command: str = f"hython {node}.py"
        self.state: Any = types.SimpleNamespace(name="CookedCache")
        self.outputFiles: List[Any] = [types.SimpleNamespace(path=path, tag="file/geo")]


def test_a_cook_that_ends_with_every_item_from_cache_shows_why_on_the_node(
    scene: FakeHou, tmp_path: Path
) -> None:
    """With every item cooked from files, PDG offers nothing, so the end of the cook must say why.

    The error names each step in the cook and one of its files, and no job is made.
    """
    # Start a cook of render, whose cook set names render and the step above it, as Houdini's does.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    scheduler: Any = _scheduler(network)
    network.context.cookSet = [network.node("sim"), network.node("render")]
    assert scheduler.onStartCook(False, [network.node("render")]) is True
    # PDG took every item of sim and render from cache, then ends the cook with nothing offered.
    scheduler.dependency_graph = (
        {
            _CachedItem("sim", 0, str(tmp_path / "geo" / "sim.0.bgeo.sc")): [],
            _CachedItem("render", 1, str(tmp_path / "geo" / "render.1.exr")): [],
        },
        {},
        [],
    )
    assert scheduler.onStopCook(False) is True
    (error,) = [call[1] for call in scheduler.calls if call[0] == "cookError"]
    assert "sim:" in error and str(tmp_path / "geo" / "sim.0.bgeo.sc") in error
    assert "render:" in error and str(tmp_path / "geo" / "render.1.exr") in error
    assert "montage:" not in error and "no job was created" in error


# -- the composition root -----------------------------------------------------


def test_the_cook_uses_the_queues_bucket_and_checks_the_role_can_list_it(
    scene: FakeHou, tmp_path: Path
) -> None:
    """Payloads go to the queue's own bucket, and one read of a missing key proves the role can list it."""
    cloud: FakeCloud = FakeCloud()
    _connect(build_network(PYRO_BONFIRE), cloud, tmp_path)
    ((bucket, key),) = cloud.s3.keys
    assert bucket == "bucket-a" and key.startswith("DeadlineCloud/")
    # The queue's display name reaches the session, which names the queue in its errors.
    assert cloud.session_names == ["Render"]


def test_a_queue_role_that_cannot_list_its_bucket_is_refused_before_any_upload(
    scene: FakeHou, tmp_path: Path
) -> None:
    """Without the listing permission a missing output record reads as an error, so the cook can't tell results apart."""
    cloud: FakeCloud = FakeCloud(s3_error=_client_error("AccessDenied"))
    with pytest.raises(CookSetupError) as raised:
        _connect(build_network(PYRO_BONFIRE), cloud, tmp_path)
    assert "s3:ListBucket" in str(raised.value)
    assert "bucket-a" in str(raised.value) and "Render" in str(raised.value)


def test_another_error_from_the_bucket_check_is_logged_and_the_cook_goes_on(
    scene: FakeHou, tmp_path: Path, caplog: Any
) -> None:
    """A throttle or a network blip says nothing about the role, so it must not block the cook."""
    cloud: FakeCloud = FakeCloud(s3_error=_client_error("SlowDown"))
    with caplog.at_level(logging.WARNING):
        _connect(build_network(PYRO_BONFIRE), cloud, tmp_path)
    assert "Could not check access to bucket bucket-a" in caplog.text


def test_an_opted_out_workstation_sends_no_outcome_event(
    scene: FakeHou, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: Any
) -> None:
    """The opt-out lives in the client library's own client, so the cook's event must go through it."""
    from queue import Queue

    from deadline.client import api

    # A fresh library client, built with the opt-out set, as on the artist's workstation.
    monkeypatch.setenv("DEADLINE_CLOUD_TELEMETRY_OPT_OUT", "true")
    previous: Any = _TELEMETRY_CLIENTS.pop("deadline-cloud-library", None)

    def restore() -> None:
        # Put back the library's cached telemetry client, or drop the one this test built.
        _TELEMETRY_CLIENTS.pop("deadline-cloud-library", None)
        if previous is not None:
            _TELEMETRY_CLIENTS["deadline-cloud-library"] = previous

    request.addfinalizer(restore)
    client: Any = api.get_deadline_cloud_library_telemetry_client()
    client.event_queue = Queue()

    class Clients(FakeCloud):
        telemetry_recorder: Any = REGISTRATION._DeadlineCloudCalls.telemetry_recorder

    record: Any = Clients().telemetry_recorder()
    assert record.__self__ is client and record.__func__ is type(client).record_event
    services: CookServices = _connect(build_network(PYRO_BONFIRE), Clients(), tmp_path)
    services.journal.closed(CookSummary(cook_id="cook-1", final_phase=CookPhase.DONE))
    assert client.event_queue.empty()
    # The same client with telemetry on queues the event, so an empty queue means the opt-out held.
    monkeypatch.setattr(client, "telemetry_opted_out", False)
    monkeypatch.setattr(client, "_initialized", True)
    opted_in: CookServices = _connect(build_network(PYRO_BONFIRE), Clients(), tmp_path)
    opted_in.journal.closed(CookSummary(cook_id="cook-1", final_phase=CookPhase.DONE))
    assert not client.event_queue.empty()


@pytest.mark.parametrize(
    "settings, words",
    [({}, "farm ID"), ({"defaults.farm_id": "farm-1"}, "queue ID")],
)
def test_a_missing_farm_or_queue_is_named(
    scene: FakeHou, tmp_path: Path, settings: Dict[str, str], words: str
) -> None:
    """The artist needs to know which workstation setting to fill in."""
    with pytest.raises(CookSetupError, match=words):
        _connect(build_network(PYRO_BONFIRE), FakeCloud(settings=settings), tmp_path)


def test_an_expired_login_is_retried_while_it_renews(scene: FakeHou, tmp_path: Path) -> None:
    """The monitor renews credentials in the background, so a short wait often lets the read succeed."""
    cloud: FakeCloud = FakeCloud(
        queue=[NoCredentialsError(), _client_error("ExpiredToken"), _QUEUE]
    )
    _connect(build_network(PYRO_BONFIRE), cloud, tmp_path)
    assert cloud.sleeps == [2.0, 4.0]


@pytest.mark.parametrize(
    "error, words",
    [
        (NoCredentialsError(), "login is missing or has expired"),
        (_client_error("AccessDeniedException"), "was denied"),
        (_client_error("ResourceNotFoundException"), "was not found"),
        (RuntimeError("boom"), "Cannot read queue queue-1"),
    ],
)
def test_a_failed_queue_read_names_its_cause(
    scene: FakeHou, tmp_path: Path, error: Exception, words: str
) -> None:
    """Each cause has its own remedy, so the message must say which one it is."""
    with pytest.raises(CookSetupError, match=words):
        _connect(build_network(PYRO_BONFIRE), FakeCloud(queue=[error]), tmp_path)


def test_an_error_the_library_says_needs_a_login_asks_for_one(
    scene: FakeHou, tmp_path: Path
) -> None:
    """Some credential providers raise their own errors, so the library's login check decides."""
    cloud: FakeCloud = FakeCloud(queue=[RuntimeError("provider failed")], login_needed=True)
    with pytest.raises(CookSetupError, match="login is missing or has expired"):
        _connect(build_network(PYRO_BONFIRE), cloud, tmp_path)


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: Any) -> None:
    """The real client library with its own config file and a made-up key, so nothing reads this machine."""
    from deadline.client.api._session import invalidate_boto3_session_cache

    # Every file the library or boto3 reads is under tmp_path, and the key works only offline.
    monkeypatch.setenv("DEADLINE_CONFIG_FILE_PATH", str(tmp_path / "deadline" / "config"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws" / "credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDWORKSTATION")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "workstation-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_REGION"):
        monkeypatch.delenv(name, raising=False)
    # The library caches its sessions, so each test starts and ends with none.
    invalidate_boto3_session_cache()
    request.addfinalizer(invalidate_boto3_session_cache)


def test_the_deadline_cloud_calls_reads_a_workstation_setting_and_an_unset_one_as_empty(
    library: None,
) -> None:
    """An unset farm ID must read as empty, or the cook can't tell the artist to set it."""
    from deadline.client import config

    config.set_setting("defaults.farm_id", "farm-1")
    clients: Any = REGISTRATION._DeadlineCloudCalls()
    assert clients.get_setting("defaults.farm_id") == "farm-1"
    assert clients.get_setting("defaults.queue_id") == ""
    # A library that answers None for an unset value reads as empty too.
    clients.config = types.SimpleNamespace(get_setting=lambda name: None)
    assert clients.get_setting("defaults.farm_id") == ""


def test_the_deadline_cloud_calls_builds_a_deadline_client_and_a_queue_session_from_the_library(
    library: None,
) -> None:
    """Without these the cook can't read the queue, and its uploads would use the artist's own role."""
    from botocore.stub import Stubber

    clients: Any = REGISTRATION._DeadlineCloudCalls()
    client: Any = clients.deadline_client()
    assert client.meta.service_model.service_name == "deadline"
    # The session's credentials come from the queue role that this client assumes for this queue.
    expires: datetime = datetime(2099, 1, 1, tzinfo=timezone.utc)
    stub: Stubber = Stubber(client)
    stub.add_response(
        "assume_queue_role_for_user",
        {
            "credentials": {
                "accessKeyId": "AKIDQUEUE",
                "secretAccessKey": "queue-secret",
                "sessionToken": "queue-token",
                "expiration": expires,
            }
        },
        {"farmId": "farm-1", "queueId": "queue-1"},
    )
    with stub:
        session: Any = clients.queue_session(client, "farm-1", "queue-1", "Render")
        assert session.get_credentials().get_frozen_credentials().access_key == "AKIDQUEUE"
    stub.assert_no_pending_responses()


class _AuthCheck:
    """The library's ``api`` as the login check sees it: the real status enum and a fixed answer."""

    def __init__(self, answer: Any) -> None:
        from deadline.client import api

        self.AwsAuthenticationStatus: Any = api.AwsAuthenticationStatus
        self.answer: Any = answer

    def check_authentication_status(self) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _statuses() -> List[Any]:
    """Every status the client library's login check can give."""
    from deadline.client import api

    return list(api.AwsAuthenticationStatus)


@pytest.mark.parametrize("status", _statuses(), ids=lambda status: status.name)
def test_only_a_status_that_needs_a_login_asks_the_artist_to_log_in(status: Any) -> None:
    """Asking for a login on any other status sends an artist with a working login the wrong way."""
    clients: Any = REGISTRATION._DeadlineCloudCalls()
    clients.api = _AuthCheck(status)
    assert clients.needs_login() is (status.name == "NEEDS_LOGIN")


def test_a_login_check_that_fails_does_not_ask_for_a_login() -> None:
    """A failed check shows nothing about the login, so the queue error is reported as it is."""
    clients: Any = REGISTRATION._DeadlineCloudCalls()
    clients.api = _AuthCheck(RuntimeError("no network"))
    assert clients.needs_login() is False


@pytest.mark.parametrize(
    "attachments", [{}, {"s3BucketName": "bucket-a"}, {"rootPrefix": "DeadlineCloud"}]
)
def test_a_queue_without_job_attachments_cannot_cook(
    scene: FakeHou, tmp_path: Path, attachments: Dict[str, str]
) -> None:
    """Every payload is stored under the bucket and its prefix, so a queue missing either is refused by name."""
    queue: Dict[str, Any] = {"displayName": "Render", "jobAttachmentSettings": attachments}
    cloud: FakeCloud = FakeCloud(queue=[queue])
    with pytest.raises(CookSetupError, match="Render has no job attachments bucket"):
        _connect(build_network(PYRO_BONFIRE), cloud, tmp_path)


def test_a_cook_of_only_in_process_nodes_is_refused_at_its_start_before_the_farm(
    scene: FakeHou,
) -> None:
    """Cook Selected Node on a wedge showed no message at all, because PDG offered nothing.

    PDG still starts the cook with the wedges as its cook set, so the start refuses it.
    """
    # Start a cook of the wedge alone, then check the error and that no farm call ran.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    cloud: FakeCloud = FakeCloud()
    scheduler: Any = _scheduler(network, cloud)
    assert scheduler.onStartCook(False, [network.node("wedge1")]) is False
    (error,) = [call[1] for call in scheduler.calls if call[0] == "cookError"]
    assert "wedge1" in error and "No node in this cook runs a command on a worker" in error
    assert cloud.s3.keys == []


class _Deferred:
    """``hdefereval``: records what is queued for the main thread, and runs it only when asked."""

    def __init__(self) -> None:
        self.queued: List[Callable[[], Any]] = []

    def executeDeferred(self, function: Callable[[], Any]) -> None:
        self.queued.append(function)

    def executeInMainThreadWithResult(self, function: Callable[..., Any], *args: Any) -> Any:
        return function(*args)


@pytest.fixture
def ui(scene: FakeHou, monkeypatch: pytest.MonkeyPatch) -> _Deferred:
    """A Houdini session with a UI, whose main-thread queue the test runs by hand."""
    # Turn the UI on and install the queue in place of Houdini's module.
    scene.ui = True
    deferred: _Deferred = _Deferred()
    monkeypatch.setitem(sys.modules, "hdefereval", deferred)
    return deferred


@pytest.mark.parametrize("has_ui", [True, False])
def test_a_refused_cook_shows_a_popup_with_the_node_errors_text_only_with_a_ui(
    scene: FakeHou, monkeypatch: pytest.MonkeyPatch, has_ui: bool
) -> None:
    """The TOP Cook button gave only the error flag, so the artist didn't see the refusal.

    The popup is queued, never shown on PDG's thread, so PDG never waits on a dialog.
    hython has no one to read a dialog, so there only the node error and the log carry it.
    """
    # Refuse a cook, see nothing shown yet, then run the queue and read the popup.
    scene.ui = has_ui
    deferred: _Deferred = _Deferred()
    monkeypatch.setitem(sys.modules, "hdefereval", deferred)
    scheduler: Any = _scheduler(build_network(COOKING_EXAMPLE))
    scheduler.onStartCook(False, None)
    (error,) = [call[1] for call in scheduler.calls if call[0] == "cookError"]
    assert scene.messages.calls == [] and len(deferred.queued) == int(has_ui)
    if not has_ui:
        return
    deferred.queued[0]()
    ((text, buttons),) = scene.messages.calls
    assert text == error and buttons == ()
    assert scene.messages.options[0]["severity"] == "Error"


def test_each_cook_shows_one_popup_and_the_next_cook_shows_its_own(
    scene: FakeHou, ui: _Deferred
) -> None:
    """A dialog per offered item would bury the artist, but a later cook's refusal must still show."""
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    scheduler: Any = _scheduler(network)
    assert scheduler.onStartCook(False, [network.node("sim")]) is True
    # An offer PDG can't describe fails, twice, in the same cook.
    scheduler.onSchedule(object())
    scheduler.onSchedule(object())
    assert len([call for call in scheduler.calls if call[0] == "cookError"]) == 2
    assert len(ui.queued) == 1
    # The next cook is a new cook, so its refusal shows again.
    scheduler.onStopCook(True)
    network.node("render").isDynamic = True
    scheduler.onStartCook(False, None)
    assert len(ui.queued) == 2


def test_submits_stop_logs_under_the_package_logger(
    scene: FakeHou, caplog: pytest.LogCaptureFixture
) -> None:
    """PDG loads the module under its own name, so the package's log level hid these lines.

    One setLevel on the package's logger must show both lines, in this order.
    """
    # Submit stored a stop, and onStartCook takes it and refuses the cook start.
    caplog.set_level(logging.INFO, logger="deadline.houdini_pdg")
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    scheduler: Any = _scheduler(network)
    path: str = network.scheduler_node.path()
    host_module.submit_stops[path] = ("The cook can't start.", "")
    assert scheduler.onStartCook(False, None) is False
    lines: List[str] = [
        r.getMessage() for r in caplog.records if r.name.startswith("deadline.houdini_pdg.")
    ]
    assert lines == [
        f"Took Submit's stop for {path} in onStartCook",
        f"Refused the cook start to show Submit's stop on {path}'s tabs",
    ]


def test_on_start_cook_takes_submits_stop_sends_it_to_both_tabs_and_refuses(
    scene: FakeHou, ui: _Deferred
) -> None:
    """The stop reaches the node only through this refused start, as PDG keeps a message sent in a cook.

    Submit already showed the popup, so this start shows none and contacts no farm.
    """
    # The cook would start without the stop, so the refusal comes from the stop alone.
    network: FakeNetwork = build_network(PYRO_BONFIRE)
    cloud: FakeCloud = FakeCloud()
    scheduler: Any = _scheduler(network, cloud)
    path: str = network.scheduler_node.path()
    host_module.submit_stops[path] = ("The cook can't start.", "render: slow")
    assert scheduler.onStartCook(False, [network.node("sim")]) is False
    assert scheduler.calls == [
        ("cookError", "The cook can't start."),
        ("cookWarning", "render: slow"),
    ]
    # The stop is used once, and nothing else was queued or called.
    assert host_module.submit_stops == {} and ui.queued == [] and cloud.s3.keys == []
