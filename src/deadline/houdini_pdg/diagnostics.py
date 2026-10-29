# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""What a cook leaves behind: a debug directory, a log, and one telemetry event.

A cook is long and spread across Houdini, Deadline Cloud, and S3, so its record
is what explains a failure afterwards.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .graph import GraphSnapshot, NodeInfo, TaskKey
from .ports import CookPhase, CookSummary, WorkItemHandle
from .scene_reads import ReadReport

_logger: logging.Logger = logging.getLogger(__name__)

# A sibling of PDG's ``pdgtemp``, which PDG is free to clean.
_DEBUG_DIR_NAME: str = "pdg_debug"
_ORCHESTRATOR_LOG: str = "orchestrator.log"
_COOK_JSON: str = "cook.json"
_BUNDLE_DIR: str = "bundle"

#: Named like the other Deadline Cloud client events.
_OUTCOME_EVENT: str = "com.amazon.rum.deadline.houdini_pdg.cook_outcome"

# A cook's failures usually share one cause, and each tail costs API calls.
_FAILURE_TAIL_LIMIT: int = 3
# Lines held while no directory exists. A pass that never writes keeps its lines
# on the console only, so this bounds the memory it can hold.
_BUFFER_LIMIT: int = 10_000
# Suffixes tried when two cooks share a start second and a cook ID prefix.
_MAX_NAME_SUFFIX: int = 1_000


class _Formatter(logging.Formatter):
    """Formats a line as ``[<ISO-8601 UTC>] message``, whatever the workstation's time zone."""

    def __init__(self) -> None:
        super().__init__("[%(asctime)s] %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        """The record's time in UTC, so lines from any workstation compare directly."""
        return datetime.fromtimestamp(record.created, timezone.utc).isoformat()


class _AppendTarget(logging.Handler):
    """Appends one line per record, opening the file each time so no handle outlives the cook."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path: str = path
        self.setFormatter(_Formatter())

    def emit(self, record: logging.LogRecord) -> None:
        """Append the record, and drop it if the file cannot be written: logging never fails the cook."""
        try:
            line: str = self.format(record)
            # A file name that is not valid UTF-8 decodes to lone surrogates; escape
            # them so the line is still written.
            with open(self.path, "a", encoding="utf-8", errors="backslashreplace") as f:
                f.write(line + "\n")
        except Exception:
            # A logging error must never reach the code that logged the line.
            pass

    def handleError(self, record: logging.LogRecord) -> None:
        """Ignore the error: a log file that cannot be written must not print a traceback per line."""


class _CookLogHandler(logging.handlers.MemoryHandler):
    """Buffers records until the cook's directory exists, then writes each one as it arrives."""

    def __init__(self) -> None:
        super().__init__(capacity=_BUFFER_LIMIT, target=None)
        self.dropped: int = 0

    def shouldFlush(self, record: logging.LogRecord) -> bool:
        """Flush every record once a log file exists; until then, keep only the newest lines."""
        # Without a log file, drop the oldest lines beyond the limit, and count them
        # for the log's first line.
        if self.target is None:
            overflow: int = len(self.buffer) - self.capacity
            if overflow > 0:
                del self.buffer[:overflow]
                self.dropped += overflow
        return self.target is not None

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer or write the record, and never raise into the code that logged it."""
        try:
            super().emit(record)
        except Exception:
            # A logging error must never reach the code that logged the line.
            pass

    def handleError(self, record: logging.LogRecord) -> None:
        """Ignore the error, for the same reason as the file target."""

    def attach(self, path: str) -> None:
        """Start writing to ``path``, first flushing every buffered line in order."""
        target: _AppendTarget = _AppendTarget(path)
        self.acquire()
        try:
            # The dropped count goes first, so a reader knows the log's start is missing.
            if self.dropped:
                note: logging.LogRecord = logging.makeLogRecord(
                    {
                        "name": __name__,
                        "levelno": logging.WARNING,
                        "levelname": logging.getLevelName(logging.WARNING),
                        "msg": f"{self.dropped} earlier line(s) were dropped before this log existed.",
                    }
                )
                target.handle(note)
            self.setTarget(target)
            self.flush()
        finally:
            self.release()


class CookJournal:
    """Records one cook under ``<working_dir>/pdg_debug/<UTC start>-<cook ID prefix>/``.

    The directory is created on the first write, so a pass that PDG starts and
    abandons before offering work leaves nothing behind. No method raises: a
    diagnostics failure must not fail the cook.

    ``record_event`` sends telemetry, for example
    ``get_deadline_cloud_library_telemetry_client().record_event``.
    """

    def __init__(
        self,
        *,
        working_dir: str,
        cook_id: str,
        started_at: datetime,
        debug_level: int,
        record_event: Callable[[str, Dict[str, Any]], Any],
    ) -> None:
        self._working_dir: str = working_dir
        self._cook_id: str = cook_id
        # A naive time is read as UTC, so the directory name never depends on the workstation.
        aware: datetime = (
            started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=timezone.utc)
        )
        self._started_at: datetime = aware.astimezone(timezone.utc)
        # A debug level that is not a number reads as 0, the always-on set.
        try:
            self._debug_level: int = int(debug_level)
        except (TypeError, ValueError):
            self._debug_level = 0
        self._record_event: Callable[[str, Dict[str, Any]], Any] = record_event

        # The directory, the log handler, and the per-cook counters, shared across
        # PDG's callback threads under one lock.
        self._lock: threading.RLock = threading.RLock()
        self._handler: _CookLogHandler = _CookLogHandler()
        self._dir: Optional[str] = None
        self._dir_warned: bool = False
        self._tails_fetched: int = 0
        self._closed: bool = False

    def log_handler(self) -> logging.Handler:
        """A handler that holds lines in memory until the directory exists, then writes ``orchestrator.log``."""
        return self._handler

    def bundle_dir(self) -> str:
        """Where the job bundle is written, so the debug directory holds the bundle as submitted."""
        try:
            directory: Optional[str] = self._ensure_dir()
            if directory is not None:
                path: str = os.path.join(directory, _BUNDLE_DIR)
                os.makedirs(path, exist_ok=True)
                return path
        except Exception:
            _logger.debug("Could not create the bundle directory", exc_info=True)
        # Without a debug directory the bundle still needs a home, or the
        # diagnostics would fail the submission.
        try:
            return tempfile.mkdtemp(prefix="pdg_bundle_")
        except Exception:
            return os.path.join(tempfile.gettempdir(), f"pdg_bundle_{self._cook_id}")

    def snapshot_taken(
        self, nodes: Sequence[NodeInfo], snapshot: GraphSnapshot, reads: Optional[ReadReport]
    ) -> None:
        """Write ``snapshot.json`` and ``scene_reads.json``, and ``graph.json`` at debug level 1 or above."""
        raise NotImplementedError

    def job_submitted(self, job_id: str) -> None:
        """Record the job ID in ``cook.json``."""
        try:
            if self._ensure_dir() is not None:
                self._merge_cook_json({"job_id": job_id})
        except Exception:
            _logger.debug("Could not record the job ID", exc_info=True)

    def item_scheduled(self, item: WorkItemHandle) -> None:
        """At debug level 1 or above, describe the first few work items of each node."""
        raise NotImplementedError

    def item_failed(self, key: TaskKey, log_tail: Callable[[], List[str]]) -> None:
        """Log the worker's last lines for the first few failures. ``log_tail`` is called only within that limit."""
        try:
            # Count this failure against the per-cook limit before any API call.
            with self._lock:
                if self._tails_fetched >= _FAILURE_TAIL_LIMIT:
                    return
                self._tails_fetched += 1
                fetched: int = self._tails_fetched
            task: str = f"{key.step_name}[{key.task_index}]"

            # None means the read failed, and an empty list means there was nothing
            # to read; each gets its own warning.
            lines: Optional[List[str]] = _read_tail(log_tail, task)
            if lines is not None and not lines:
                _logger.warning(
                    "No worker log is available for task %s. The queue role may lack "
                    "logs:GetLogEvents, or the session log may have expired.",
                    task,
                )
            elif lines:
                _logger.warning("Last %d line(s) of the worker log for task %s:", len(lines), task)
                for line in lines:
                    _logger.warning("    | %s", line)

            # Say once, at the last allowed failure, that later ones get no tail.
            if fetched == _FAILURE_TAIL_LIMIT:
                _logger.warning(
                    "Worker logs for further failures are not fetched: %d is the per-cook limit.",
                    _FAILURE_TAIL_LIMIT,
                )
        except Exception:
            _logger.debug("Could not log the worker's last lines", exc_info=True)

    def closed(self, summary: CookSummary) -> None:
        """Write the final counts, and send one outcome event that carries counts only: no paths or names."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        try:
            abandoned: bool = _is_abandoned(summary)
        except Exception:
            # Nothing can be recorded for a summary the journal cannot read.
            _logger.debug("Could not read the cook summary", exc_info=True)
            return
        try:
            outcome: str = _outcome_of(summary)
            counts: Dict[str, Any] = _counts_of(summary)
            # A failed setup has no other artifact, so its log is worth a directory.
            # An abandoned pass gets none.
            if self._dir is not None or summary.final_phase is CookPhase.FAILED:
                if self._ensure_dir() is not None:
                    self._merge_cook_json(
                        {
                            "final_phase": summary.final_phase.value,
                            "outcome": "abandoned" if abandoned else outcome,
                            "job_id": summary.job_id,
                            "refusals": [r.value for r in summary.refusals],
                            **counts,
                        }
                    )
                    _logger.info("Debug artifacts for this cook: %s", self._dir)
        except Exception:
            _logger.debug("Could not write the final counts", exc_info=True)

        # PDG's preliminary pass is not a cook the artist ran, so it is not counted.
        if abandoned:
            return
        try:
            self._record_event(_OUTCOME_EVENT, _outcome_event(summary))
        except Exception:
            _logger.debug("Could not record the cook outcome event", exc_info=True)

    # -- the directory and its files --

    def _ensure_dir(self) -> Optional[str]:
        """The cook's directory, created on the first call. None when it cannot be created."""
        with self._lock:
            if self._dir is not None:
                return self._dir
            # Warn once: every later write fails the same way.
            try:
                path: str = self._create_dir()
            except Exception as e:
                if not self._dir_warned:
                    self._dir_warned = True
                    _logger.warning(
                        "Could not create this cook's debug directory, so its log stays on "
                        "the console only: %s",
                        e,
                    )
                return None

            # The log and cook.json start together, so the log's first lines are
            # the ones buffered before the directory existed.
            self._dir = path
            self._handler.attach(os.path.join(path, _ORCHESTRATOR_LOG))
            self._merge_cook_json(
                {
                    "cook_id": self._cook_id,
                    "started_at": self._started_at.isoformat(),
                    "debug_level": self._debug_level,
                }
            )
            _logger.info("Debug artifacts: %s", path)
            return path

    def _create_dir(self) -> str:
        """Make a fresh directory named by the UTC start and the cook ID prefix, never reusing one."""
        if not self._working_dir:
            raise OSError("no working directory is set")
        root: str = os.path.join(self._working_dir, _DEBUG_DIR_NAME)
        os.makedirs(root, exist_ok=True)
        # ``mkdir`` fails on an existing name, so two cooks that start in the same
        # second with the same prefix each get their own directory.
        base: str = f"{self._started_at.strftime('%Y-%m-%d-%H-%M-%S')}-{self._cook_id[:8]}"
        for n in range(1, _MAX_NAME_SUFFIX + 1):
            path: str = os.path.join(root, base if n == 1 else f"{base}-{n}")
            try:
                os.mkdir(path)
                return path
            except FileExistsError:
                continue
        raise OSError(f"every name for {base} is taken")

    def _merge_cook_json(self, updates: Mapping[str, Any]) -> None:
        """Merge ``updates`` into ``cook.json``, once the directory exists."""
        with self._lock:
            if self._dir is None:
                return
            try:
                _merge_json_file(os.path.join(self._dir, _COOK_JSON), updates)
            except Exception:
                _logger.debug("Could not write cook.json", exc_info=True)


# -- files --


def _write_json(path: str, payload: Any) -> None:
    """Write ``payload`` through a temporary file, so a crash never leaves half a document."""
    tmp: str = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, path)


def _read_object(path: str) -> Dict[str, Any]:
    """The file's JSON object, or an empty one when the file is missing, corrupt, or holds anything else."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded: Any = json.load(f)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _merge_json_file(path: str, updates: Mapping[str, Any]) -> None:
    """Merge ``updates`` over the file's object, later keys winning. Anything but an object reads as empty."""
    existing: Dict[str, Any] = _read_object(path)
    existing.update(updates)
    # Stamped on every write, in UTC, so it says when the cook last changed the file.
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, existing)


# -- failures --


def _read_tail(log_tail: Callable[[], List[str]], task: str) -> Optional[List[str]]:
    """The worker's last lines, or None after warning that they could not be read."""
    try:
        return list(log_tail())
    except Exception as e:
        _logger.warning("Could not read the worker log for task %s: %s", task, e)
        return None


# -- the outcome --


def _counts_of(summary: CookSummary) -> Dict[str, Any]:
    """The summary's counts, as plain numbers and flags."""
    return {
        "step_count": int(summary.step_count),
        "task_count": int(summary.task_count),
        "succeeded": int(summary.succeeded),
        "failed": int(summary.failed),
        "canceled": int(summary.canceled),
        "stopped_by_user": bool(summary.stopped_by_user),
    }


def _is_abandoned(summary: CookSummary) -> bool:
    """A pass PDG started and dropped before offering work. It is not a cook, so it sends no event."""
    return (
        summary.final_phase in (CookPhase.IDLE, CookPhase.CONNECTED)
        and not summary.job_id
        and not summary.refusals
        and not summary.stopped_by_user
        and summary.step_count == summary.task_count == 0
        and summary.succeeded == summary.failed == summary.canceled == 0
    )


def _outcome_of(summary: CookSummary) -> str:
    """``succeeded``, ``failed``, ``canceled``, ``refused``, or ``setup_failed``, from the counts."""
    # The first class that applies wins, so a failure outranks a later stop.
    if summary.refusals:
        return "refused"
    if summary.failed > 0:
        return "failed"
    if summary.stopped_by_user or summary.canceled > 0:
        return "canceled"
    if summary.task_count > 0 and summary.succeeded >= summary.task_count:
        return "succeeded"
    if summary.job_id:
        # The job exists, but not every task completed.
        return "failed"
    if summary.task_count == 0 and summary.final_phase is CookPhase.DONE:
        # Nothing needed the farm, as in a static cook. The counts cannot tell this
        # apart from a setup failure, so only here does the phase decide.
        return "succeeded"
    return "setup_failed"


def _outcome_event(summary: CookSummary) -> Dict[str, Any]:
    """The event's details: the outcome class and counts, with no paths, names, or free text."""
    scenarios: List[str] = sorted({r.value for r in summary.refusals})
    return {
        "outcome": _outcome_of(summary),
        "refusal_scenarios": ",".join(scenarios),
        "refusal_count": len(summary.refusals),
        "job_submitted": bool(summary.job_id),
        **_counts_of(summary),
    }
