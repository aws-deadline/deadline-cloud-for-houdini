# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Fake Deadline Cloud and S3 clients that answer from reads recorded on a real job.

``fixtures/pyro_bonfire`` holds a real 26-task cook of the Pyro Bonfire scene: its debug
directory, and every Deadline Cloud and S3 read the comparison makes of its job, with
the IDs and paths replaced by made-up ones. The snapshot's ``items`` lists were added
from each step's first and last item, which the manifests confirm.

That cook did not record PDG's work items. ``pdg_work_items.json`` holds what PDG has
after such a cook: every item ``CookedSuccess``, with the files its task uploaded outside
PDG's own folders as its outputs.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from botocore.exceptions import ClientError

from deadline.houdini_pdg.ports import OutputFile
from test.integ.pdg.compare_cook_with_farm import PdgWorkItem
from test.integ.pdg.farm import FarmQueue, FarmView, read_farm_view

#: The recorded cook's debug directory.
PYRO_BONFIRE_DIR: Path = Path(__file__).parent / "fixtures" / "pyro_bonfire"

#: The farm and queue the recorded job ran on.
RECORDED_QUEUE: FarmQueue = FarmQueue(
    farm_id="farm-exfarm00000000000000000000000001",
    queue_id="queue-exqueue0000000000000000000000001",
)

#: The recorded job.
RECORDED_JOB_ID: str = "job-exjob000000000000000000000000001"


def recorded_reads() -> Dict[str, Any]:
    """A fresh copy of every recorded read, for a test to change."""
    reads: Dict[str, Any] = json.loads((PYRO_BONFIRE_DIR / "farm_reads.json").read_text())
    return reads


def recorded_pdg_work_items() -> List[PdgWorkItem]:
    """PDG's work items after the recorded cook, all succeeded."""
    entries: List[Dict[str, Any]] = json.loads(
        (PYRO_BONFIRE_DIR / "pdg_work_items.json").read_text()
    )
    return [
        PdgWorkItem(
            node=entry["node"],
            name=entry["name"],
            index=entry["index"],
            state=entry["state"],
            outputs=tuple(OutputFile(o["path"], o["tag"]) for o in entry["outputs"]),
        )
        for entry in entries
    ]


class FakeDeadline:
    """Answers GetJob, GetQueue, GetStep, ListSteps and ListTasks from recorded reads.

    Paginated calls return ``page_size`` results per page, so a test can check that
    every page is read.
    """

    def __init__(self, reads: Mapping[str, Any], page_size: int = 100) -> None:
        self._reads: Mapping[str, Any] = reads
        self._page_size: int = page_size

    def get_job(self, *, farmId: str, queueId: str, jobId: str) -> Dict[str, Any]:
        """The recorded job, after checking it is the one asked for."""
        assert (farmId, queueId, jobId) == (
            RECORDED_QUEUE.farm_id,
            RECORDED_QUEUE.queue_id,
            RECORDED_JOB_ID,
        )
        return copy.deepcopy(self._reads["get_job"])

    def get_queue(self, *, farmId: str, queueId: str) -> Dict[str, Any]:
        """The recorded queue's job attachments settings and name."""
        return copy.deepcopy(self._reads["get_queue"])

    def get_step(self, *, farmId: str, queueId: str, jobId: str, stepId: str) -> Dict[str, Any]:
        """The recorded GetStep response for one step."""
        return copy.deepcopy(self._reads["get_step"][stepId])

    def get_paginator(self, operation: str) -> "_Paginator":
        """ListSteps or ListTasks, split into pages."""
        results: Dict[str, Any] = {
            "list_steps": lambda **kw: ("steps", self._reads["list_steps"]),
            "list_tasks": lambda **kw: ("tasks", self._reads["list_tasks"][kw["stepId"]]),
        }
        return _Paginator(results[operation], self._page_size)


class FakeS3:
    """Answers ListObjectsV2 and GetObject from the recorded objects.

    A recorded object with no body, such as a work item payload, is listed but can't be
    read: the comparison never needs one. A missing key raises ``NoSuchKey``, as S3 does.
    """

    def __init__(self, objects: List[Mapping[str, Any]], page_size: int = 1000) -> None:
        self._objects: Dict[str, Mapping[str, Any]] = {o["Key"]: o for o in objects}
        self._page_size: int = page_size

    def get_object(self, *, Bucket: str, Key: str) -> Dict[str, Any]:
        """The object's body as a stream, with its metadata."""
        # Only the recorded bucket exists, and a payload is never read.
        assert Bucket == "example-job-attachments"
        recorded: Optional[Mapping[str, Any]] = self._objects.get(Key)
        if recorded is None:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": Key}}, "GetObject")
        assert "Body" in recorded, f"the comparison read an object it never needs: {Key}"
        return {
            "Body": io.BytesIO(recorded["Body"].encode("utf-8")),
            "Metadata": dict(recorded.get("Metadata") or {}),
        }

    def get_paginator(self, operation: str) -> "_Paginator":
        """ListObjectsV2, split into pages."""
        assert operation == "list_objects_v2"

        def listing(**kwargs: Any) -> Any:
            keys: List[Dict[str, str]] = [
                {"Key": key} for key in sorted(self._objects) if key.startswith(kwargs["Prefix"])
            ]
            return "Contents", keys

        return _Paginator(listing, self._page_size)


class _Paginator:
    """A boto3 paginator over a fixed result list. An empty page omits the key, as S3's does."""

    def __init__(self, results: Any, page_size: int) -> None:
        self._results: Any = results
        self._page_size: int = page_size

    def paginate(self, **kwargs: Any) -> Iterator[Dict[str, Any]]:
        """Each page of results."""
        # S3 leaves the result list out of an empty page, and the code must cope with that.
        key: str
        items: List[Any]
        key, items = self._results(**kwargs)
        if not items:
            yield {}
            return
        for start in range(0, len(items), self._page_size):
            yield {key: copy.deepcopy(items[start : start + self._page_size])}


def read_recorded_farm_view(reads: Optional[Mapping[str, Any]] = None) -> FarmView:
    """``read_farm_view`` on the recorded job, or on a changed copy of its reads."""
    source: Mapping[str, Any] = recorded_reads() if reads is None else reads
    return read_farm_view(
        RECORDED_QUEUE,
        RECORDED_JOB_ID,
        deadline_client=FakeDeadline(source),
        s3_client=FakeS3(source["s3_objects"]),
    )
