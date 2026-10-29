# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for ``CookJournal``, the record each cook leaves in its debug directory.

Every test drives the journal through its public methods and reads back the files
it writes and the telemetry events it sends. The telemetry call is a recording
fake. Nothing in the module is patched.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from deadline.houdini_pdg import diagnostics
from deadline.houdini_pdg.diagnostics import CookJournal
from deadline.houdini_pdg.graph import TaskKey
from deadline.houdini_pdg.ports import CookPhase, CookSummary
from deadline.houdini_pdg.validation import RefusalScenario

START: datetime = datetime(2026, 10, 6, 13, 54, 30, tzinfo=timezone.utc)
COOK_ID: str = "ca9a0704111122223333444455556666"
PACKAGE_LOGGER: str = "deadline.houdini_pdg"
OUTCOME_EVENT: str = "com.amazon.rum.deadline.houdini_pdg.cook_outcome"
OUTCOMES: Set[str] = {"succeeded", "failed", "canceled", "refused", "setup_failed"}

# Cook IDs have the shape the cook creates: 32 lowercase hex characters.
hex_ids: st.SearchStrategy[str] = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)
utc_seconds: st.SearchStrategy[datetime] = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2099, 1, 1),
    timezones=st.just(timezone.utc),
).map(lambda d: d.replace(microsecond=0))


# -- fakes and builders -------------------------------------------------------


class Events:
    """A telemetry call that records what it is sent, and can raise after recording."""

    def __init__(self, raises: bool = False) -> None:
        self.sent: List[Tuple[str, Dict[str, Any]]] = []
        self.raises: bool = raises

    def __call__(self, event_type: str, details: Dict[str, Any]) -> None:
        self.sent.append((event_type, dict(details)))
        if self.raises:
            raise RuntimeError("telemetry is down")


def _journal(
    working_dir: Any,
    *,
    cook_id: str = COOK_ID,
    started_at: datetime = START,
    debug_level: Any = 0,
    events: Optional[Events] = None,
) -> CookJournal:
    """A journal with test defaults, sending its events to ``events`` when given."""
    return CookJournal(
        working_dir=str(working_dir),
        cook_id=cook_id,
        started_at=started_at,
        debug_level=debug_level,
        record_event=events if events is not None else Events(),
    )


def _expected_dir(working_dir: Any, started_at: datetime = START, cook_id: str = COOK_ID) -> Path:
    """The directory name the naming rule gives: UTC start to the second, then the ID's first 8."""
    stamp: str = started_at.astimezone(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    return Path(working_dir) / "pdg_debug" / f"{stamp}-{cook_id[:8]}"


def _record(message: str) -> logging.LogRecord:
    """A log record carrying ``message``, as any logger would hand to the journal's handler."""
    return logging.makeLogRecord({"msg": message, "levelno": logging.INFO, "levelname": "INFO"})


def _log_messages(directory: Path) -> List[str]:
    """The messages in ``orchestrator.log``, without their timestamps."""
    log: Path = directory / "orchestrator.log"
    if not log.exists():
        return []
    return [line.split("] ", 1)[1] for line in log.read_text(encoding="utf-8").splitlines()]


def _read_json(path: Path) -> Any:
    """The parsed content of a JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def package_log() -> Iterator[logging.Logger]:
    """The package logger at DEBUG, with any handler a test adds removed afterwards."""
    # Remember the logger's level and handlers, so the test's changes are undone afterwards.
    logger: logging.Logger = logging.getLogger(PACKAGE_LOGGER)
    level: int = logger.level
    logger.setLevel(logging.DEBUG)
    before: List[logging.Handler] = list(logger.handlers)
    yield logger
    for handler in list(logger.handlers):
        if handler not in before:
            logger.removeHandler(handler)
    logger.setLevel(level)


def _summary(**kwargs: Any) -> CookSummary:
    """A summary of a finished cook, overridden by ``kwargs``."""
    values: Dict[str, Any] = {"cook_id": COOK_ID, "final_phase": CookPhase.DONE}
    values.update(kwargs)
    return CookSummary(**values)


# -- the directory ------------------------------------------------------------


def test_nothing_is_written_until_the_first_write(tmp_path: Path) -> None:
    """A pass that only logs and closes before any work leaves no directory, so PDG's
    preliminary pass does not litter the working directory."""
    journal: CookJournal = _journal(tmp_path)
    # Log a line, then end the pass where PDG's preliminary pass ends.
    journal.log_handler().handle(_record("buffered"))
    journal.closed(_summary(final_phase=CookPhase.CONNECTED))
    assert not (tmp_path / "pdg_debug").exists()


def test_the_directory_is_named_by_utc_start_and_cook_id_prefix(tmp_path: Path) -> None:
    """A start time in a local zone is named in UTC, so names sort the same on every workstation."""
    local: datetime = START.astimezone(timezone(timedelta(hours=-7)))
    _journal(tmp_path, started_at=local).job_submitted("job-1")
    assert [p.name for p in (tmp_path / "pdg_debug").iterdir()] == ["2026-10-06-13-54-30-ca9a0704"]


def test_a_naive_start_time_is_read_as_utc(tmp_path: Path) -> None:
    """A start time with no zone is taken as UTC rather than the workstation's zone."""
    _journal(tmp_path, started_at=START.replace(tzinfo=None)).job_submitted("job-1")
    assert _expected_dir(tmp_path).is_dir()


@settings(max_examples=40, deadline=None)
@given(cook_id=hex_ids, when=utc_seconds)
def test_the_path_follows_the_naming_model_and_is_stable(cook_id: str, when: datetime) -> None:
    """For any cook ID and start, the bundle lands in the named directory, which exists, and a
    second call returns the same path rather than a new directory."""
    with tempfile.TemporaryDirectory() as wd:
        journal: CookJournal = _journal(wd, cook_id=cook_id, started_at=when)
        # The first call makes the directory; the second must find the same one.
        bundle: str = journal.bundle_dir()
        assert Path(bundle) == _expected_dir(wd, when, cook_id) / "bundle"
        assert Path(bundle).is_dir()
        assert journal.bundle_dir() == bundle
        assert len(list((Path(wd) / "pdg_debug").iterdir())) == 1


@settings(max_examples=25, deadline=None)
@given(
    st.lists(
        st.tuples(
            utc_seconds,
            # Two prefixes only, so cooks that share a second also share a prefix.
            st.tuples(st.sampled_from(["abcdef01", "01234567"]), hex_ids).map(
                lambda t: t[0] + t[1][8:]
            ),
        ),
        min_size=1,
        max_size=6,
        unique=True,
    )
)
def test_names_sort_by_time_and_distinct_cooks_never_share_a_directory(
    pairs: List[Tuple[datetime, str]],
) -> None:
    """For any set of cooks, each gets its own directory, and sorting the names sorts the
    cooks by start time, so a plain listing is chronological."""
    with tempfile.TemporaryDirectory() as wd:
        # Make each cook's directory and keep its name, then compare names with start times.
        made: Dict[Tuple[datetime, str], str] = {}
        for when, cook_id in pairs:
            bundle: str = _journal(wd, cook_id=cook_id, started_at=when).bundle_dir()
            made[(when, cook_id)] = os.path.basename(os.path.dirname(bundle))
        assert len(set(made.values())) == len(made)
        by_name: List[Tuple[datetime, str]] = sorted(made, key=lambda k: made[k])
        assert [k[0] for k in by_name] == sorted(k[0] for k in by_name)


def test_a_same_second_collision_on_the_same_prefix_gets_its_own_directory(tmp_path: Path) -> None:
    """Two cooks that start in the same second with the same ID prefix never overwrite each
    other's ``cook.json``."""
    first: CookJournal = _journal(tmp_path, cook_id="abcdefgh" + "1" * 24)
    second: CookJournal = _journal(tmp_path, cook_id="abcdefgh" + "2" * 24)
    # Both write before either reads back, as two cooks started together would.
    first.job_submitted("job-1")
    second.job_submitted("job-2")
    names: List[str] = sorted(p.name for p in (tmp_path / "pdg_debug").iterdir())
    assert names == ["2026-10-06-13-54-30-abcdefgh", "2026-10-06-13-54-30-abcdefgh-2"]
    root: Path = tmp_path / "pdg_debug"
    assert _read_json(root / names[0] / "cook.json")["job_id"] == "job-1"
    assert _read_json(root / names[1] / "cook.json")["job_id"] == "job-2"


def test_a_name_is_found_up_to_the_thousandth_suffix_and_no_further(tmp_path: Path) -> None:
    """With the plain name and suffixes 2 to 999 taken, a cook gets suffix 1000. When that
    is taken too, the next cook writes nothing rather than reuse a directory."""
    root: Path = tmp_path / "pdg_debug"
    base: str = _expected_dir(tmp_path).name
    # Take the plain name and every suffix up to 999.
    root.mkdir()
    (root / base).mkdir()
    for n in range(2, 1000):
        (root / f"{base}-{n}").mkdir()

    # The next cook takes the last free suffix.
    _journal(tmp_path).job_submitted("job-1000")
    assert _read_json(root / f"{base}-1000" / "cook.json")["job_id"] == "job-1000"

    # With every suffix taken, a later cook makes no directory and its bundle goes elsewhere.
    late: CookJournal = _journal(tmp_path)
    late.job_submitted("job-late")
    assert not (root / f"{base}-1001").exists()
    fallback: str = late.bundle_dir()
    shutil.rmtree(fallback)
    assert not fallback.startswith(str(tmp_path))


def test_bundle_dir_falls_back_to_a_temporary_directory_when_the_working_dir_is_unusable(
    tmp_path: Path,
) -> None:
    """When the debug directory cannot be made, the bundle still gets an existing directory
    outside the working directory, so a diagnostics problem never fails the submission."""
    blocker: Path = tmp_path / "file"
    # A file where the working directory should be, so nothing can be made under it.
    blocker.write_text("not a directory")
    path: str = _journal(blocker).bundle_dir()
    assert os.path.isdir(path)
    shutil.rmtree(path)
    assert not path.startswith(str(blocker))


def test_an_empty_working_dir_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no working directory set, nothing is written relative to the process's current
    directory."""
    # Run from an empty directory, so anything written relative to it would show.
    monkeypatch.chdir(tmp_path)
    journal: CookJournal = _journal("")
    journal.job_submitted("job-1")
    journal.closed(_summary(final_phase=CookPhase.FAILED))
    assert list(tmp_path.iterdir()) == []


def test_a_directory_that_cannot_be_made_is_warned_about_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every write after the first fails the same way, so the artist sees one warning, not one
    per call."""
    blocker: Path = tmp_path / "file"
    blocker.write_text("")
    # Call every method that needs the directory, then count the warnings.
    journal: CookJournal = _journal(blocker)
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        journal.job_submitted("job-1")
        journal.bundle_dir()
        journal.job_submitted("job-2")
        journal.closed(_summary(final_phase=CookPhase.FAILED))
    warnings: List[str] = [
        r.getMessage()
        for r in caplog.records
        if "Could not create this cook's debug" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "console only" in warnings[0]


# -- the log ------------------------------------------------------------------


def test_lines_logged_before_and_after_the_directory_exists_land_in_order(
    tmp_path: Path, package_log: logging.Logger
) -> None:
    """Lines logged through the package logger before the first write are flushed first, and
    later lines follow them."""
    journal: CookJournal = _journal(tmp_path)
    # Two lines before the first write, and one after.
    package_log.addHandler(journal.log_handler())
    package_log.info("one")
    package_log.info("two")
    journal.job_submitted("job-1")
    package_log.info("three")
    messages: List[str] = _log_messages(_expected_dir(tmp_path))
    assert [m for m in messages if m in ("one", "two", "three")] == ["one", "two", "three"]


# Any single-line text, with lone surrogates mixed in: Python decodes a file name that is
# not valid UTF-8 into them, and a log line naming such a file must still be written.
single_lines: st.SearchStrategy[str] = st.lists(
    st.one_of(
        st.characters(exclude_categories=("Cc", "Zl", "Zp")),
        st.sampled_from(["\ud800", "\udce9", "\udfff"]),
    ),
    min_size=1,
    max_size=8,
).map("".join)


def _as_written(message: str) -> str:
    """``message`` as a UTF-8 file holds it, with each character UTF-8 cannot encode escaped."""
    return message.encode("utf-8", "backslashreplace").decode("utf-8")


@settings(max_examples=30, deadline=None)
@given(
    before=st.lists(single_lines, max_size=8),
    after=st.lists(single_lines, max_size=8),
)
def test_every_line_reaches_the_log_in_emission_order(before: List[str], after: List[str]) -> None:
    """For any lines logged before and after the directory exists, ``orchestrator.log`` holds
    all of them in the order they were logged, so a line naming an odd file is never lost."""
    with tempfile.TemporaryDirectory() as wd:
        journal: CookJournal = _journal(wd)
        # Log the first lines, make the directory, then log the rest.
        handler: logging.Handler = journal.log_handler()
        for i, msg in enumerate(before):
            handler.handle(_record(f"b{i} {msg}"))
        journal.job_submitted("job-1")
        for i, msg in enumerate(after):
            handler.handle(_record(f"a{i} {msg}"))
        # Keep only this test's lines, not the journal's own.
        ours: List[str] = [
            m for m in _log_messages(_expected_dir(wd)) if m[:1] in ("a", "b") and m[1:2].isdigit()
        ]
        assert ours == [_as_written(f"b{i} {m}") for i, m in enumerate(before)] + [
            _as_written(f"a{i} {m}") for i, m in enumerate(after)
        ]


def test_a_line_naming_a_file_that_is_not_valid_utf8_is_written_with_the_byte_escaped(
    tmp_path: Path,
) -> None:
    """A line naming a file whose name is not valid UTF-8 is written, before and after the
    directory exists, with the bad byte escaped. Without the escape, the whole line is lost."""
    journal: CookJournal = _journal(tmp_path)
    handler: logging.Handler = journal.log_handler()
    # The name Python gives a file called ``caf<0xE9>`` on a UTF-8 file system:
    # ``os.fsdecode(b"/proj/caf\xe9/geo.bgeo")`` on Linux and macOS.
    path: str = "/proj/caf\udce9/geo.bgeo"
    handler.handle(_record(f"before {path}"))
    journal.job_submitted("job-1")
    handler.handle(_record(f"after {path}"))
    messages: List[str] = _log_messages(_expected_dir(tmp_path))
    escaped: str = path.replace("\udce9", "\\udce9")
    assert [m for m in messages if m.startswith(("before ", "after "))] == [
        f"before {escaped}",
        f"after {escaped}",
    ]


def test_each_line_starts_with_an_iso8601_utc_timestamp(tmp_path: Path) -> None:
    """Each line reads ``[<ISO-8601 UTC>] message``, so lines from any workstation compare."""
    journal: CookJournal = _journal(tmp_path)
    journal.job_submitted("job-1")
    journal.log_handler().handle(_record("hello"))
    # Split the last line into its stamp and its message.
    log: Path = _expected_dir(tmp_path) / "orchestrator.log"
    last: str = log.read_text(encoding="utf-8").splitlines()[-1]
    assert last.startswith("[")
    stamp: str
    message: str
    stamp, message = last[1:].split("] ", 1)
    assert message == "hello"
    assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0)


def test_a_line_logged_before_the_directory_exists_keeps_the_time_it_was_logged(
    tmp_path: Path,
) -> None:
    """A line held in memory is stamped with the time it was logged, not the time the directory
    was made, so the log shows how long the cook waited before its first write."""
    journal: CookJournal = _journal(tmp_path)
    # A record logged an hour before the directory exists.
    logged_at: datetime = datetime.now(timezone.utc) - timedelta(hours=1)
    record: logging.LogRecord = _record("early")
    record.created = logged_at.timestamp()
    journal.log_handler().handle(record)
    journal.job_submitted("job-1")
    # Find the line and read its stamp back.
    log: Path = _expected_dir(tmp_path) / "orchestrator.log"
    line: str = next(
        x for x in log.read_text(encoding="utf-8").splitlines() if x.endswith("] early")
    )
    stamp: datetime = datetime.fromisoformat(line[1:].split("] ", 1)[0])
    assert abs(stamp - logged_at) < timedelta(milliseconds=1)


@pytest.mark.skipif(sys.platform == "win32", reason="replaces a file with a directory")
@settings(max_examples=20, deadline=None)
@given(st.text())
def test_the_handler_never_raises_when_the_log_cannot_be_written(message: str) -> None:
    """For any message, logging does not raise when ``orchestrator.log`` cannot be written,
    or when a record's arguments do not match its format, because logging must never fail
    the cook."""
    with tempfile.TemporaryDirectory() as wd:
        journal: CookJournal = _journal(wd)
        journal.job_submitted("job-1")
        journal.log_handler().handle(_record("first"))
        log: Path = _expected_dir(wd) / "orchestrator.log"
        # Put a directory where the log file is, so every later write fails.
        log.unlink()
        log.mkdir()
        journal.log_handler().handle(_record(message))
        journal.log_handler().handle(logging.makeLogRecord({"msg": "%s %s", "args": ("one",)}))


def test_the_handler_never_raises_without_a_directory(tmp_path: Path) -> None:
    """When the directory cannot be made, lines are still accepted and nothing is written."""
    blocker: Path = tmp_path / "file"
    blocker.write_text("")
    journal: CookJournal = _journal(blocker)
    journal.job_submitted("job-1")
    # A line logged after the directory could not be made has nowhere to go but memory.
    journal.log_handler().handle(_record("still fine"))
    assert blocker.read_text() == ""


def test_a_long_wait_keeps_the_newest_ten_thousand_lines_and_says_how_many_were_dropped(
    tmp_path: Path,
) -> None:
    """Before the directory exists, only the newest 10,000 lines are held, so a pass that never
    writes cannot hold memory for the whole session. The log then opens by saying how many
    lines were dropped."""
    journal: CookJournal = _journal(tmp_path)
    # One line more than the limit arrives before the directory exists.
    for i in range(10_001):
        journal.log_handler().handle(_record(f"line {i}"))
    journal.job_submitted("job-1")
    messages: List[str] = _log_messages(_expected_dir(tmp_path))
    assert messages[0] == "1 earlier line(s) were dropped before this log existed."
    assert messages[1:] == [f"line {i}" for i in range(1, 10_001)]


def test_ten_thousand_lines_are_all_kept(tmp_path: Path) -> None:
    """Exactly 10,000 lines before the directory exists fit, so none is dropped and the log
    carries no dropped-lines note."""
    journal: CookJournal = _journal(tmp_path)
    # Exactly the limit arrives before the directory exists.
    for i in range(10_000):
        journal.log_handler().handle(_record(f"line {i}"))
    journal.job_submitted("job-1")
    messages: List[str] = _log_messages(_expected_dir(tmp_path))
    assert messages[:10_000] == [f"line {i}" for i in range(10_000)]
    assert not any("dropped" in m for m in messages)


# -- cook.json ----------------------------------------------------------------


@settings(max_examples=30, deadline=None)
@given(
    job_ids=st.lists(st.text(alphabet="job-0123", min_size=1, max_size=6), min_size=1, max_size=5),
    close=st.booleans(),
)
def test_cook_json_holds_the_last_value_written_for_each_key(
    job_ids: List[str], close: bool
) -> None:
    """For any sequence of journal calls, ``cook.json`` keeps the keys of earlier calls, holds
    the last value written for each key, and has an ``updated_at`` in UTC."""
    with tempfile.TemporaryDirectory() as wd:
        journal: CookJournal = _journal(wd, debug_level=1)
        # Several writes of the same key, then maybe the close, which writes it last.
        for job_id in job_ids:
            journal.job_submitted(job_id)
        if close:
            journal.closed(_summary(job_id="final", task_count=2, succeeded=2))
        meta: Dict[str, Any] = _read_json(_expected_dir(wd) / "cook.json")
        assert meta["cook_id"] == COOK_ID
        assert meta["debug_level"] == 1
        assert meta["job_id"] == ("final" if close else job_ids[-1])
        assert ("outcome" in meta) is close
        assert datetime.fromisoformat(meta["updated_at"]).utcoffset() == timedelta(0)


@settings(max_examples=40, deadline=None)
@given(
    existing=st.one_of(
        st.binary(max_size=40),
        st.recursive(
            st.none() | st.booleans() | st.integers() | st.text(max_size=4),
            lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=3), c, max_size=3),
            max_leaves=6,
        ).map(lambda v: json.dumps(v).encode()),
    ),
)
def test_any_existing_cook_json_is_merged_over_without_raising(existing: bytes) -> None:
    """For any bytes or JSON value found in ``cook.json``, the next write does not raise, and
    the file afterwards is an object holding that write's keys."""
    with tempfile.TemporaryDirectory() as wd:
        journal: CookJournal = _journal(wd)
        journal.job_submitted("job-1")
        # Replace the file's content between two writes.
        path: Path = _expected_dir(wd) / "cook.json"
        path.write_bytes(existing)
        journal.job_submitted("job-2")
        result: Any = _read_json(path)
        assert isinstance(result, dict)
        assert result["job_id"] == "job-2"


def test_cook_json_records_the_cook_and_the_job(tmp_path: Path) -> None:
    """``cook.json`` names the cook, its start, its debug level, and its job."""
    # Level 2 is recorded as given, although it writes what level 1 writes.
    _journal(tmp_path, debug_level=2).job_submitted("job-123")
    meta: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "cook.json")
    assert meta["cook_id"] == COOK_ID
    assert meta["job_id"] == "job-123"
    assert meta["debug_level"] == 2
    assert datetime.fromisoformat(meta["started_at"]) == START


def test_closing_records_the_final_counts(tmp_path: Path) -> None:
    """Teardown adds the phase, the outcome, the refusals, and the counts to ``cook.json``."""
    journal: CookJournal = _journal(tmp_path)
    # A cook with one failed task out of four, closed after its job was recorded.
    journal.job_submitted("job-1")
    journal.closed(
        _summary(job_id="job-1", step_count=2, task_count=4, succeeded=3, failed=1, canceled=0)
    )
    meta: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "cook.json")
    assert meta["final_phase"] == "Done"
    assert meta["outcome"] == "failed"
    assert meta["refusals"] == []
    assert (meta["step_count"], meta["task_count"]) == (2, 4)
    assert (meta["succeeded"], meta["failed"], meta["canceled"]) == (3, 1, 0)
    assert meta["stopped_by_user"] is False
    assert "cached" not in meta


# -- failures -----------------------------------------------------------------


def test_log_tails_are_fetched_for_at_most_three_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Each tail costs API calls and failures usually share a cause, so only the first three
    failures read a tail. The limit is announced once, with the third."""
    calls: List[int] = []

    def tail(i: int) -> Any:
        """A tail reader that records which failure called it."""

        def read() -> List[str]:
            calls.append(i)
            return [f"line from {i}", "second line"]

        return read

    # Five failures; only the first three may read a tail.
    journal: CookJournal = _journal(tmp_path)
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        for i in range(5):
            journal.item_failed(TaskKey("render", i), tail(i))
    messages: List[str] = [r.getMessage() for r in caplog.records]
    assert calls == [0, 1, 2]
    assert messages[:3] == [
        "Last 2 line(s) of the worker log for task render[0]:",
        "    | line from 0",
        "    | second line",
    ]
    # The limit notice comes once, right after the third tail.
    limit: List[int] = [i for i, m in enumerate(messages) if "per-cook limit" in m]
    assert len(limit) == 1
    assert messages[limit[0] - 1] == "    | second line"
    assert messages[limit[0] - 3] == "Last 2 line(s) of the worker log for task render[2]:"
    assert "3 is the per-cook limit" in messages[limit[0]]


def test_an_unreadable_or_empty_tail_is_said_out_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A tail that cannot be read, and one that is empty, each get a warning naming the task,
    so the artist knows why no worker lines follow."""

    def raises() -> List[str]:
        raise RuntimeError("AccessDenied")

    # One tail read that raises and one that finds nothing.
    journal: CookJournal = _journal(tmp_path)
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        journal.item_failed(TaskKey("render", 0), raises)
        journal.item_failed(TaskKey("render", 1), lambda: [])
    messages: List[str] = [r.getMessage() for r in caplog.records]
    assert messages[0] == "Could not read the worker log for task render[0]: AccessDenied"
    assert messages[1].startswith("No worker log is available for task render[1].")
    assert len(messages) == 2


def test_a_failure_with_a_key_that_is_not_a_task_key_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure report the journal cannot read never fails the cook, and the debug log keeps
    the reason, since nothing else says why no worker lines were logged."""
    journal: CookJournal = _journal(tmp_path)
    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        journal.item_failed(None, lambda: ["x"])  # type: ignore[arg-type]
    assert any(
        r.levelno == logging.DEBUG
        and r.exc_info
        and r.getMessage() == "Could not log the worker's last lines"
        for r in caplog.records
    )


# -- the outcome --------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, outcome",
    [
        ({"job_id": "j", "task_count": 3, "succeeded": 3}, "succeeded"),
        ({"job_id": "j", "task_count": 1, "succeeded": 1}, "succeeded"),
        ({"job_id": "j", "task_count": 3, "succeeded": 2, "failed": 1}, "failed"),
        # A failure outranks the cancels it caused, and a stop that came after it.
        ({"job_id": "j", "task_count": 3, "failed": 1, "canceled": 2}, "failed"),
        ({"job_id": "j", "task_count": 3, "failed": 1, "stopped_by_user": True}, "failed"),
        ({"job_id": "j", "task_count": 3, "succeeded": 1, "canceled": 2}, "canceled"),
        ({"job_id": "j", "task_count": 3, "succeeded": 2, "canceled": 1}, "canceled"),
        ({"job_id": "j", "task_count": 3, "stopped_by_user": True}, "canceled"),
        # A job that ended with tasks unfinished did not succeed.
        ({"job_id": "j", "task_count": 3, "succeeded": 1}, "failed"),
        ({"job_id": "j", "final_phase": CookPhase.DONE}, "failed"),
        ({"refusals": (RefusalScenario.DYNAMIC_NODE,), "failed": 1}, "refused"),
        ({"final_phase": CookPhase.FAILED}, "setup_failed"),
        ({"final_phase": CookPhase.SUBMITTING, "task_count": 3}, "setup_failed"),
        ({"final_phase": CookPhase.DONE, "task_count": 3}, "setup_failed"),
        # A static cook needs no farm: no job, no tasks, and it reached DONE.
        ({"final_phase": CookPhase.DONE}, "succeeded"),
    ],
)
def test_the_outcome_class_follows_the_counts(
    tmp_path: Path, fields: Dict[str, Any], outcome: str
) -> None:
    """The outcome is the first class that applies: refused, failed, canceled, succeeded, then
    failed for a job, then the static-cook rule."""
    events: Events = Events()
    _journal(tmp_path, events=events).closed(_summary(**fields))
    assert [(t, d["outcome"]) for t, d in events.sent] == [(OUTCOME_EVENT, outcome)]


def test_the_event_carries_the_counts(tmp_path: Path) -> None:
    """The event carries every count, whether a job was submitted, and the refusals."""
    events: Events = Events()
    _journal(tmp_path, events=events).closed(
        _summary(
            job_id="job-1",
            step_count=2,
            task_count=5,
            succeeded=3,
            failed=1,
            canceled=1,
            stopped_by_user=True,
        )
    )
    assert events.sent[0][1] == {
        "outcome": "failed",
        "refusal_scenarios": "",
        "refusal_count": 0,
        "job_submitted": True,
        "step_count": 2,
        "task_count": 5,
        "succeeded": 3,
        "failed": 1,
        "canceled": 1,
        "stopped_by_user": True,
    }


def test_a_refusal_event_names_its_scenarios(tmp_path: Path) -> None:
    """A refused cook's event lists each scenario once, sorted, and counts every refusal."""
    events: Events = Events()
    # One scenario twice and another once, out of order.
    refusals: Tuple[RefusalScenario, ...] = (
        RefusalScenario.ROP_BATCH,
        RefusalScenario.DYNAMIC_NODE,
        RefusalScenario.ROP_BATCH,
    )
    _journal(tmp_path, events=events).closed(
        _summary(final_phase=CookPhase.REFUSED, refusals=refusals)
    )
    details: Dict[str, Any] = events.sent[0][1]
    assert details["outcome"] == "refused"
    assert details["refusal_scenarios"] == "dynamic_node,rop_batch"
    assert details["refusal_count"] == 3
    assert details["job_submitted"] is False


summaries: st.SearchStrategy[CookSummary] = st.builds(
    CookSummary,
    cook_id=st.just(COOK_ID),
    final_phase=st.sampled_from(list(CookPhase)),
    job_id=st.sampled_from(["", "job-0123456789abcdef"]),
    step_count=st.integers(0, 5),
    task_count=st.integers(0, 50),
    succeeded=st.integers(0, 50),
    failed=st.integers(0, 5),
    canceled=st.integers(0, 5),
    refusals=st.lists(st.sampled_from(list(RefusalScenario)), max_size=3).map(tuple),
    stopped_by_user=st.booleans(),
)


def _ran_anything(s: CookSummary) -> bool:
    """Whether the cook did anything an abandoned pass never does: a job, a refusal, a stop, a
    task, or a count."""
    return bool(
        s.job_id
        or s.refusals
        or s.stopped_by_user
        or s.step_count
        or s.task_count
        or s.succeeded
        or s.failed
        or s.canceled
    )


@settings(max_examples=80, deadline=None)
@given(summaries, st.integers(min_value=1, max_value=3))
def test_a_cook_sends_exactly_one_event_carrying_only_counts(s: CookSummary, closes: int) -> None:
    """For any summary, closing once or more sends one event, unless the pass was abandoned
    before any work, which sends none. No value names a path, the scene, or the cook."""
    events: Events = Events()
    with tempfile.TemporaryDirectory() as wd:
        journal: CookJournal = _journal(os.path.join(wd, "scene_name"), events=events)
        for _ in range(closes):
            journal.closed(s)
    # An abandoned pass ends IDLE or CONNECTED having done nothing else.
    abandoned: bool = s.final_phase in (CookPhase.IDLE, CookPhase.CONNECTED) and not _ran_anything(
        s
    )
    assert len(events.sent) == (0 if abandoned else 1)
    # Every value is a count, a flag, or a fixed word.
    for _, details in events.sent:
        assert details["outcome"] in OUTCOMES
        for value in details.values():
            assert isinstance(value, (int, bool, str))
            if isinstance(value, str):
                assert os.sep not in value and "/" not in value
                assert "scene_name" not in value and COOK_ID not in value and "job-" not in value


@settings(max_examples=80, deadline=None)
@given(summaries, st.sampled_from(list(CookPhase)))
def test_the_phase_decides_only_for_a_cook_with_no_job_and_no_tasks(
    s: CookSummary, other_phase: CookPhase
) -> None:
    """For any summary with a job, a task, a refusal, a stop, a failure, or a cancel, the
    outcome is the same whatever the final phase, so the class comes from the counts. Only a
    cook with none of those, such as a static cook, needs the phase."""
    decided_by_counts: bool = bool(
        s.job_id or s.task_count or s.refusals or s.stopped_by_user or s.failed or s.canceled
    )
    if not decided_by_counts:
        return
    # Close the same summary with two final phases and compare the outcomes.
    outcomes: List[str] = []
    for phase in (s.final_phase, other_phase):
        events: Events = Events()
        with tempfile.TemporaryDirectory() as wd:
            _journal(wd, events=events).closed(replace(s, final_phase=phase))
        outcomes.append(events.sent[0][1]["outcome"])
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("phase", [CookPhase.IDLE, CookPhase.CONNECTED])
def test_an_abandoned_pass_leaves_no_directory_and_sends_no_event(
    tmp_path: Path, phase: CookPhase
) -> None:
    """A pass PDG drops before offering work is not a cook the artist ran, so it writes nothing
    and is not counted."""
    events: Events = Events()
    journal: CookJournal = _journal(tmp_path, events=events)
    # A pass that logs, then ends where PDG's preliminary pass ends.
    journal.log_handler().handle(_record("Connected"))
    journal.closed(_summary(final_phase=phase))
    assert events.sent == []
    assert not (tmp_path / "pdg_debug").exists()


@pytest.mark.parametrize(
    "fields",
    [
        {"job_id": "j"},
        {"refusals": (RefusalScenario.DYNAMIC_NODE,)},
        {"stopped_by_user": True},
        {"step_count": 1},
        {"task_count": 1},
        {"succeeded": 1},
        {"failed": 1},
        {"canceled": 1},
    ],
)
def test_a_connected_pass_that_did_anything_is_counted(
    tmp_path: Path, fields: Dict[str, Any]
) -> None:
    """A pass that ended CONNECTED but has a job, a refusal, a stop, a task, or a count is a
    real cook, so it sends its event."""
    events: Events = Events()
    _journal(tmp_path, events=events).closed(_summary(final_phase=CookPhase.CONNECTED, **fields))
    assert len(events.sent) == 1


def test_an_abandoned_pass_with_a_directory_is_labeled_abandoned(tmp_path: Path) -> None:
    """When an abandoned pass already made its directory, ``cook.json`` says it was abandoned
    rather than giving it an outcome class, and no event is sent."""
    events: Events = Events()
    journal: CookJournal = _journal(tmp_path, events=events)
    # Making the bundle directory creates the cook directory before the pass is dropped.
    journal.bundle_dir()
    journal.closed(_summary(final_phase=CookPhase.CONNECTED))
    assert _read_json(_expected_dir(tmp_path) / "cook.json")["outcome"] == "abandoned"
    assert events.sent == []


def test_a_raising_telemetry_call_does_not_raise(tmp_path: Path) -> None:
    """A telemetry client that raises does not reach the cook, and a second close sends
    nothing more."""
    events: Events = Events(raises=True)
    journal: CookJournal = _journal(tmp_path, events=events)
    # Close twice; the second close must not send again.
    journal.closed(_summary(final_phase=CookPhase.FAILED))
    journal.closed(_summary(final_phase=CookPhase.FAILED))
    assert len(events.sent) == 1


@pytest.mark.parametrize(
    "not_a_summary, reason",
    [
        (None, "Could not read the cook summary"),
        (object(), "Could not read the cook summary"),
        ("done", "Could not read the cook summary"),
        # Reads as a finished cook, then has none of the counts.
        (SimpleNamespace(final_phase=CookPhase.DONE), "Could not write the final counts"),
    ],
)
def test_closing_with_something_that_is_not_a_summary_does_not_raise(
    tmp_path: Path, not_a_summary: Any, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Teardown never fails the cook, even when the caller hands it the wrong object. Nothing
    is sent for a cook whose outcome cannot be read, and the debug log keeps the reason."""
    events: Events = Events()
    journal: CookJournal = _journal(tmp_path, events=events)
    journal.job_submitted("job-1")
    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        journal.closed(not_a_summary)
    assert events.sent == []
    # The swallowed error is the only trace of why the cook has no outcome.
    assert any(
        r.levelno == logging.DEBUG and r.exc_info and r.getMessage() == reason
        for r in caplog.records
    )


def test_a_failed_setup_keeps_its_log(tmp_path: Path) -> None:
    """A cook that fails before writing anything still gets a directory, because its log is
    the only thing that explains the failure."""
    journal: CookJournal = _journal(tmp_path)
    # A cook that logs its failure and closes without having written anything.
    journal.log_handler().handle(_record("COOK SETUP FAILED: no farm"))
    journal.closed(_summary(final_phase=CookPhase.FAILED))
    directory: Path = _expected_dir(tmp_path)
    assert "COOK SETUP FAILED: no farm" in _log_messages(directory)
    meta: Dict[str, Any] = _read_json(directory / "cook.json")
    assert meta["outcome"] == "setup_failed"
    assert meta["final_phase"] == "Failed"


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    abandoned=st.integers(min_value=1, max_value=3),
    wrote_bundle=st.booleans(),
    submitted=st.booleans(),
    lines=st.lists(st.text(alphabet="abc ", min_size=1, max_size=6), max_size=5),
)
def test_one_press_with_abandoned_passes_leaves_one_directory(
    abandoned: int, wrote_bundle: bool, submitted: bool, lines: List[str]
) -> None:
    """For any press of abandoned passes followed by one real pass, exactly one directory is
    left. It holds every line the real pass logged, and ``bundle/`` exactly when the real pass
    wrote a bundle, even if the submission then failed."""
    with tempfile.TemporaryDirectory() as wd:
        # Each abandoned pass has its own cook ID, logs, and ends before offering work.
        for i in range(abandoned):
            early: CookJournal = _journal(wd, cook_id=f"{i:08d}" + "e" * 24)
            early.log_handler().handle(_record("abandoned pass"))
            early.closed(_summary(final_phase=CookPhase.CONNECTED))

        # The real pass logs, maybe writes a bundle, then either submits or fails.
        real: CookJournal = _journal(wd)
        for line in lines:
            real.log_handler().handle(_record(f"real {line}"))
        if wrote_bundle:
            real.bundle_dir()
        if submitted:
            real.job_submitted("job-1")
        real.closed(
            _summary(job_id="job-1", task_count=1, succeeded=1)
            if submitted
            else _summary(final_phase=CookPhase.FAILED)
        )

        # One directory, holding the real pass's lines only.
        only: List[Path] = list((Path(wd) / "pdg_debug").iterdir())
        assert len(only) == 1
        messages: List[str] = _log_messages(only[0])
        assert [m for m in messages if m.startswith("real ")] == [f"real {line}" for line in lines]
        assert "abandoned pass" not in messages
        assert (only[0] / "bundle").is_dir() is wrote_bundle


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_an_unwritable_debug_directory_never_fails_a_call(tmp_path: Path) -> None:
    """Once the directory exists but cannot be written, every method still returns normally,
    because a diagnostics failure must not fail the cook."""
    events: Events = Events()
    journal: CookJournal = _journal(tmp_path, events=events)
    journal.job_submitted("job-1")
    journal.log_handler().handle(_record("before"))
    directory: Path = _expected_dir(tmp_path)
    # Make the directory read-only, then call each method that writes or logs.
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        # None of these can create a file, and none may raise.
        journal.item_failed(TaskKey("a", 0), lambda: ["x"])
        journal.log_handler().handle(_record("after"))
        journal.job_submitted("job-2")
        journal.closed(_summary(final_phase=CookPhase.FAILED))
    finally:
        directory.chmod(stat.S_IRWXU)
    # The event still went out, and no new file appeared.
    assert len(events.sent) == 1
    assert sorted(p.name for p in directory.iterdir()) == ["cook.json", "orchestrator.log"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_a_swallowed_error_leaves_its_traceback_at_debug_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The journal never raises, so the traceback in its debug log is the only way to find
    out why a write or the telemetry call failed."""
    events: Events = Events(raises=True)
    journal: CookJournal = _journal(tmp_path, events=events)
    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        # A file named ``bundle`` blocks the bundle directory, so it falls back elsewhere.
        journal.job_submitted("job-1")
        directory: Path = _expected_dir(tmp_path)
        (directory / "bundle").write_text("")
        fallback: str = journal.bundle_dir()
        shutil.rmtree(fallback)

        # A read-only directory fails the cook.json merge, and the telemetry call raises.
        directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            journal.closed(_summary(job_id="job-1", task_count=1, succeeded=1))
        finally:
            directory.chmod(stat.S_IRWXU)

    with_traceback: Set[str] = {
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and r.exc_info
    }
    assert with_traceback >= {
        "Could not create the bundle directory",
        "Could not write cook.json",
        "Could not record the cook outcome event",
    }


def test_a_debug_level_that_is_not_a_number_reads_as_zero(tmp_path: Path) -> None:
    """A debug level that cannot be read as a number falls back to the default level instead
    of failing the cook."""
    journal: CookJournal = _journal(tmp_path, debug_level="high")
    journal.job_submitted("job-1")
    assert _read_json(_expected_dir(tmp_path) / "cook.json")["debug_level"] == 0


# -- layering -----------------------------------------------------------------


@pytest.mark.skipif(sys.version_info < (3, 10), reason="needs sys.stdlib_module_names")
def test_diagnostics_imports_only_the_standard_library_and_core_modules() -> None:
    """The journal imports no SDK, no ``hou``, and no ``pdg``, so the cook can hold it directly
    and the tests run without Houdini or AWS."""
    stdlib: Set[str] = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    core: Set[str] = {"graph", "ports", "scene_reads", "validation", "errors", "houdini.readers"}
    # Relative imports must name a core module, and absolute ones the standard library.
    tree: ast.AST = ast.parse(Path(diagnostics.__file__).read_text(encoding="utf-8"))
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.ImportFrom) and stmt.level:
            assert stmt.module in core
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names: List[str] = (
                [a.name for a in stmt.names] if isinstance(stmt, ast.Import) else [str(stmt.module)]
            )
            for name in names:
                assert name.split(".")[0] in stdlib
