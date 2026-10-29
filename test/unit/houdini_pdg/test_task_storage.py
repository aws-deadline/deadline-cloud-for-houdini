# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for storing work item payloads and output records in S3.

S3 is the only thing faked: a dict-backed bucket that raises botocore's own errors.
Keys are read back from that bucket after a put, so the tests use only the public
methods. The key layout is checked against a model built from the documented
layout, and the output-manifest collision against job attachments' own selection
code, not against the code under test.
"""

from __future__ import annotations

import gzip
import io
import json
import re
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from deadline.houdini_pdg.errors import CookSetupError, OutputManifestError, PayloadTransferError
from deadline.houdini_pdg.ports import OutputFile, TaskAddress, TaskStorage
from deadline.houdini_pdg.task_storage import S3TaskStorage

# Private, but it is the code that decides which manifests a downstream step syncs,
# so the collision tests run it instead of a copy of its rule.
from deadline.job_attachments.download import _get_tasks_manifests_keys_from_s3
from deadline.job_attachments.models import JobAttachmentS3Settings

ENV_NAMES: Tuple[str, ...] = (
    "DEADLINE_FARM_ID",
    "DEADLINE_QUEUE_ID",
    "DEADLINE_JOB_ID",
    "DEADLINE_STEP_ID",
    "DEADLINE_TASK_ID",
)

ADDRESS: TaskAddress = TaskAddress("farm-a", "queue-b", "job-c", "step-d", "task-d-0")

# The pattern job attachments uses to find a task's output manifests, from
# ``_get_tasks_manifests_keys_from_s3`` in ``deadline/job_attachments/download.py``.
OUTPUT_MANIFEST_DISCOVERY_PATTERN: str = r"step-.*/.*/.*output.*"

# The file names are read by a different release on the worker than the one that
# wrote them on the workstation, so they are part of the format.
PAYLOAD_FILENAME: str = "pdg_workitem.json.gz"
OUTPUTS_FILENAME: str = "__pdg_results.json"


class FakeS3:
    """A dict-backed bucket that raises S3's own errors, and counts calls."""

    def __init__(self) -> None:
        # Objects by (bucket, key), every key put and every (bucket, key) fetched in
        # call order, and failures a test plants for the next put or get.
        self.objects: Dict[Tuple[str, str], bytes] = {}
        self.puts: List[str] = []
        self.gets: List[Tuple[str, str]] = []
        self.fail_put: Optional[Exception] = None
        self.fail_get: Optional[Exception] = None

        # Job attachments registers a request hook on its S3 client. The fake
        # sends no requests, so the hook is never needed.
        self.meta: Any = SimpleNamespace(events=SimpleNamespace(register=lambda *a, **k: None))

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> Dict[str, Any]:
        """Store ``Body``, or raise the planted failure."""
        # The attempt counts even when it fails, as an S3 request does.
        self.puts.append(Key)
        if self.fail_put:
            raise self.fail_put
        self.objects[(Bucket, Key)] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> Dict[str, Any]:
        """Return the stored body as a stream, or raise ``NoSuchKey`` as S3 does."""
        # The attempt counts even when it fails. A planted failure wins, then a
        # missing object, as S3 orders them.
        self.gets.append((Bucket, Key))
        if self.fail_get:
            raise self.fail_get
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def get_paginator(self, operation: str) -> Any:
        """A paginator for ``list_objects_v2``, the only listing job attachments makes."""
        if operation != "list_objects_v2":
            raise NotImplementedError(operation)
        return SimpleNamespace(paginate=self._list_pages)

    def _list_pages(self, *, Bucket: str, Prefix: str) -> List[Dict[str, Any]]:
        """One page holding every key under ``Prefix``, sorted as S3 lists them."""
        keys: List[str] = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        return [{"Contents": [{"Key": k} for k in keys]}]


class FakeSession:
    """A boto3 session whose S3 client is a fake bucket, for job attachments' own code."""

    def __init__(self, s3: FakeS3) -> None:
        self._s3: FakeS3 = s3

    def client(self, service_name: str, **kwargs: Any) -> FakeS3:
        """The fake bucket, whatever client settings are asked for."""
        return self._s3


def _client_error(code: str, operation: str) -> ClientError:
    """The error botocore raises for an S3 error response with this code."""
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _storage(s3: Optional[FakeS3] = None, root: str = "DeadlineCloud") -> TaskStorage:
    """Storage over ``s3``, or over an empty fake bucket, in bucket ``bucket``."""
    return S3TaskStorage(s3_client=s3 or FakeS3(), bucket="bucket", root_prefix=root)


def _payload_key(address: TaskAddress, root: str = "DeadlineCloud") -> str:
    """The key ``put_payload`` writes for ``address``, read from a fresh fake bucket."""
    s3: FakeS3 = FakeS3()
    _storage(s3, root).put_payload(address, "{}")
    return s3.puts[0]


def _outputs_key(address: TaskAddress, root: str = "DeadlineCloud") -> str:
    """The key ``put_outputs`` writes for ``address``, read from a fresh fake bucket."""
    s3: FakeS3 = FakeS3()
    _storage(s3, root).put_outputs(address, [])
    return s3.puts[0]


def _env(address: TaskAddress) -> Dict[str, str]:
    """The session variables a worker agent sets for ``address``."""
    ids: Tuple[str, ...] = (
        address.farm_id,
        address.queue_id,
        address.job_id,
        address.step_id,
        address.task_id,
    )
    return dict(zip(ENV_NAMES, ids))


def _hex() -> st.SearchStrategy[str]:
    """32 lowercase hex digits, the shape of a Deadline Cloud resource ID's suffix."""
    return st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)


@st.composite
def addresses(draw: Any) -> TaskAddress:
    """Task addresses shaped like real farm, queue, job, step, and task IDs."""
    step: str = draw(_hex())
    return TaskAddress(
        farm_id="farm-" + draw(_hex()),
        queue_id="queue-" + draw(_hex()),
        job_id="job-" + draw(_hex()),
        step_id="step-" + step,
        task_id=f"task-{step}-{draw(st.integers(min_value=0, max_value=100_000))}",
    )


# One to three path segments of S3-safe characters, joined by slashes.
root_prefixes: st.SearchStrategy[str] = st.lists(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.", min_size=1
    ),
    min_size=1,
    max_size=3,
).map("/".join)

# JSON objects with nested scalars, lists, and objects, as PDG serializes them.
json_payloads: st.SearchStrategy[Dict[str, Any]] = st.dictionaries(
    st.text(),
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
        lambda children: st.lists(children) | st.dictionaries(st.text(), children),
        max_leaves=20,
    ),
)


# Keys


@given(address=addresses(), root=root_prefixes)
def test_keys_sit_under_the_job_attachments_task_prefix(address: TaskAddress, root: str) -> None:
    """Both files sit in the folder job attachments uses for the task's output manifests.

    The worker and the workstation each compute the key, and job attachments'
    cleanup and download tools look under this prefix.
    """
    # The documented layout, and job attachments' own prefix, agree.
    layout: str = "/".join(
        [
            root,
            "Manifests",
            address.farm_id,
            address.queue_id,
            address.job_id,
            address.step_id,
            address.task_id,
        ]
    )
    task_prefix: str = JobAttachmentS3Settings(
        s3BucketName="bucket", rootPrefix=root
    ).full_task_output_prefix(
        address.farm_id, address.queue_id, address.job_id, address.step_id, address.task_id
    )
    assert task_prefix == layout

    # Both files sit directly in that folder.
    assert _payload_key(address, root) == f"{layout}/{PAYLOAD_FILENAME}"
    assert _outputs_key(address, root) == f"{layout}/{OUTPUTS_FILENAME}"


@given(a=addresses(), b=addresses())
def test_distinct_tasks_never_share_a_key(a: TaskAddress, b: TaskAddress) -> None:
    """Two tasks with real-shaped IDs never read each other's payload or record, and a task's two files differ."""
    if a != b:
        assert _payload_key(a) != _payload_key(b)
        assert _outputs_key(a) != _outputs_key(b)
    assert _payload_key(a) != _outputs_key(a)


def test_slashes_around_the_root_prefix_never_reach_the_key() -> None:
    """A root prefix written with slashes gives no leading slash and no empty segment.

    S3 treats ``/Root/x`` and ``Root/x`` as different keys, so the worker would
    look in the wrong place.
    """
    key: str = _payload_key(ADDRESS, root="/Deadline/Cloud/")
    assert key.startswith("Deadline/Cloud/Manifests/farm-a/")
    assert "//" not in key


def test_slashes_around_an_id_never_reach_the_key() -> None:
    """An ID with slashes around it names the same key as the bare ID."""
    padded: TaskAddress = TaskAddress("/farm-a/", "queue-b", "job-c", "step-d", "task-d-0/")
    assert _payload_key(padded) == _payload_key(ADDRESS)
    assert _outputs_key(padded) == _outputs_key(ADDRESS)


@pytest.mark.parametrize("root", ["", "/", "//", " ", "/ /", " /"])
def test_a_root_prefix_that_strips_to_nothing_is_refused(root: str) -> None:
    """Without a root prefix every key would start at the top of the bucket, a slash, or a space."""
    with pytest.raises(CookSetupError, match="root prefix"):
        _storage(root=root)


@pytest.mark.parametrize("bucket", ["", " "])
def test_an_empty_bucket_is_refused(bucket: str) -> None:
    """A blank bucket can store nothing, so the cook stops before it submits."""
    with pytest.raises(CookSetupError, match="bucket"):
        S3TaskStorage(s3_client=FakeS3(), bucket=bucket, root_prefix="Root")


def test_a_bucket_with_spaces_around_it_stores_in_the_bare_bucket() -> None:
    """Spaces copied around a bucket name do not reach S3, which would refuse the name."""
    s3: FakeS3 = FakeS3()
    S3TaskStorage(s3_client=s3, bucket=" bucket ", root_prefix="Root").put_payload(ADDRESS, "{}")
    assert [bucket for bucket, _ in s3.objects] == ["bucket"]


@pytest.mark.parametrize("field", ["farm_id", "queue_id", "job_id", "step_id", "task_id"])
@pytest.mark.parametrize("value", ["", " ", "/", "//", "/ /"])
def test_an_address_with_an_empty_id_cannot_exist(field: str, value: str) -> None:
    """An empty ID drops a folder from the key, so two tasks could share one payload."""
    ids: Dict[str, str] = {
        "farm_id": "farm-a",
        "queue_id": "queue-b",
        "job_id": "job-c",
        "step_id": "step-d",
        "task_id": "task-d-0",
    }
    with pytest.raises(ValueError, match=field):
        TaskAddress(**{**ids, field: value})


def test_an_address_cannot_change_once_made() -> None:
    """Both sides derive keys from an address, so changing one after a put would orphan the payload."""
    with pytest.raises(FrozenInstanceError):
        ADDRESS.task_id = "task-d-1"  # type: ignore[misc]


# Keys from the session environment


@given(
    address=addresses(),
    extra=st.dictionaries(
        st.text(min_size=1).filter(lambda k: k not in ENV_NAMES), st.text(), max_size=5
    ),
)
def test_the_worker_reads_the_scheduler_address_from_its_session(
    address: TaskAddress, extra: Dict[str, str]
) -> None:
    """The worker's address equals the one the scheduler stored the payload under.

    Other variables in the session never change it.
    """
    worker: TaskAddress = TaskAddress.from_env({**extra, **_env(address)})
    assert worker == address
    assert _payload_key(worker) == _payload_key(address)


@given(
    removed=st.sets(st.sampled_from(ENV_NAMES), min_size=1),
    blank=st.sampled_from([None, "", "  ", "/"]),
)
def test_every_missing_or_empty_variable_is_named(removed: Set[str], blank: Optional[str]) -> None:
    """The error names exactly the variables that are missing or blank, all at once.

    The artist fixes the session environment once, not one variable per failed run.
    """
    # Remove or blank the chosen variables, then check the message names exactly them.
    env: Dict[str, str] = _env(ADDRESS)
    for name in removed:
        if blank is None:
            del env[name]
        else:
            env[name] = blank
    with pytest.raises(PayloadTransferError) as excinfo:
        TaskAddress.from_env(env)
    named: Set[str] = set(re.findall(r"DEADLINE_[A-Z]+_ID", str(excinfo.value)))
    assert named == removed


def test_the_process_environment_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no mapping, the worker reads its own process environment."""
    for name, value in _env(ADDRESS).items():
        monkeypatch.setenv(name, value)
    assert TaskAddress.from_env() == ADDRESS


# Collision with output manifest discovery


def _manifest_key(address: TaskAddress, root: str = "DeadlineCloud") -> str:
    """Where the worker agent writes the task's output manifest for one session action."""
    prefix: str = _payload_key(address, root).rsplit("/", 1)[0]
    return f"{prefix}/2026-08-27T23:32:32.974781Z_sessionaction-x-2/abc123_output"


def _job_attachments_selection(s3: FakeS3, address: TaskAddress, root: str) -> List[str]:
    """The output manifests job attachments would sync from the address's step."""
    step_prefix: str = JobAttachmentS3Settings(
        s3BucketName="bucket", rootPrefix=root
    ).full_step_output_prefix(address.farm_id, address.queue_id, address.job_id, address.step_id)
    return _get_tasks_manifests_keys_from_s3(step_prefix, "bucket", session=FakeSession(s3))


# A root that itself names a step, a task, or an output could match the pattern
# whatever the file names are, and is not a real job attachments root.
plain_roots: st.SearchStrategy[str] = root_prefixes.filter(
    lambda r: not any(word in r for word in ("task-", "step-", "output"))
)


@given(address=addresses(), root=plain_roots)
def test_our_files_are_never_mistaken_for_an_output_manifest(
    address: TaskAddress, root: str
) -> None:
    """Downstream steps still find the real output manifest next to the payload and record.

    A file that matched the pattern and sorted after the session folder would be
    picked instead, and the downstream step would sync no inputs.
    """
    # Store both files, then check neither name nor key matches the discovery pattern.
    s3: FakeS3 = FakeS3()
    storage: TaskStorage = _storage(s3, root)
    storage.put_payload(address, "{}")
    storage.put_outputs(address, [OutputFile("a.bgeo.sc")])
    for key in s3.puts:
        assert "output" not in key.rsplit("/", 1)[1]
        assert not re.search(OUTPUT_MANIFEST_DISCOVERY_PATTERN, key)

    # Job attachments' own selection over the task's folder picks only the real manifest.
    manifest: str = _manifest_key(address, root)
    _plant(s3, manifest, b"{}")
    assert _job_attachments_selection(s3, address, root) == [manifest]


# Payloads


def _plant(s3: FakeS3, key: str, raw: bytes) -> None:
    """Put ``raw`` at ``key`` in the fake bucket, as another writer would."""
    s3.objects[("bucket", key)] = raw


@given(payload=json_payloads)
def test_a_payload_round_trips_and_is_stored_gzipped(payload: Dict[str, Any]) -> None:
    """The worker gets back the object the scheduler stored, and S3 holds it compressed."""
    # Store, check the stored bytes start with the gzip magic, and fetch it back.
    s3: FakeS3 = FakeS3()
    storage: TaskStorage = _storage(s3)
    storage.put_payload(ADDRESS, json.dumps(payload))
    assert s3.objects[("bucket", _payload_key(ADDRESS))][:2] == b"\x1f\x8b"
    assert storage.get_payload(ADDRESS) == payload


@given(payload=json_payloads)
def test_a_plain_json_payload_is_accepted(payload: Dict[str, Any]) -> None:
    """An uncompressed payload decodes too, in case one was stored that way."""
    s3: FakeS3 = FakeS3()
    _plant(s3, _payload_key(ADDRESS), json.dumps(payload).encode("utf-8"))
    assert _storage(s3).get_payload(ADDRESS) == payload


@settings(max_examples=300)
@given(raw=st.binary())
def test_any_stored_bytes_decode_to_an_object_or_raise_payload_transfer_error(raw: bytes) -> None:
    """Whatever is at the key, the worker gets an object or the module's own error.

    Any other exception would escape the worker's handling and hide the cause.
    """
    for candidate in (raw, b"\x1f\x8b" + raw, gzip.compress(raw)):
        s3: FakeS3 = FakeS3()
        _plant(s3, _payload_key(ADDRESS), candidate)
        try:
            result: Dict[str, Any] = _storage(s3).get_payload(ADDRESS)
        except PayloadTransferError:
            continue
        assert isinstance(result, dict)


@pytest.mark.parametrize("compress", [False, True])
def test_a_payload_nested_too_deeply_to_parse_is_a_payload_transfer_error(compress: bool) -> None:
    """Input nested past the parser's limit raises the module's error, not ``RecursionError``."""
    # Plant 100,000 open brackets, well past the parser's nesting limit.
    raw: bytes = b"[" * 100_000
    s3: FakeS3 = FakeS3()
    _plant(s3, _payload_key(ADDRESS), gzip.compress(raw) if compress else raw)
    with pytest.raises(PayloadTransferError, match="not valid JSON"):
        _storage(s3).get_payload(ADDRESS)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (b"[]", "an array"),
        (b"1", "a number"),
        (b"1.5", "a number"),
        (b'"text"', "a string"),
        (b"null", "null"),
        (b"true", "a boolean"),
        (gzip.compress(b"[1, 2]", mtime=0), "an array"),
    ],
)
def test_a_payload_that_is_not_an_object_is_refused(raw: bytes, kind: str) -> None:
    """Valid JSON that is not an object is not a work item, so it is refused by name.

    The error names what was stored in JSON's terms, so a boolean is not reported
    as a number and nothing is reported by a Python type name.
    """
    s3: FakeS3 = FakeS3()
    _plant(s3, _payload_key(ADDRESS), raw)
    with pytest.raises(PayloadTransferError, match=f"must be a JSON object; got {kind} "):
        _storage(s3).get_payload(ADDRESS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (gzip.compress(b'{"a": 1}', mtime=0)[:-6], "corrupt or truncated gzip stream"),
        (b"\x1f\x8b\x08garbage", "corrupt or truncated gzip stream"),
        (b"not json", "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        (gzip.compress(b"not json", mtime=0), "not valid JSON"),
    ],
    ids=["truncated gzip", "corrupt gzip", "not json", "not utf-8", "gzip of not json"],
)
def test_a_corrupt_stored_payload_names_its_location(raw: bytes, expected: str) -> None:
    """A truncated, corrupt, or non-UTF-8 payload is refused, naming the object to inspect.

    A broken gzip stream and a good stream holding bad JSON are told apart, so the
    error does not blame the JSON for a stream that never decompressed.
    """
    # Plant the bad bytes where the payload belongs, then fetch it.
    s3: FakeS3 = FakeS3()
    _plant(s3, _payload_key(ADDRESS), raw)
    with pytest.raises(PayloadTransferError, match=expected) as excinfo:
        _storage(s3).get_payload(ADDRESS)
    assert f"s3://bucket/{_payload_key(ADDRESS)}" in str(excinfo.value)


def test_payload_bytes_are_what_pdg_serialized() -> None:
    """The stored bytes are PDG's text, not a re-serialization that could reorder or round."""
    # Text whose key order, number spelling, and non-ASCII character a re-dump would change.
    s3: FakeS3 = FakeS3()
    text: str = '{"b": 1.10, "a": "é"}'
    _storage(s3).put_payload(ADDRESS, text)
    stored: bytes = s3.objects[("bucket", _payload_key(ADDRESS))]
    assert gzip.decompress(stored) == text.encode("utf-8")


@given(times=st.integers(min_value=1, max_value=5))
def test_storing_a_payload_is_one_put_and_repeating_it_changes_nothing(times: int) -> None:
    """Each store is one ``PutObject``, and storing again leaves the same payload."""
    # Store once in one bucket and several times in another, then compare.
    once: FakeS3 = FakeS3()
    many: FakeS3 = FakeS3()
    _storage(once).put_payload(ADDRESS, '{"a": 1}')
    for _ in range(times):
        _storage(many).put_payload(ADDRESS, '{"a": 1}')
    assert len(once.puts) == 1
    assert len(many.puts) == times
    assert {k: gzip.decompress(v) for k, v in once.objects.items()} == {
        k: gzip.decompress(v) for k, v in many.objects.items()
    }


def test_a_failed_payload_put_names_the_key() -> None:
    """A refused store says where it tried to write, and does not retry on its own."""
    # S3 refuses the put; the error names the key and S3 saw exactly one request.
    s3: FakeS3 = FakeS3()
    s3.fail_put = _client_error("AccessDenied", "PutObject")
    with pytest.raises(PayloadTransferError) as excinfo:
        _storage(s3).put_payload(ADDRESS, "{}")
    assert _payload_key(ADDRESS) in str(excinfo.value)
    assert len(s3.puts) == 1


def test_a_payload_that_cannot_be_encoded_names_the_key() -> None:
    """Text with a lone surrogate has no UTF-8 form, so it fails like a refused put and stores nothing."""
    # Store the text; the error names the key and the bucket stays empty.
    s3: FakeS3 = FakeS3()
    with pytest.raises(PayloadTransferError) as excinfo:
        _storage(s3).put_payload(ADDRESS, '{"a": "\udcff"}')
    assert _payload_key(ADDRESS) in str(excinfo.value)
    assert s3.objects == {}


@pytest.mark.parametrize(
    "failure", [None, _client_error("AccessDenied", "GetObject"), RuntimeError("network down")]
)
def test_a_payload_the_worker_cannot_fetch_says_the_task_ran_too_early(
    failure: Optional[Exception],
) -> None:
    """A missing or unreadable payload names its S3 URI and the likely cause.

    The scheduler stores the payload before it lets the task run, so the usual
    cause is a task that started too soon.
    """
    # No planted failure means the object is simply missing.
    s3: FakeS3 = FakeS3()
    s3.fail_get = failure
    with pytest.raises(PayloadTransferError) as excinfo:
        _storage(s3).get_payload(ADDRESS)
    message: str = str(excinfo.value)
    assert f"s3://bucket/{_payload_key(ADDRESS)}" in message
    assert "ran before its payload was ready" in message


# Output records


output_files: st.SearchStrategy[OutputFile] = st.builds(
    OutputFile,
    path=st.text(),
    tag=st.sampled_from(["", "file/geo", "file/image/exr", "file/usd"]) | st.text(),
)


@given(outputs=st.lists(output_files, max_size=10))
def test_an_output_record_round_trips_the_entries_with_paths(outputs: List[OutputFile]) -> None:
    """The scheduler reads back the worker's outputs, in order, with their tags.

    An entry with an empty path names no file, so it is left out.
    """
    storage: TaskStorage = _storage()
    storage.put_outputs(ADDRESS, outputs)
    assert storage.get_outputs(ADDRESS) == [o for o in outputs if o.path]


def test_the_record_format_is_versioned() -> None:
    """The record is versioned JSON, since the worker and the workstation can run different releases."""
    # Read the stored record as plain JSON, as another release would.
    s3: FakeS3 = FakeS3()
    _storage(s3).put_outputs(ADDRESS, [OutputFile("__PDG_DIR__/geo/a.bgeo.sc", "file/geo")])
    record: Any = json.loads(s3.objects[("bucket", _outputs_key(ADDRESS))])
    assert record == {
        "version": 1,
        "outputs": [{"path": "__PDG_DIR__/geo/a.bgeo.sc", "tag": "file/geo"}],
    }


@pytest.mark.parametrize("recorded", [False, True], ids=["no record", "record"])
def test_reading_outputs_is_one_get_of_the_output_record(recorded: bool) -> None:
    """Reading a task's outputs is exactly one ``GetObject``, of the record's key.

    The cook reads outputs for every finished task of a step that declares none, so
    a second fetch, or a fetch of the payload instead, would multiply requests or
    read the wrong file.
    """
    # Optionally store a record, then read the outputs and list what was fetched.
    s3: FakeS3 = FakeS3()
    storage: TaskStorage = _storage(s3)
    if recorded:
        storage.put_outputs(ADDRESS, [OutputFile("a.bgeo.sc")])
    storage.get_outputs(ADDRESS)
    assert s3.gets == [("bucket", _outputs_key(ADDRESS))]


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_every_missing_object_code_is_none(code: str) -> None:
    """S3 reports a missing object with any of these codes, depending on the call and permissions."""
    s3: FakeS3 = FakeS3()
    s3.fail_get = _client_error(code, "GetObject")
    assert _storage(s3).get_outputs(ADDRESS) is None


@pytest.mark.parametrize(
    "failure",
    [_client_error("AccessDenied", "GetObject"), RuntimeError("network down")],
)
def test_an_unreadable_output_record_names_its_location(failure: Exception) -> None:
    """A record that exists but cannot be read is an error, not "no outputs"."""
    # The get fails for a reason other than a missing object.
    s3: FakeS3 = FakeS3()
    s3.fail_get = failure
    with pytest.raises(OutputManifestError) as excinfo:
        _storage(s3).get_outputs(ADDRESS)
    assert f"s3://bucket/{_outputs_key(ADDRESS)}" in str(excinfo.value)


def _stored_record(raw: bytes) -> TaskStorage:
    """Storage whose bucket holds ``raw`` as the task's output record."""
    s3: FakeS3 = FakeS3()
    _plant(s3, _outputs_key(ADDRESS), raw)
    return _storage(s3)


def test_entries_keep_their_recorded_tag_or_an_empty_one() -> None:
    """A recorded tag is kept as written, and a missing one becomes empty.

    PDG tags a file with an empty tag by its extension, so nothing is inferred here.
    """
    raw: bytes = json.dumps(
        {
            "version": 1,
            "outputs": [
                {"path": "a.bgeo.sc", "tag": "file/geo/custom"},
                {"path": "b.exr"},
                {"path": "c.usd", "tag": None},
                {"path": "", "tag": "file/geo"},
                {"tag": "file/geo"},
                {"path": None, "tag": "file/geo"},
            ],
        }
    ).encode()
    assert _stored_record(raw).get_outputs(ADDRESS) == [
        OutputFile("a.bgeo.sc", "file/geo/custom"),
        OutputFile("b.exr", ""),
        OutputFile("c.usd", ""),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"not json",
        pytest.param(b"[" * 100_000, id="nested too deeply to parse"),
        b"[]",
        b"null",
        b"{}",
        b'{"outputs": [{"path": "a"}]}',
        b'{"version": 1, "outputs": null}',
        b'{"version": 1, "outputs": {}}',
        b'{"version": 1, "outputs": ["a.bgeo"]}',
        b'{"version": 1, "outputs": [{"path": 3}]}',
        b'{"version": 1, "outputs": [{"path": "a", "tag": 3}]}',
        b'{"version": 2, "outputs": []}',
    ],
)
def test_a_malformed_record_raises_output_manifest_error_naming_its_location(raw: bytes) -> None:
    """Every malformed record raises the module's error, naming the object to inspect.

    An ``AttributeError`` or ``RecursionError`` instead would escape the cook's
    handling of a bad record.
    """
    with pytest.raises(OutputManifestError) as excinfo:
        _stored_record(raw).get_outputs(ADDRESS)
    assert f"s3://bucket/{_outputs_key(ADDRESS)}" in str(excinfo.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"[]", "must be a JSON object; got an array"),
        (b'{"version": 2, "outputs": []}', "version must be 1; got 2"),
        (b'{"version": "1", "outputs": []}', 'version must be 1; got "1"'),
        (b'{"version": true, "outputs": []}', "version must be 1; got true"),
        (b'{"outputs": [{"path": "a"}]}', "version must be 1; got null"),
        (b'{"version": 1, "outputs": {}}', 'must hold a list under "outputs"; got an object'),
        (b'{"version": 1}', 'must hold a list under "outputs"; got null'),
        (b'{"version": 1, "outputs": ["a.bgeo"]}', 'must be a JSON object; got "a.bgeo"'),
        (
            b'{"version": 1, "outputs": [{"path": 3}]}',
            "path in the task's output record must be a string; got 3",
        ),
        (
            b'{"version": 1, "outputs": [{"path": "a", "tag": 3}]}',
            "tag in the task's output record must be a string; got 3",
        ),
    ],
)
def test_a_malformed_record_says_what_it_expected_and_what_it_got(
    raw: bytes, expected: str
) -> None:
    """Each shape error names the rule the record broke and the value it holds.

    The value is shown as it appears in the stored JSON, so a string "1" and a
    number 1 read differently, and the artist can find the entry in the object.
    """
    with pytest.raises(OutputManifestError, match=re.escape(expected) + " "):
        _stored_record(raw).get_outputs(ADDRESS)


@settings(max_examples=300)
@given(raw=st.binary() | st.text().map(str.encode))
def test_any_record_bytes_parse_to_a_list_or_raise_output_manifest_error(raw: bytes) -> None:
    """Whatever bytes are stored, the result is a list of outputs or the module's error."""
    try:
        result: Optional[List[OutputFile]] = _stored_record(raw).get_outputs(ADDRESS)
    except OutputManifestError:
        return
    assert isinstance(result, list)


@given(
    entries=st.lists(
        st.dictionaries(
            st.sampled_from(["path", "tag", "checksum"]),
            st.none() | st.text() | st.integers() | st.lists(st.text(), max_size=2),
        ),
        max_size=5,
    )
)
def test_any_record_shape_parses_to_outputs_with_paths_or_raises_output_manifest_error(
    entries: List[Dict[str, Any]],
) -> None:
    """Entries of any shape give outputs that each name a file, or the module's error."""
    # A well-formed record around entries of any shape.
    raw: bytes = json.dumps({"version": 1, "outputs": entries}).encode()
    try:
        result: Optional[List[OutputFile]] = _stored_record(raw).get_outputs(ADDRESS)
    except OutputManifestError:
        return
    assert result is not None
    assert all(isinstance(o, OutputFile) and o.path for o in result)


def test_a_failed_output_record_put_names_the_key() -> None:
    """A record that cannot be stored raises the transfer error, naming where it tried to write."""
    # The put fails; the error is the transfer error and names the record's key.
    s3: FakeS3 = FakeS3()
    s3.fail_put = RuntimeError("network down")
    with pytest.raises(PayloadTransferError) as excinfo:
        _storage(s3).put_outputs(ADDRESS, [OutputFile("a.bgeo.sc")])
    assert _outputs_key(ADDRESS) in str(excinfo.value)
