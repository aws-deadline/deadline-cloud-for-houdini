# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""What a cook leaves behind: a debug directory, a log, and one telemetry event.

A cook is long and spread across Houdini, Deadline Cloud, and S3, so its record
is what explains a failure afterwards.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from .graph import GraphSnapshot, NodeInfo, TaskKey
from .ports import CookSummary, WorkItemHandle
from .scene_reads import ReadReport


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
        raise NotImplementedError

    def log_handler(self) -> logging.Handler:
        """A handler that holds lines in memory until the directory exists, then writes ``orchestrator.log``."""
        raise NotImplementedError

    def bundle_dir(self) -> str:
        """Where the job bundle is written, so the debug directory holds the bundle as submitted."""
        raise NotImplementedError

    def snapshot_taken(
        self, nodes: Sequence[NodeInfo], snapshot: GraphSnapshot, reads: Optional[ReadReport]
    ) -> None:
        """Write ``snapshot.json`` and ``scene_reads.json``, and ``graph.json`` at debug level 1 or above."""
        raise NotImplementedError

    def job_submitted(self, job_id: str) -> None:
        """Record the job ID in ``cook.json``."""
        raise NotImplementedError

    def item_scheduled(self, item: WorkItemHandle) -> None:
        """At debug level 1 or above, describe the first few work items of each node."""
        raise NotImplementedError

    def item_failed(self, key: TaskKey, log_tail: Callable[[], List[str]]) -> None:
        """Log the worker's last lines for the first few failures. ``log_tail`` is called only within that limit."""
        raise NotImplementedError

    def closed(self, summary: CookSummary) -> None:
        """Write the final counts, and send one outcome event that carries counts only: no paths or names."""
        raise NotImplementedError
