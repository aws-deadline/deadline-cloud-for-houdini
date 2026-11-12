# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Registers the Deadline Cloud scheduler with PDG. Experimental.

PDG loads this module from ``$HOUDINI_PATH/pdg/types/deadlinecloud/``. The
installer does not put it there; the PDG README explains how to add it by hand.

The node's icon, parameters, help, and Tab menu entry come from the HDA in
``otls/deadline_cloud_scheduler.hda``, which binds to this type by name, the same
way Houdini's own schedulers pair a Python class with an HDA.

This module builds the scheduler's objects and forwards PDG's callbacks to them.
No exception reaches PDG. A failure is shown on the scheduler node as a cook
error, in words an artist can act on, and its traceback goes to the log.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
import uuid
from typing import Any, Callable, Dict, FrozenSet, Optional, Set, Tuple

import pdg  # type: ignore[import-not-found]
from deadline.client import api as deadline_api
from deadline.client import config as deadline_config
from pdg.scheduler import PyScheduler  # type: ignore[import-not-found]

from deadline.houdini_pdg.cook import CookServices, PdgScheduler, ScheduleResult, SystemClock
from deadline.houdini_pdg.diagnostics import CookJournal
from deadline.houdini_pdg.errors import (
    CookSetupError,
    GraphRefusedError,
    artist_message,
    format_advisories,
)
from deadline.houdini_pdg.farm_job import DeadlineFarmJob
from deadline.houdini_pdg.houdini.host import (
    HoudiniPdgHost,
    cook_set_names,
    run_pre_cook_checks,
    show_error_popup,
    submit_stops,
)
from deadline.houdini_pdg.houdini.readers import declared_files
from deadline.houdini_pdg.houdini.settings import SchedulerSettings
from deadline.houdini_pdg.ports import Clock, WorkItemHandle
from deadline.houdini_pdg.submitter import PdgSubmitter, farm_problem, get_workstation_setting
from deadline.houdini_pdg.task_storage import S3TaskStorage
from deadline.houdini_pdg.validation import Verdict

# PDG imports this module as pdg.types.deadlinecloud, so a name from __name__ would sit outside
# the package's logger, and setting that logger's level wouldn't reach these lines.
_logger: logging.Logger = logging.getLogger("deadline.houdini_pdg.registration")

_SCHEDULE_RESULTS: Dict[ScheduleResult, Any] = {
    ScheduleResult.SUCCEEDED: pdg.scheduleResult.Succeeded,
    ScheduleResult.DEFERRED: pdg.scheduleResult.Deferred,
}


class _DeadlineCloudCalls:
    """Builds the client library's clients and sessions for a cook, and reads its settings.

    A test replaces these calls by subclassing, or replaces ``api`` with a stand-in.
    """

    #: The client library's ``api`` and ``config`` modules, which every call here goes through.
    api: Any = deadline_api
    config: Any = deadline_config

    def get_setting(self, name: str) -> str:
        """A workstation setting, such as ``defaults.farm_id``, or ``""`` when unset."""
        return get_workstation_setting(name, self.config)

    def deadline_client(self) -> Any:
        """A Deadline Cloud client with the workstation's login."""
        return self.api.get_boto3_client("deadline")

    def queue_session(self, client: Any, farm_id: str, queue_id: str, display_name: str) -> Any:
        """A boto3 session with the queue role's credentials."""
        return self.api.get_queue_user_boto3_session(
            deadline=client, farm_id=farm_id, queue_id=queue_id, queue_display_name=display_name
        )

    def needs_login(self) -> bool:
        """Whether the client library says the workstation must log in again."""
        # A failed check can't show that a login would help, so it asks for none.
        try:
            status: Any = self.api.check_authentication_status()
            return bool(status == self.api.AwsAuthenticationStatus.NEEDS_LOGIN)
        except Exception:
            return False

    def telemetry_recorder(self) -> Callable[[str, Dict[str, Any]], Any]:
        """The telemetry client's ``record_event``, which honors the workstation's opt-out."""
        try:
            record: Callable[[str, Dict[str, Any]], Any] = (
                self.api.get_deadline_cloud_library_telemetry_client().record_event
            )
            return record
        except Exception:
            _logger.debug("Telemetry is unavailable for this cook", exc_info=True)
            return lambda event_type, details: None

    def sleep(self, seconds: float) -> None:
        """Wait before a retry."""
        time.sleep(seconds)


class DeadlineCloudPDGScheduler(PyScheduler):
    """Cooks a TOP network as one Deadline Cloud job."""

    @classmethod
    def templateName(cls) -> str:
        """The scheduler type name. The HDA's ``subtype`` and node type name must match it."""
        return "deadlinecloudscheduler"

    @classmethod
    def templateBody(cls) -> str:
        """The node's parm template. The HDA declares the parms."""
        return json.dumps({"name": cls.templateName(), "parameters": []})

    def __init__(self, scheduler: Any, name: str) -> None:
        super().__init__(scheduler, name)
        # Built per cook, so a node can be created and its help read even when a
        # cook cannot start.
        self._core: Optional[PdgScheduler] = None
        self._start_failed: bool = False
        self._scratch_dir: Optional[str] = None
        # Per cook: PDG calls onStartCook, onSchedule and onStopCook separately, so no caller
        # can pass it. onStartCook resets it, like _start_failed.
        self._popup_shown: bool = False

    def onStartCook(self, static: bool, cook_set: Any) -> bool:
        """Start a cook.

        A failure reports its error here. A graph refusal returns False, because PDG
        starts a Service node's processes after a True return. Any other failure
        returns True, and the cook is canceled on the first tick.
        """
        self._start_failed = False
        self._popup_shown = False
        try:
            # One host per cook, so the scene files are scanned once per cook.
            host: HoudiniPdgHost = HoudiniPdgHost(self)
            # A stopped Submit starts this cook only to put its stop on the node's tabs, because
            # PDG keeps a message sent inside a cook. Submit shows the popup itself.
            path: str = host.scheduler_node().path()
            stop: Optional[Tuple[str, str]] = submit_stops.pop(path, None)
            if stop is not None:
                # Send the stop to the node's tabs and refuse the cook. Submit showed the popup.
                _logger.info("Took Submit's stop for %s in onStartCook", path)
                error, advisories = stop
                if error:
                    self.cookError(error)
                if advisories:
                    host.show_warning(advisories)
                _logger.info("Refused the cook start to show Submit's stop on %s's tabs", path)
                return False
            # Houdini starts a generate with static False, and says so in the cook options.
            generate: bool = static or _is_generate(self.context)
            names: Optional[FrozenSet[str]] = cook_set_names(cook_set)
            if not generate:
                # A generate submits nothing, so only a cook is checked.
                _refuse(run_pre_cook_checks(host.scheduler_node(), cook_set=names)[0])
            clock: SystemClock = SystemClock()
            clients: _DeadlineCloudCalls = self._clients()
            self._core = PdgScheduler(
                host=host,
                clock=clock,
                read_settings=host.read_settings,
                connect=lambda settings, cook_id: _connect(
                    settings, cook_id, host=host, clock=clock, clients=clients
                ),
            )
            return self._core.on_start_cook(generate, names)
        except Exception as error:
            self._core = None
            self._start_failed = True
            self._fail("onStartCook", error)
            # A refused graph returns False, so PDG starts nothing, such as a Service node's
            # processes. The node keeps the error made above, and no working directory is needed.
            if isinstance(error, GraphRefusedError):
                # The verdict's advisories go on the Warning tab, next to the refusals.
                if error.verdict.advisories:
                    host.show_warning(format_advisories(error.verdict.advisories))
                return False
            self._use_scratch_working_dir()
            return True

    def onSchedule(self, work_item: Any) -> Any:
        """Hand a work item to the cook. Anything but success or deferral fails it."""
        try:
            if self._start_failed or self._core is None:
                return pdg.scheduleResult.CookFailed
            result: ScheduleResult = self._core.on_schedule(_to_work_item_handle(work_item))
        except Exception as error:
            self._fail("onSchedule", error)
            return pdg.scheduleResult.CookFailed
        return _SCHEDULE_RESULTS.get(result, pdg.scheduleResult.CookFailed)

    def onTick(self) -> Any:
        """Advance the cook, or cancel it when it could not start."""
        if self._start_failed:
            return pdg.tickResult.SchedulerCancelCook
        try:
            if self._core is not None:
                self._core.on_tick()
        except Exception as error:
            self._fail("onTick", error)
        return pdg.tickResult.SchedulerReady

    def onStopCook(self, cancel: bool) -> bool:
        """Close the cook."""
        core: Optional[PdgScheduler] = self._core
        self._core = None
        try:
            return True if core is None else core.on_stop_cook(cancel)
        except Exception as error:
            self._fail("onStopCook", error)
            return True
        finally:
            self._remove_scratch_working_dir()

    def _clients(self) -> _DeadlineCloudCalls:
        """The Deadline Cloud calls this scheduler's cooks use."""
        return _DeadlineCloudCalls()

    def _use_scratch_working_dir(self) -> None:
        # PDG copies its job scripts into the working directory before it
        # schedules anything. A failed start never sets one, so PDG would copy
        # them to the filesystem root and warn once per script.
        self._remove_scratch_working_dir()
        try:
            self._scratch_dir = tempfile.mkdtemp(prefix="deadline_pdg_")
            self.setWorkingDir(self._scratch_dir, self._scratch_dir)
        except Exception:
            _logger.exception("Could not set a working directory for the failed cook")

    def _remove_scratch_working_dir(self) -> None:
        # Delete the failed start's temporary working directory, if there is one.
        if self._scratch_dir:
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            self._scratch_dir = None

    def _fail(self, callback: str, error: Exception) -> None:
        # Log the traceback, and show the artist the error on the node, and once
        # per cook in a popup.
        _logger.error("%s failed", callback, exc_info=error)
        message: str = artist_message(error)
        try:
            self.cookError(message)
        except Exception:
            _logger.exception("Could not show the error on the scheduler node")
        if self._popup_shown:
            return
        self._popup_shown = True
        try:
            show_error_popup(message)
        except Exception:
            _logger.exception("Could not show the error in a popup")


#: The ``pdg.cookType`` members for a cook that only generates work items.
_GENERATE_COOK_TYPES: FrozenSet[str] = frozenset({"GenerateFull", "GenerateNode", "GenerateStatic"})


def _is_generate(context: Any) -> bool:
    """Whether PDG's cook options say this cook only generates work items."""
    try:
        cook_type: Any = context.cookOptions.cookType
    except Exception:
        return False
    # PDG's enum prints as ``cookType.GenerateNode``, and its ``name`` is the member alone.
    # StaticDepsGen has GenerateFull's value and Houdini names it GenerateFull, so it reads as one.
    name: str = str(getattr(cook_type, "name", "") or cook_type)
    return name.rsplit(".", 1)[-1] in _GENERATE_COOK_TYPES


def _refuse(verdict: Verdict) -> None:
    """Log each refusal, then raise ``GraphRefusedError`` when there is any."""
    for refusal in verdict.refusals:
        _logger.error(
            "Refused, %s (%s): %s %s",
            refusal.subject,
            refusal.scenario.value,
            refusal.detail,
            refusal.remedy,
        )
    if verdict.refusals:
        raise GraphRefusedError(verdict)


def _connect(
    settings: SchedulerSettings,
    cook_id: str,
    *,
    host: HoudiniPdgHost,
    clock: Clock,
    clients: _DeadlineCloudCalls,
) -> CookServices:
    """The cook's farm services, for the workstation's farm and queue, from one ``GetQueue``."""
    problem: str = farm_problem(clients.get_setting)
    if problem:
        raise CookSetupError(problem)
    farm_id: str = clients.get_setting("defaults.farm_id")
    queue_id: str = clients.get_setting("defaults.queue_id")
    # The queue gives the bucket, and its role gives the session that reaches it.
    client, queue = _read_queue(farm_id, queue_id, clients)
    bucket, root_prefix = _read_job_attachments_bucket(queue, queue_id)
    s3_client: Any = _open_queue_s3_client(client, queue, farm_id, queue_id, clients)
    _check_bucket_listing(s3_client, bucket, root_prefix, queue.get("displayName") or queue_id)
    return CookServices(
        submitter=PdgSubmitter(
            node=host.scheduler_node(),
            host=host,
            working_dir=settings.working_dir,
            output_directories=settings.output_directories,
            log=logging.getLogger("deadline.houdini_pdg.submitter").info,
            # The queue was read once, here, so the submitter does not read it again.
            job_attachments=lambda _farm, _queue: (bucket, root_prefix),
            cook_id=cook_id,
            get_setting=clients.get_setting,
        ),
        storage=S3TaskStorage(s3_client=s3_client, bucket=bucket, root_prefix=root_prefix),
        open_job=lambda job_id, snapshot: DeadlineFarmJob(
            deadline_client=client,
            farm_id=farm_id,
            queue_id=queue_id,
            job_id=job_id,
            snapshot=snapshot,
            clock=clock,
        ),
        journal=CookJournal(
            working_dir=settings.working_dir,
            cook_id=cook_id,
            started_at=clock.now(),
            debug_level=settings.debug_level,
            record_event=clients.telemetry_recorder(),
        ),
    )


def _open_queue_s3_client(
    client: Any, queue: Any, farm_id: str, queue_id: str, clients: _DeadlineCloudCalls
) -> Any:
    """An S3 client with the queue role's credentials. Raises ``CookSetupError`` naming the cause."""
    try:
        session: Any = clients.queue_session(
            client, farm_id, queue_id, str(queue.get("displayName") or "")
        )
        return session.client("s3")
    except Exception as e:
        raise _queue_error(e, farm_id, queue_id, clients) from e


def _check_bucket_listing(s3_client: Any, bucket: str, root_prefix: str, queue: str) -> None:
    """Refuse a queue role that cannot list its bucket, by reading a key that cannot exist.

    Without ``s3:ListBucket``, S3 answers ``AccessDenied`` for a missing object,
    so a task with no output record could not be told from one the role can't read.
    """
    key: str = f"{root_prefix.strip('/')}/PdgAccessCheck/{uuid.uuid4().hex}"
    try:
        s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as e:
        # A missing key is the expected answer. Only a denial proves the permission is missing.
        code: str = _error_code(e)
        if code in ("NoSuchKey", "404", "NotFound"):
            return
        if code == "AccessDenied" or type(e).__name__ == "AccessDenied":
            raise CookSetupError(
                f"The role of queue {queue} cannot list its job attachments bucket {bucket}. "
                "Add s3:ListBucket on the bucket to the queue role, then cook again."
            ) from e
        _logger.warning("Could not check access to bucket %s: %s", bucket, e)
        return
    _logger.warning("The access check found an object at %s in bucket %s.", key, bucket)


def _to_work_item_handle(work_item: Any) -> WorkItemHandle:
    """The cook's view of a PDG work item."""
    node: Any = getattr(work_item, "node", None)
    return WorkItemHandle(
        item_id=int(work_item.id),
        index=int(work_item.index),
        name=str(work_item.name),
        node_name=str(getattr(node, "name", "") or ""),
        # The item's expected outputs now, because PDG can change them after generation.
        declared_outputs=declared_files(getattr(work_item, "expectedOutputFiles", None)),
        native=work_item,
    )


def _read_job_attachments_bucket(queue: Any, queue_id: str) -> Tuple[str, str]:
    """The queue's job attachments bucket and root prefix. Raises ``CookSetupError`` without them."""
    # Both the bucket and the root prefix are needed to name a task's keys.
    settings: Dict[str, str] = queue.get("jobAttachmentSettings") or {}
    bucket: str = settings.get("s3BucketName", "")
    root_prefix: str = settings.get("rootPrefix", "")
    if not bucket or not root_prefix:
        raise CookSetupError(
            f"Queue {queue.get('displayName') or queue_id} has no job attachments bucket. "
            "The scheduler stores each work item's data there, so choose a queue that has one."
        )
    return bucket, root_prefix


#: Errors and codes that mean the workstation's login is missing or expired.
_LOGIN_ERRORS: Tuple[str, ...] = (
    "NoCredentialsError",
    "TokenRetrievalError",
    "UnauthorizedSSOTokenError",
    "SSOTokenLoadError",
    "CredentialRetrievalError",
    "ExpiredToken",
    "ExpiredTokenException",
    "UnrecognizedClientException",
    "InvalidClientTokenId",
)

#: Seconds to wait before each retry of a queue read that failed for login. Deadline
#: Cloud Monitor renews its credentials in the background, sometimes only after they lapse.
_LOGIN_RETRY_DELAYS: Tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)


def _read_queue(farm_id: str, queue_id: str, clients: _DeadlineCloudCalls) -> Tuple[Any, Any]:
    """A Deadline Cloud client and the queue it reads. Raises ``CookSetupError`` naming the cause.

    The read is retried while the login is expired, so a renewal in progress can finish.
    """
    try:
        client: Any = clients.deadline_client()
        for delay in _LOGIN_RETRY_DELAYS:
            # Only a login error is worth waiting for. Any other error is raised at once.
            try:
                return client, client.get_queue(farmId=farm_id, queueId=queue_id)
            except Exception as e:
                if not {type(e).__name__, _error_code(e)} & set(_LOGIN_ERRORS):
                    raise
                _logger.info("The login was refused (%s); retrying in %s s", e, delay)
                clients.sleep(delay)
        return client, client.get_queue(farmId=farm_id, queueId=queue_id)
    except Exception as e:
        raise _queue_error(e, farm_id, queue_id, clients) from e


def _error_code(error: Exception) -> str:
    """The service error code botocore attaches to ``error``, or ``""``."""
    response: Any = getattr(error, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _queue_error(
    error: Exception, farm_id: str, queue_id: str, clients: _DeadlineCloudCalls
) -> CookSetupError:
    """The setup error for a failed queue read, with the remedy for its cause."""
    # Match the error by its class name and its service code, from the most specific cause.
    names: Set[str] = {type(error).__name__, _error_code(error)}
    login: CookSetupError = CookSetupError(
        "Your AWS Deadline Cloud login is missing or has expired. Log in with the node's "
        f"Login button or Deadline Cloud Monitor, then cook again. Detail: {error}"
    )
    if names & set(_LOGIN_ERRORS):
        return login
    if any("AccessDenied" in name for name in names):
        return CookSetupError(
            f"Access to queue {queue_id} on farm {farm_id} was denied. Check that your AWS "
            f"identity may use this queue. Detail: {error}"
        )
    if "ResourceNotFound" in _error_code(error):
        return CookSetupError(
            f"Queue {queue_id} on farm {farm_id} was not found. Choose a farm and queue in the "
            "node's Deadline Cloud settings."
        )
    # Some credential providers raise their own errors, so ask the library.
    if clients.needs_login():
        return login
    return CookSetupError(f"Cannot read queue {queue_id} on farm {farm_id}: {error}")


def registerTypes(type_registry: Any) -> None:
    """Register the scheduler type with PDG."""
    type_registry.registerScheduler(
        DeadlineCloudPDGScheduler,
        name=DeadlineCloudPDGScheduler.templateName(),
        label="Deadline Cloud Scheduler (Experimental)",
        parm_category="DeadlineCloud",
    )
