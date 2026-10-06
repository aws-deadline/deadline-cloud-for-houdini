# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Work item payloads and output records in S3.

PDG writes a work item's payload when it offers the item, after ``CreateJob``, so
the payload cannot be a job attachment. The scheduler stores it at a key derived
from the task's IDs, and the worker derives the same key from its session. This
module is the only place that key is computed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ports import OutputFile, TaskAddress

_PAYLOAD_FILENAME = "pdg_workitem.json.gz"

# Must not contain "output": job attachments finds a session's output manifest by
# a pattern that matches it, and a match here would hide the real manifest from
# the downstream steps.
_OUTPUTS_FILENAME = "__pdg_results.json"


class S3TaskStorage:
    """Implements ``TaskStorage`` under each task's job attachments output prefix."""

    def __init__(self, *, s3_client: Any, bucket: str, root_prefix: str) -> None:
        raise NotImplementedError

    @classmethod
    def for_queue(cls, *, deadline_client: Any, farm_id: str, queue_id: str) -> "S3TaskStorage":
        """Storage for the scheduler: the queue's bucket, reached with the queue role's credentials."""
        raise NotImplementedError

    def put_payload(self, address: TaskAddress, payload_json: str) -> None:
        """Store the payload gzip-compressed. Raises ``PayloadTransferError`` naming the key."""
        raise NotImplementedError

    def get_payload(self, address: TaskAddress) -> Dict[str, Any]:
        """Fetch and decode a payload. A missing object means the task ran before its payload was stored."""
        raise NotImplementedError

    def put_outputs(self, address: TaskAddress, outputs: Sequence[OutputFile]) -> None:
        """Publish the task's output record."""
        raise NotImplementedError

    def get_outputs(self, address: TaskAddress) -> Optional[List[OutputFile]]:
        """The task's output record, or None when there is none."""
        raise NotImplementedError
