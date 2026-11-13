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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .graph import DeclaredFile, GraphSnapshot, NodeInfo, TaskKey, WorkItemSpec
from .houdini.readers import describe_work_item
from .ports import CookPhase, CookSummary, WorkItemHandle
from .scene_reads import ReadMatch, ReadReport, ReadVerdict

_logger: logging.Logger = logging.getLogger(__name__)

# A sibling of PDG's ``pdgtemp``, which PDG is free to clean.
_DEBUG_DIR_NAME: str = "pdg_debug"
_ORCHESTRATOR_LOG: str = "orchestrator.log"
_COOK_JSON: str = "cook.json"
_SNAPSHOT_JSON: str = "snapshot.json"
_SCENE_READS_JSON: str = "scene_reads.json"
_GRAPH_JSON: str = "graph.json"
_SCHEDULED_ITEMS: str = "scheduled_items.jsonl"
_SCHEDULED_SUMMARY: str = "scheduled_items_summary.json"
_BUNDLE_DIR: str = "bundle"

#: Named like the other Deadline Cloud client events.
_OUTCOME_EVENT: str = "com.amazon.rum.deadline.houdini_pdg.cook_outcome"

# PDG offers a node's items together, so a global cap would spend every slot on
# the first node. A few per node shows each node's item shape.
_DETAIL_PER_NODE: int = 3
# A cook's failures usually share one cause, and each tail costs API calls.
_FAILURE_TAIL_LIMIT: int = 3
# Per node in graph.json. Counts stay exact; only the per-item entries are capped.
_ITEM_DETAIL_LIMIT: int = 200
# Per item in graph.json: fan-out can reach thousands of edges.
_EDGE_LIST_LIMIT: int = 50
_UNPRODUCED_LIMIT: int = 200
# Lines held while no directory exists. A pass that never writes keeps its lines
# on the console only, so this bounds the memory it can hold.
_BUFFER_LIMIT: int = 10_000
# Suffixes tried when two cooks share a start second and a cook ID prefix.
_MAX_NAME_SUFFIX: int = 1_000

# File lists in a work item description, by key.
_FILE_LIST_KEYS: Tuple[str, ...] = (
    "input_files",
    "expected_input_files",
    "output_files",
    "expected_output_files",
)


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


class _DetailQuota:
    """Grants the first ``per_node`` offers of each node, and counts every offer."""

    def __init__(self, per_node: int) -> None:
        self.per_node: int = max(0, int(per_node))
        self.granted: Dict[str, int] = {}
        self.offered: Dict[str, int] = {}

    def grant(self, node_name: str) -> bool:
        """Count the offer, and say whether the node still has a detail slot left."""
        # Every offer is counted, granted or not, so the summary can say how many were skipped.
        self.offered[node_name] = self.offered.get(node_name, 0) + 1
        used: int = self.granted.get(node_name, 0)
        granted: bool = used < self.per_node
        if granted:
            self.granted[node_name] = used + 1
        return granted


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
        self._quota: _DetailQuota = _DetailQuota(_DETAIL_PER_NODE)
        self._sampled: List[Dict[str, Any]] = []
        self._items_by_name: Dict[str, WorkItemSpec] = {}
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
        # Index the generated items by name, so each scheduled item can be compared
        # with what generation declared for it.
        try:
            items: Dict[str, WorkItemSpec] = {
                item.name: item for step in snapshot.steps for item in step.items
            }
            with self._lock:
                self._items_by_name = items
        except Exception:
            _logger.debug("Could not index the snapshot's work items", exc_info=True)

        # Each file is written on its own, so one that fails costs only itself.
        self._write_artifact(_SNAPSHOT_JSON, lambda: _snapshot_payload(snapshot))
        self._write_artifact(_SCENE_READS_JSON, lambda: _scene_reads_payload(snapshot, reads))
        if self._debug_level >= 1:
            self._write_artifact(_GRAPH_JSON, lambda: _graph_payload(nodes, snapshot))

    def job_submitted(self, job_id: str) -> None:
        """Record the job ID in ``cook.json``."""
        try:
            if self._ensure_dir() is not None:
                self._merge_cook_json({"job_id": job_id})
        except Exception:
            _logger.debug("Could not record the job ID", exc_info=True)

    def item_scheduled(self, item: WorkItemHandle) -> None:
        """At debug level 1 or above, describe the first few work items of each node."""
        if self._debug_level < 1:
            return
        try:
            # Take a slot before describing, because describing is most of the cost.
            with self._lock:
                if not self._quota.grant(item.node_name):
                    return

            # Describe the live item and compare it with what generation declared.
            described: Dict[str, Any]
            source: str
            described, source = _describe(item)
            with self._lock:
                spec: Optional[WorkItemSpec] = self._items_by_name.get(item.name)
            diff: Dict[str, Any] = _diff_against_generation(spec, described)
            record: Dict[str, Any] = {
                "name": item.name,
                "node": item.node_name,
                "index": item.index,
                "described_from": source,
                "counts": {k: len(described[k]) for k in _FILE_LIST_KEYS if k in described},
                "diff": diff,
                "item": described,
            }
            line: str = json.dumps(record, default=str)

            # One appended line per item, so a cook that dies mid-way keeps every
            # complete record.
            with self._lock:
                self._sampled.append({"node": item.node_name, "diff": diff})
                directory: Optional[str] = self._ensure_dir()
                if directory is None:
                    return
                with open(os.path.join(directory, _SCHEDULED_ITEMS), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            _logger.debug("Could not record a scheduled work item", exc_info=True)

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
                    if self._debug_level >= 1 and self._sampled:
                        self._write_artifact(_SCHEDULED_SUMMARY, self._scheduled_summary)
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

    def _write_artifact(self, name: str, build: Callable[[], Any]) -> None:
        """Build one file's content and write it, logging a warning instead of raising."""
        try:
            payload: Any = build()
            directory: Optional[str] = self._ensure_dir()
            if directory is None:
                return
            _write_json(os.path.join(directory, name), payload)
        except Exception as e:
            _logger.warning("Could not write %s: %s", name, e)

    def _scheduled_summary(self) -> Dict[str, Any]:
        """Totals over every offered item, and counts of what changed in the described sample."""
        with self._lock:
            sampled: List[Dict[str, Any]] = list(self._sampled)
            offered: Dict[str, int] = dict(self._quota.offered)
            granted: Dict[str, int] = dict(self._quota.granted)
        known: List[Dict[str, Any]] = [
            s["diff"] for s in sampled if s["diff"].get("known_at_generation")
        ]

        def count(field: str) -> int:
            """How many sampled items generation knew and reported a change in ``field``."""
            return sum(1 for d in known if d.get(field))

        return {
            "per_node_limit": self._quota.per_node,
            "items_offered": sum(offered.values()),
            "items_offered_per_node": offered,
            "logged_items": len(sampled),
            "logged_per_node": granted,
            # Everything below counts the logged sample only.
            "sampled_unknown_at_generation": len(sampled) - len(known),
            "sampled_gained_input_files": count("gained_input_files"),
            "sampled_untokenized_inputs": count("untokenized_input_files"),
            "sampled_gained_declared_outputs": count("gained_declared_outputs"),
            "sampled_lost_declared_outputs": count("lost_declared_outputs"),
            "sampled_command_changed": count("command_changed"),
        }


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


# -- snapshot.json and scene_reads.json --


def _snapshot_payload(snapshot: GraphSnapshot) -> Dict[str, Any]:
    """The job's shape as submitted: totals, and each step's tasks, dependencies and work items."""
    return {
        "total_tasks": snapshot.total_tasks,
        "step_count": len(snapshot.steps),
        "leaf_steps": list(snapshot.leaf_steps()),
        # Partitioners and in-process nodes. A surprise here explains a surprising step list.
        "nodes_mapping_to_nothing": list(snapshot.skipped_nodes),
        "steps_with_remapped_indices": list(snapshot.remapped_steps),
        "steps_without_items": list(snapshot.steps_without_items),
        "steps": [
            {
                "name": step.name,
                "task_count": step.task_count,
                "task_range": step.task_range,
                "depends_on": list(step.dependencies),
                # The edges that exist because a task reads a file the other step writes.
                "depends_on_for_data": list(step.data_dependencies),
                "declares_outputs": step.declares_outputs,
                # Every step in a snapshot has at least one item.
                "first_item": step.items[0].name,
                "last_item": step.items[-1].name,
                # A step's items are in task-index order, so the task with TaskIndex i runs
                # items[i]. Nothing else outside the cook records which task ran which item.
                "items": [item.name for item in step.items],
            }
            for step in snapshot.steps
        ],
    }


def _scene_reads_payload(snapshot: GraphSnapshot, report: Optional[ReadReport]) -> Dict[str, Any]:
    """Each step's scene reads, the step each was attributed to, and the data edges they gave."""
    if report is None:
        return {"version": 1, "available": False, "steps": [], "summary": {}}

    # One entry per step that has a read or a data edge, in name order.
    by_step: Dict[str, List[ReadMatch]] = {}
    for match in report.matches:
        by_step.setdefault(match.read.step, []).append(match)
    step_of: Dict[str, Any] = {s.name: s for s in snapshot.steps}
    steps: List[Dict[str, Any]] = []
    for name in sorted(set(by_step) | set(report.data_edges)):
        matches: List[ReadMatch] = sorted(
            by_step.get(name, []), key=lambda m: (m.read.node_path, m.read.parm)
        )
        step: Any = step_of.get(name)
        steps.append(
            {
                "step": name,
                "dependencies": list(step.dependencies) if step else [],
                "data_edges": sorted(report.data_edges.get(name, ())),
                "data_edges_added": sorted(
                    {m.producer for m in matches if m.verdict is ReadVerdict.EDGE_ADDED}
                ),
                "already_covered": sorted(
                    {m.producer for m in matches if m.verdict is ReadVerdict.COVERED}
                ),
                "data_edges_refused_as_cyclic": sorted(snapshot.refused_data_edges.get(name, ())),
                "reads": [
                    {
                        "node": m.read.node_path,
                        "parm": m.read.parm,
                        "raw": m.read.raw,
                        "path": m.read.path,
                        "producer": m.producer,
                        "verdict": m.verdict.value,
                        **({"ambiguous_with": list(m.ambiguous_with)} if m.ambiguous_with else {}),
                    }
                    for m in matches
                ],
            }
        )

    # Every verdict appears in the summary, with 0 when no read had it.
    verdicts: Dict[str, int] = {v.value: 0 for v in ReadVerdict}
    for match in report.matches:
        verdicts[match.verdict.value] += 1
    return {
        "version": 1,
        "available": True,
        "steps": steps,
        "unportable": [
            {"step": r.step, "node": r.node_path, "parm": r.parm, "raw": r.raw, "path": r.path}
            for r in report.unportable
        ],
        "attribute_variables": [
            {
                "step": a.step,
                "node": a.node_path,
                "parm": a.parm,
                "raw": a.raw,
                "variable": a.variable,
                "attribute": a.attribute,
            }
            for a in report.attribute_variables
        ],
        "summary": {
            "reads": len(report.matches),
            **verdicts,
            "unportable": len(report.unportable),
            "attribute_variables": len(report.attribute_variables),
            "data_edges_refused_as_cyclic": sum(
                len(v) for v in snapshot.refused_data_edges.values()
            ),
        },
    }


# -- graph.json --


def _transitive_reduction(
    deps: Mapping[str, Set[str]], keep: Optional[Mapping[str, Set[str]]] = None
) -> Dict[str, Set[str]]:
    """Drop each edge another dependency already implies, except those in ``keep``.

    The result is only meaningful for acyclic input, which every snapshot is. A
    cycle still terminates.
    """
    protected: Mapping[str, Set[str]] = keep or {}
    ancestors: Dict[str, Set[str]] = {}

    def resolve(name: str, path: Set[str]) -> Set[str]:
        """Every step ``name`` depends on, directly or not, memoized in ``ancestors``."""
        if name in ancestors:
            return ancestors[name]
        # A name already on the current path is a cycle: stop rather than recurse.
        if name in path:
            return set()
        result: Set[str] = set()
        for parent in deps.get(name, ()):
            result.add(parent)
            result |= resolve(parent, path | {name})
        ancestors[name] = result
        return result

    for name in list(deps):
        resolve(name, set())

    # An edge is implied when another direct dependency already reaches it.
    reduced: Dict[str, Set[str]] = {}
    for name, direct in deps.items():
        must_keep: Set[str] = protected.get(name, set())
        reduced[name] = {
            dep
            for dep in direct
            if dep in must_keep
            or not any(dep in ancestors.get(other, set()) for other in direct if other != dep)
        }
    return reduced


def _why(node: NodeInfo, step_names: Set[str], without_items: Set[str]) -> str:
    """One sentence on why a node did or did not become a step."""
    if node.name in without_items:
        return "runs a command on a worker, but generated no items"
    # A known flag decides; without one, the items' commands do.
    if node.schedulable is not None:
        if node.schedulable:
            return f"runs a command on a worker ({len(node.items)} item(s) generated)"
        return "has no command for a worker, so it runs inside PDG"
    with_command: int = sum(1 for item in node.items if item.command)
    if with_command:
        return f"{with_command} of {len(node.items)} item(s) carry a command"
    if node.name in step_names:
        return "becomes a step"
    return f"none of {len(node.items)} item(s) carry a command"


def _edge_list(names: Sequence[str]) -> Dict[str, Any]:
    """An item's dependency names, capped, with the exact count."""
    entry: Dict[str, Any] = {"count": len(names), "names": list(names[:_EDGE_LIST_LIMIT])}
    if len(names) > _EDGE_LIST_LIMIT:
        entry["truncated"] = True
    return entry


def _files(files: Sequence[DeclaredFile]) -> List[Dict[str, str]]:
    """Each file's path and tag. The tag routes a file downstream, so it is always kept."""
    return [{"path": f.path, "tag": f.tag} for f in files]


def _graph_payload(nodes: Sequence[NodeInfo], snapshot: GraphSnapshot) -> Dict[str, Any]:
    """Everything known about the graph before submission, with each step's dependencies as submitted."""
    # The dependencies are the snapshot's, never recomputed, so the dump cannot
    # disagree with the job. The reduction only reports what it would drop.
    step_names: Set[str] = {s.name for s in snapshot.steps}
    without_items: Set[str] = set(snapshot.steps_without_items)
    deps: Dict[str, Set[str]] = {s.name: set(s.dependencies) for s in snapshot.steps}
    data: Dict[str, Set[str]] = {s.name: set(s.data_dependencies) for s in snapshot.steps}
    reduced: Dict[str, Set[str]] = _transitive_reduction(deps, keep=data)
    task_index: Dict[Tuple[str, str], int] = {
        (item.node_name, item.name): item.task_index for s in snapshot.steps for item in s.items
    }
    produced: Set[str] = {
        f.path for s in snapshot.steps for item in s.items for f in item.declared_outputs
    }

    entries: List[Dict[str, Any]] = []
    unproduced: Dict[Tuple[str, str], DeclaredFile] = {}
    truncated: List[str] = []
    totals: Dict[str, int] = {
        "declared_input_refs": 0,
        "declared_output_refs": 0,
        "item_dependency_edges": 0,
    }
    for node in sorted(nodes, key=lambda n: n.name):
        # Exact counts over every item, before the per-node cap below.
        inputs: int = sum(len(i.declared_inputs) for i in node.items)
        outputs: int = sum(len(i.declared_outputs) for i in node.items)
        edges: int = sum(len(i.depends_on) for i in node.items)
        totals["declared_input_refs"] += inputs
        totals["declared_output_refs"] += outputs
        totals["item_dependency_edges"] += edges
        for item in node.items:
            for f in item.declared_inputs:
                if f.path not in produced:
                    unproduced.setdefault((f.path, f.tag), f)

        # Item detail is capped per node, in PDG index order.
        ordered: List[Any] = sorted(node.items, key=lambda i: (i.index, i.name))
        shown: List[Any] = ordered[:_ITEM_DETAIL_LIMIT]
        if len(shown) < len(ordered):
            truncated.append(node.name)
        dependencies: List[str] = sorted(deps.get(node.name, ()))
        entries.append(
            {
                "name": node.name,
                "top_type": node.top_type,
                "is_dynamic": node.is_dynamic,
                "cook_type": node.cook_type,
                "scheduler_override": node.scheduler_override,
                "in_cook_set": node.in_cook_set,
                "becomes_step": node.name in step_names,
                "why": _why(node, step_names, without_items),
                "upstream_nodes": list(node.upstream),
                "dependencies": dependencies,
                "data_dependencies": sorted(data.get(node.name, ())),
                "dependencies_reduction_would_drop": sorted(
                    set(dependencies) - reduced.get(node.name, set())
                ),
                "data_dependencies_refused_as_cyclic": sorted(
                    snapshot.refused_data_edges.get(node.name, ())
                ),
                "dependencies_on_steps_without_items": sorted(
                    snapshot.dependencies_on_dropped.get(node.name, ())
                ),
                "exported_attribute_names": list(node.exported_attribute_names),
                "item_count": len(node.items),
                "declared_input_refs": inputs,
                "declared_output_refs": outputs,
                "item_dependency_edges": edges,
                "items_shown": len(shown),
                "items": [
                    {
                        "name": item.name,
                        "pdg_index": item.index,
                        "task_index": task_index.get((node.name, item.name)),
                        "item_id": item.item_id,
                        "is_out_of_process": item.is_out_of_process,
                        "is_batch": item.is_batch,
                        "batch_parent": item.batch_parent,
                        "command": item.command,
                        "depends_on": _edge_list(item.depends_on),
                        "declared_inputs": _files(item.declared_inputs),
                        "declared_outputs": _files(item.declared_outputs),
                    }
                    for item in shown
                ],
            }
        )

    unproduced_list: List[DeclaredFile] = sorted(unproduced.values(), key=lambda f: (f.path, f.tag))
    return {
        "version": 1,
        "totals": {
            "nodes": len(nodes),
            "work_items": sum(len(n.items) for n in nodes),
            "steps": len(snapshot.steps),
            "tasks": snapshot.total_tasks,
            "step_dependency_edges": sum(len(s.dependencies) for s in snapshot.steps),
            **totals,
        },
        "nodes": entries,
        # Inputs no task declares as an output: scene assets, or a file an
        # in-process node writes mid-cook.
        "unproduced_input_files": _files(unproduced_list[:_UNPRODUCED_LIMIT]),
        "unproduced_input_file_count": len(unproduced_list),
        "item_detail_limit": _ITEM_DETAIL_LIMIT,
        "edge_list_limit": _EDGE_LIST_LIMIT,
        "nodes_with_truncated_items": truncated,
    }


# -- scheduled items --


def _describe(item: WorkItemHandle) -> Tuple[Dict[str, Any], str]:
    """The live item's description, or the handle's own fields when the live item cannot be read."""
    try:
        described: Any = describe_work_item(item.native)
        # The reader returns a dict even when nothing could be read; with no name, it
        # read nothing, and its empty file lists would report every output as lost.
        if isinstance(described, dict) and described.get("name"):
            return described, "work_item"
    except Exception:
        # PDG's object can be gone by now; the handle's fields still describe the item.
        pass
    return (
        {
            "id": item.item_id,
            "index": item.index,
            "name": item.name,
            "node_name": item.node_name,
            "expected_output_files": _files(item.declared_outputs),
        },
        "handle",
    )


def _paths(entries: Any) -> Set[str]:
    """The ``path`` of each file entry in a description, skipping anything else."""
    paths: Set[str] = set()
    for entry in entries or ():
        if isinstance(entry, dict) and "path" in entry:
            paths.add(str(entry["path"]))
    return paths


def _diff_against_generation(
    spec: Optional[WorkItemSpec], described: Mapping[str, Any]
) -> Dict[str, Any]:
    """What changed between generation and ``onSchedule``, for the fields the job's shape relies on.

    Only fields the description carries are compared, so a fallback description
    reports no spurious change.
    """
    if spec is None:
        return {"known_at_generation": False}
    diff: Dict[str, Any] = {"known_at_generation": True}
    if "input_files" in described:
        generated_inputs: Set[str] = {f.path for f in spec.declared_inputs}
        now_inputs: Set[str] = _paths(described["input_files"])
        diff["gained_input_files"] = sorted(now_inputs - generated_inputs)
        # An input without a PDG token cannot be relocated by token substitution.
        diff["untokenized_input_files"] = sorted(p for p in now_inputs if "__PDG_" not in p)
    if "expected_output_files" in described:
        generated_outputs: Set[str] = {f.path for f in spec.declared_outputs}
        now_outputs: Set[str] = _paths(described["expected_output_files"])
        # Non-empty means the step's declares_outputs was decided too early.
        diff["gained_declared_outputs"] = sorted(now_outputs - generated_outputs)
        diff["lost_declared_outputs"] = sorted(generated_outputs - now_outputs)
    if "command" in described:
        diff["command_changed"] = str(described["command"]) != spec.command
        diff["command_at_generation"] = spec.command
    return diff


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
