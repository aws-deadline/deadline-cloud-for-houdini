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
from typing import Any, Optional

import pdg  # type: ignore[import-not-found]
from pdg.scheduler import PyScheduler  # type: ignore[import-not-found]

from deadline.houdini_pdg.errors import artist_message
from deadline.houdini_pdg.cook import CookServices, PdgScheduler, ScheduleResult, SystemClock
from deadline.houdini_pdg.houdini.host import HoudiniPdgHost
from deadline.houdini_pdg.houdini.settings import SchedulerSettings
from deadline.houdini_pdg.ports import WorkItemHandle

_logger = logging.getLogger(__name__)

_SCHEDULE_RESULTS = {
    ScheduleResult.SUCCEEDED: pdg.scheduleResult.Succeeded,
    ScheduleResult.DEFERRED: pdg.scheduleResult.Deferred,
}


class DeadlineCloudPDGScheduler(PyScheduler):
    """Cooks a TOP network as one Deadline Cloud job."""

    @classmethod
    def templateName(cls) -> str:
        """The scheduler type name. The HDA's ``subtype`` and node type name must match it."""
        return "deadlinecloudscheduler"

    @classmethod
    def templateBody(cls) -> str:
        """The node's parm template."""
        return json.dumps({"name": cls.templateName(), "parameters": []})

    def __init__(self, scheduler: Any, name: str) -> None:
        super().__init__(scheduler, name)
        # Built per cook, so a node can be created and its help read even when a
        # cook cannot start.
        self._core: Optional[PdgScheduler] = None
        self._start_failed = False
        self._scratch_dir: Optional[str] = None

    def onStartCook(self, static: bool, cook_set: Any) -> bool:
        """Start a cook.

        A failure still returns True: PDG discards a scheduler's error when this
        returns False, so the error is reported here and the cook is canceled on
        the first tick instead.
        """
        self._start_failed = False
        try:
            self._core = PdgScheduler(
                host=HoudiniPdgHost(self),
                clock=SystemClock(),
                read_settings=self._read_settings,
                connect=self._connect,
            )
            return self._core.on_start_cook(static, cook_set)
        except Exception as error:
            self._core = None
            self._start_failed = True
            self._fail("onStartCook", error)
            self._use_scratch_working_dir()
            return True

    def onSchedule(self, work_item: Any) -> Any:
        """Hand a work item to the cook. Anything but success or deferral fails it."""
        try:
            if self._start_failed or self._core is None:
                return pdg.scheduleResult.CookFailed
            result = self._core.on_schedule(self._handle(work_item))
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
        core, self._core = self._core, None
        try:
            return True if core is None else core.on_stop_cook(cancel)
        except Exception as error:
            self._fail("onStopCook", error)
            return True
        finally:
            self._remove_scratch_working_dir()

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
        if self._scratch_dir:
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            self._scratch_dir = None

    def _fail(self, callback: str, error: Exception) -> None:
        _logger.error("%s failed", callback, exc_info=error)
        try:
            self.cookError(artist_message(error))
        except Exception:
            _logger.exception("Could not show the error on the scheduler node")

    def _read_settings(self) -> SchedulerSettings:
        raise NotImplementedError

    def _connect(self, settings: SchedulerSettings) -> CookServices:
        raise NotImplementedError

    def _handle(self, work_item: Any) -> WorkItemHandle:
        raise NotImplementedError


def registerTypes(type_registry: Any) -> None:
    """Register the scheduler type with PDG."""
    type_registry.registerScheduler(
        DeadlineCloudPDGScheduler,
        name=DeadlineCloudPDGScheduler.templateName(),
        label="Deadline Cloud Scheduler (Experimental)",
        parm_category="DeadlineCloud",
    )
