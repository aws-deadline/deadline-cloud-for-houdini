# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Work item payloads and output records in S3.

PDG writes a work item's payload when it offers the item, after ``CreateJob``, so
the payload cannot be a job attachment. The scheduler stores it at a key derived
from the task's IDs, and the worker derives the same key from its session. This
module is the only place that key is computed.
"""

from __future__ import annotations

import gzip
import json
import string
import zlib
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Type

from deadline.job_attachments.models import JobAttachmentS3Settings

from .errors import CookSetupError, OutputManifestError, PayloadTransferError
from .ports import OutputFile, TaskAddress

_PAYLOAD_FILENAME: str = "pdg_workitem.json.gz"

# Must not contain "output": job attachments finds a session's output manifest by
# a pattern that matches it, and a match here would hide the real manifest from
# the downstream steps.
_OUTPUTS_FILENAME: str = "__pdg_results.json"

# The worker and the workstation can run different releases, so a record names
# the format it was written in.
_RECORD_VERSION: int = 1

_GZIP_MAGIC: bytes = b"\x1f\x8b"

# S3 error codes that mean the object is not there.
_MISSING_CODES: FrozenSet[str] = frozenset({"NoSuchKey", "404", "NotFound"})

# What ``json.loads`` raises on bad input. ``RecursionError`` comes from input
# nested deeper than the parser can follow, and is not a ``ValueError``.
_JSON_ERRORS: Tuple[Type[Exception], ...] = (UnicodeDecodeError, ValueError, RecursionError)

# What ``gzip.decompress`` raises on a corrupt or truncated stream.
_GZIP_ERRORS: Tuple[Type[Exception], ...] = (OSError, EOFError, zlib.error)


class S3TaskStorage:
    """Implements ``TaskStorage`` under each task's job attachments output prefix."""

    def __init__(self, *, s3_client: Any, bucket: str, root_prefix: str) -> None:
        # A blank bucket or root would put every key at the bucket's top level, or
        # nowhere, so refuse them before any task depends on the keys. The root is
        # tested with spaces stripped too, but keeps them in the key, as job
        # attachments does.
        bucket_name: str = bucket.strip()
        root: str = root_prefix.strip("/")
        if not bucket_name or not root.strip(string.whitespace + "/"):
            raise CookSetupError(
                "The queue's job attachments settings need both an S3 bucket and a root "
                f"prefix, but the bucket is {bucket!r} and the root prefix is {root_prefix!r}."
            )

        self._s3: Any = s3_client
        self._bucket: str = bucket_name
        self._settings: JobAttachmentS3Settings = JobAttachmentS3Settings(
            s3BucketName=bucket_name, rootPrefix=root
        )

    def put_payload(self, address: TaskAddress, payload_json: str) -> None:
        """Store the payload gzip-compressed. Raises ``PayloadTransferError`` naming the key."""
        key: str = self._key(address, _PAYLOAD_FILENAME)
        # PDG's own bytes, compressed but not re-serialized, so the worker loads
        # exactly what PDG wrote. Text that cannot be encoded fails like a put, so
        # the error still names the key.
        try:
            body: bytes = gzip.compress(payload_json.encode("utf-8"))
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=body)
        except Exception as e:
            raise PayloadTransferError(
                f"Failed to store the work item payload at {self._uri(key)}: {e}"
            ) from e

    def get_payload(self, address: TaskAddress) -> Dict[str, Any]:
        """Fetch and decode a payload. A missing object means the task ran before its payload was stored."""
        key: str = self._key(address, _PAYLOAD_FILENAME)
        try:
            raw: bytes = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception as e:
            raise PayloadTransferError(
                f"Failed to fetch the work item payload from {self._uri(key)}: {e}. The "
                "scheduler stores it before it lets the task run, so a missing object means "
                "the task ran before its payload was ready."
            ) from e

        # Name the object in a decode failure, so the artist can fetch and inspect it.
        try:
            return _decode_payload(raw)
        except PayloadTransferError as e:
            raise PayloadTransferError(f"{e} ({self._uri(key)})") from e

    def put_outputs(self, address: TaskAddress, outputs: Sequence[OutputFile]) -> None:
        """Publish the task's output record. Raises ``PayloadTransferError`` naming the key."""
        key: str = self._key(address, _OUTPUTS_FILENAME)
        record: Dict[str, Any] = {
            "version": _RECORD_VERSION,
            "outputs": [{"path": o.path, "tag": o.tag} for o in outputs],
        }
        try:
            self._s3.put_object(
                Bucket=self._bucket, Key=key, Body=json.dumps(record).encode("utf-8")
            )
        except Exception as e:
            raise PayloadTransferError(
                f"Failed to store the task's output record at {self._uri(key)}: {e}"
            ) from e

    def get_outputs(self, address: TaskAddress) -> Optional[List[OutputFile]]:
        """The task's output record, or None when there is none."""
        # A task that published nothing has no record, which is not an error. Any
        # other failure to read it is.
        key: str = self._key(address, _OUTPUTS_FILENAME)
        try:
            raw: bytes = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception as e:
            if _is_missing(e):
                return None
            raise OutputManifestError(
                f"Cannot read the task's output record at {self._uri(key)}: {e}"
            ) from e

        # Name the object in a parse failure, so the artist can fetch and inspect it.
        try:
            return _parse_record(raw)
        except OutputManifestError as e:
            raise OutputManifestError(f"{e} ({self._uri(key)})") from e

    def _key(self, address: TaskAddress, filename: str) -> str:
        # Job attachments builds the same prefix for the task's output manifests,
        # so the files sit with the rest of the task's data. Slashes around an ID
        # would add an empty path segment.
        ids: List[str] = [
            address.farm_id,
            address.queue_id,
            address.job_id,
            address.step_id,
            address.task_id,
        ]
        prefix: str = self._settings.full_task_output_prefix(*(i.strip("/") for i in ids))
        return f"{prefix}/{filename}"

    def _uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"


def _decode_payload(raw: bytes) -> Dict[str, Any]:
    # Accept plain JSON too, in case a payload was stored uncompressed.
    text: bytes = _decompress_payload(raw) if raw[:2] == _GZIP_MAGIC else raw
    try:
        payload: Any = json.loads(text.decode("utf-8"))
    except _JSON_ERRORS as e:
        raise PayloadTransferError(f"The work item payload is not valid JSON: {e}") from e

    # PDG's loader reads the payload's fields by name, so anything else is not a payload.
    if not isinstance(payload, dict):
        raise PayloadTransferError(
            f"The work item payload must be a JSON object; got {_json_kind(payload)}"
        )
    return payload


def _decompress_payload(raw: bytes) -> bytes:
    # A broken stream is reported apart from bad JSON, so the error points at the
    # step that failed.
    try:
        return gzip.decompress(raw)
    except _GZIP_ERRORS as e:
        raise PayloadTransferError(
            f"The work item payload is a corrupt or truncated gzip stream: {e}"
        ) from e


def _parse_record(raw: bytes) -> List[OutputFile]:
    try:
        record: Any = json.loads(raw.decode("utf-8"))
    except _JSON_ERRORS as e:
        raise OutputManifestError(f"The task's output record is not valid JSON: {e}") from e
    if not isinstance(record, dict):
        raise OutputManifestError(
            f"The task's output record must be a JSON object; got {_json_kind(record)}"
        )
    # A JSON true equals 1 in Python, so a boolean is refused before the comparison.
    version: Any = record.get("version")
    if isinstance(version, bool) or version != _RECORD_VERSION:
        raise OutputManifestError(
            f"The task's output record version must be {_RECORD_VERSION}; "
            f"got {json.dumps(version)}"
        )
    entries: Any = record.get("outputs")
    if not isinstance(entries, list):
        raise OutputManifestError(
            'The task\'s output record must hold a list under "outputs"; '
            f"got {_json_kind(entries)}"
        )

    # Each entry is an object whose path and tag, when present, are strings.
    outputs: List[OutputFile] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise OutputManifestError(
                "Each entry in the task's output record must be a JSON object; "
                f"got {json.dumps(entry)}"
            )
        path: Any = entry.get("path")
        tag: Any = entry.get("tag")
        if path is not None and not isinstance(path, str):
            raise OutputManifestError(
                "An output path in the task's output record must be a string; "
                f"got {json.dumps(path)}"
            )
        if tag is not None and not isinstance(tag, str):
            raise OutputManifestError(
                "An output tag in the task's output record must be a string; "
                f"got {json.dumps(tag)}"
            )
        # An entry without a path names nothing, and the rest are still worth reporting.
        if path:
            # An empty tag makes PDG categorize the file by its extension.
            outputs.append(OutputFile(path=path, tag=tag or ""))
    return outputs


def _json_kind(value: Any) -> str:
    # Name a parsed value's type as JSON does, since the reader knows the stored
    # JSON, not Python's types. A bool is an int in Python, so it is tested first.
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    return "an object"


def _is_missing(error: Exception) -> bool:
    # botocore's ClientError carries the S3 error code in its response.
    response: Any = getattr(error, "response", None)
    code: Any = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return str(code) in _MISSING_CODES
