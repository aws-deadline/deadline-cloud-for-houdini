# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for ``CookJournal``, the record each cook leaves in its debug directory.

Every test drives the journal through its public methods and reads back the files
it writes and the telemetry events it sends. The telemetry call is a recording
fake, and the work items PDG would offer are plain objects with the attributes
the work item reader looks for. Nothing in the module is patched.
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
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from deadline.houdini_pdg import diagnostics
from deadline.houdini_pdg.diagnostics import CookJournal
from deadline.houdini_pdg.graph import (
    DeclaredFile,
    GraphSnapshot,
    NodeInfo,
    RawItem,
    StepSpec,
    TaskKey,
    WorkItemSpec,
)
from deadline.houdini_pdg.ports import CookPhase, CookSummary, WorkItemHandle
from deadline.houdini_pdg.scene_reads import (
    AttributeVariableUse,
    ReadMatch,
    ReadReport,
    ReadVerdict,
    SceneRead,
)
from deadline.houdini_pdg.validation import RefusalScenario

START: datetime = datetime(2026, 10, 6, 13, 54, 30, tzinfo=timezone.utc)
COOK_ID: str = "ca9a0704111122223333444455556666"
PACKAGE_LOGGER: str = "deadline.houdini_pdg"
OUTCOME_EVENT: str = "com.amazon.rum.deadline.houdini_pdg.cook_outcome"
OUTCOMES: Set[str] = {"succeeded", "failed", "canceled", "refused", "setup_failed"}
# The files every cook that reaches a snapshot writes, whatever its debug level.
ALWAYS_ON: Set[str] = {"orchestrator.log", "cook.json", "snapshot.json", "scene_reads.json"}

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


def _spec(
    name: str,
    node: str,
    index: int,
    outputs: Tuple[str, ...] = (),
    inputs: Tuple[str, ...] = (),
    command: str = "c",
) -> WorkItemSpec:
    """A generated work item with the given declared files."""
    return WorkItemSpec(
        name=name,
        task_index=index,
        pdg_index=index,
        item_id=index + 100,
        node_name=node,
        declared_outputs=tuple(DeclaredFile(p, "file") for p in outputs),
        declared_inputs=tuple(DeclaredFile(p, "file") for p in inputs),
        command=command,
    )


def _step(name: str, n: int, deps: Sequence[str] = (), data: Sequence[str] = ()) -> StepSpec:
    """A step of ``n`` items, each declaring one output under ``/out/<step>/``."""
    items: Tuple[WorkItemSpec, ...] = tuple(
        _spec(f"{name}_{i}", name, i, outputs=(f"/out/{name}/{i}.bgeo",)) for i in range(n)
    )
    return StepSpec(name=name, dependencies=tuple(deps), items=items, data_dependencies=tuple(data))


def _diamond() -> GraphSnapshot:
    """Four steps where ``d`` depends on ``a`` directly and through ``b`` and ``c``."""
    return GraphSnapshot(
        steps=(
            _step("a", 2),
            _step("b", 1, deps=("a",)),
            _step("c", 3, deps=("a",)),
            _step("d", 2, deps=("a", "b", "c"), data=("c",)),
        ),
        skipped_nodes=("partition",),
        remapped_steps=("c",),
        refused_data_edges={"a": ("d",)},
    )


def _node(
    name: str,
    upstream: Tuple[str, ...] = (),
    items: int = 1,
    schedulable: Optional[bool] = True,
) -> NodeInfo:
    """A node whose items read one scene asset and depend on each upstream node's first item."""
    raw: Tuple[RawItem, ...] = tuple(
        RawItem(
            name=f"{name}_{i}",
            index=i,
            item_id=i,
            command="hython job.py" if schedulable else "",
            declared_outputs=(DeclaredFile(f"/out/{name}/{i}.bgeo", "file"),),
            declared_inputs=(DeclaredFile("/assets/tex.png", "file/image"),),
            depends_on=tuple(f"{u}_0" for u in upstream),
        )
        for i in range(items)
    )
    return NodeInfo(
        name=name,
        is_dynamic=False,
        top_type="ropfetch" if schedulable else "partitionbyframe",
        upstream=upstream,
        items=raw,
        schedulable=schedulable,
    )


def _diamond_nodes() -> List[NodeInfo]:
    """The nodes the diamond snapshot was made from, plus a partitioner that maps to no step."""
    return [
        _node("a", items=2),
        _node("b", ("a",)),
        _node("c", ("a",), items=3),
        _node("d", ("a", "b", "c"), items=2),
        _node("partition", ("d",), schedulable=False),
    ]


def _bare(name: str, commands: Sequence[str]) -> NodeInfo:
    """A node with no schedulable flag, whose items carry the given commands."""
    items: Tuple[RawItem, ...] = tuple(
        RawItem(name=f"{name}{i}", index=i, item_id=i, command=c) for i, c in enumerate(commands)
    )
    return NodeInfo(name=name, is_dynamic=False, top_type="t", upstream=(), items=items)


def _summary(**kwargs: Any) -> CookSummary:
    """A summary of a finished cook, overridden by ``kwargs``."""
    values: Dict[str, Any] = {"cook_id": COOK_ID, "final_phase": CookPhase.DONE}
    values.update(kwargs)
    return CookSummary(**values)


def _files(*paths: str) -> List[SimpleNamespace]:
    """File objects as PDG lists them on a work item."""
    return [SimpleNamespace(path=p, tag="file") for p in paths]


def _native(
    name: str,
    command: str = "c",
    inputs: Sequence[str] = (),
    outputs: Sequence[str] = (),
) -> SimpleNamespace:
    """A stand-in for PDG's live work item, with the attributes the work item reader reads."""
    return SimpleNamespace(
        id=1,
        index=0,
        name=name,
        command=command,
        inputFiles=_files(*inputs),
        expectedInputFiles=[],
        outputFiles=[],
        expectedOutputFiles=_files(*outputs),
    )


def _handle(name: str, node_name: str, index: int, native: Any = None) -> WorkItemHandle:
    """A work item as PDG offers it to the scheduler."""
    return WorkItemHandle(
        item_id=index,
        index=index,
        name=name,
        node_name=node_name,
        declared_outputs=(DeclaredFile(f"/out/{node_name}/{index}.bgeo", "file"),),
        native=native,
    )


def _scheduled(directory: Path) -> List[Dict[str, Any]]:
    """The records in ``scheduled_items.jsonl``."""
    path: Path = directory / "scheduled_items.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _graph_node(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    """One node's entry in ``graph.json``."""
    return next(n for n in data["nodes"] if n["name"] == name)


def _reach(deps: Dict[str, Set[str]], start: str) -> Set[str]:
    """Every name ``start`` depends on, directly or not, by a plain graph search."""
    # Depth-first over the edges, visiting each name once, so a cycle still ends.
    seen: Set[str] = set()
    stack: List[str] = list(deps.get(start, ()))
    while stack:
        name: str = stack.pop()
        if name not in seen:
            seen.add(name)
            stack.extend(deps.get(name, ()))
    return seen


@st.composite
def step_graphs(draw: Any) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Acyclic step dependencies, each step drawing from earlier steps, and a data subset of each."""
    # Each step draws its dependencies from earlier steps only, so the graph is acyclic.
    count: int = draw(st.integers(min_value=1, max_value=7))
    names: List[str] = [f"s{i}" for i in range(count)]
    deps: Dict[str, Set[str]] = {}
    data: Dict[str, Set[str]] = {}
    for i, name in enumerate(names):
        deps[name] = set(draw(st.sets(st.sampled_from(names[:i])))) if i else set()
        data[name] = {d for d in sorted(deps[name]) if draw(st.booleans())}
    return deps, data


def _snapshot_of(deps: Dict[str, Set[str]], data: Dict[str, Set[str]]) -> GraphSnapshot:
    """A snapshot with one item per step and the given dependencies, in the order drawn."""
    return GraphSnapshot(
        steps=tuple(_step(n, 1, sorted(deps[n]), sorted(data[n])) for n in deps),
    )


def _step_nodes(snapshot: GraphSnapshot) -> List[NodeInfo]:
    """One itemless node per step, named like it, for the graph dump."""
    return [
        NodeInfo(name=s.name, is_dynamic=False, top_type="t", upstream=s.dependencies, items=())
        for s in snapshot.steps
    ]


def _would_drop(snapshot: GraphSnapshot) -> Dict[str, Set[str]]:
    """What the graph dump says reduction would drop, by step, read back from ``graph.json``."""
    with tempfile.TemporaryDirectory() as wd:
        _journal(wd, debug_level=1).snapshot_taken(_step_nodes(snapshot), snapshot, None)
        data: Dict[str, Any] = _read_json(_expected_dir(wd) / "graph.json")
    return {n["name"]: set(n["dependencies_reduction_would_drop"]) for n in data["nodes"]}


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
    journal: CookJournal = _journal(blocker, debug_level=1)
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        journal.job_submitted("job-1")
        journal.bundle_dir()
        journal.snapshot_taken([], _diamond(), None)
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


def test_closing_a_refused_cook_records_its_refusals(tmp_path: Path) -> None:
    """A refused cook submits no job, so ``cook.json``'s refusals are the only record of why
    nothing reached the farm. A journal that dropped them would leave an empty list."""
    journal: CookJournal = _journal(tmp_path)
    # A cook that took its snapshot, then was refused for two reasons.
    journal.snapshot_taken([], _diamond(), None)
    refusals: Tuple[RefusalScenario, ...] = (
        RefusalScenario.ROP_BATCH,
        RefusalScenario.DYNAMIC_NODE,
    )
    journal.closed(_summary(final_phase=CookPhase.REFUSED, refusals=refusals))
    meta: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "cook.json")
    assert meta["outcome"] == "refused"
    assert sorted(meta["refusals"]) == ["dynamic_node", "rop_batch"]


# -- snapshot.json and scene_reads.json ----------------------------------------


def test_snapshot_json_matches_the_snapshot(tmp_path: Path) -> None:
    """``snapshot.json`` carries the totals, the leaf steps, the nodes that map to nothing, and
    each step's tasks, range, dependencies, and first and last item."""
    _journal(tmp_path).snapshot_taken(_diamond_nodes(), _diamond(), None)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "snapshot.json")
    # Totals and step lists first, then one step's entry in full.
    assert data["total_tasks"] == 8
    assert data["step_count"] == 4
    assert data["leaf_steps"] == ["d"]
    assert data["nodes_mapping_to_nothing"] == ["partition"]
    assert data["steps_with_remapped_indices"] == ["c"]
    assert data["steps"][3] == {
        "name": "d",
        "task_count": 2,
        "task_range": "0-1",
        "depends_on": ["a", "b", "c"],
        "depends_on_for_data": ["c"],
        "declares_outputs": True,
        "first_item": "d_0",
        "last_item": "d_1",
    }


@st.composite
def snapshots(draw: Any) -> GraphSnapshot:
    """Acyclic snapshots of 1 to 6 steps, each with 1 to 5 items and some data dependencies."""
    # Each step depends on a subset of earlier steps, so the snapshot is acyclic.
    count: int = draw(st.integers(min_value=1, max_value=6))
    steps: List[StepSpec] = []
    for i in range(count):
        earlier: List[str] = [f"s{j}" for j in range(i)]
        deps: Tuple[str, ...] = (
            tuple(sorted(draw(st.sets(st.sampled_from(earlier))))) if earlier else ()
        )
        data: Tuple[str, ...] = tuple(d for d in deps if draw(st.booleans()))
        steps.append(_step(f"s{i}", draw(st.integers(min_value=1, max_value=5)), deps, data))
    without: Tuple[str, ...] = tuple(draw(st.lists(st.sampled_from(["x", "y"]), unique=True)))
    return GraphSnapshot(steps=tuple(steps), steps_without_items=without)


@settings(max_examples=30, deadline=None)
@given(snapshots())
def test_snapshot_json_counts_and_edges_agree_with_any_snapshot(snap: GraphSnapshot) -> None:
    """For any snapshot, the step count, task total, leaf steps, data-edge subset, and first and
    last items in ``snapshot.json`` match the snapshot itself."""
    with tempfile.TemporaryDirectory() as wd:
        _journal(wd).snapshot_taken([], snap, None)
        data: Dict[str, Any] = _read_json(_expected_dir(wd) / "snapshot.json")
    # Leaf steps are those no step depends on, worked out here from the snapshot's edges.
    consumed: Set[str] = {d for s in snap.steps for d in s.dependencies}
    assert data["step_count"] == len(snap.steps)
    assert data["total_tasks"] == sum(len(s.items) for s in snap.steps)
    assert sum(s["task_count"] for s in data["steps"]) == data["total_tasks"]
    assert data["leaf_steps"] == [s.name for s in snap.steps if s.name not in consumed]
    assert data["steps_without_items"] == list(snap.steps_without_items)
    for entry, s in zip(data["steps"], snap.steps):
        assert set(entry["depends_on_for_data"]) <= set(entry["depends_on"])
        assert entry["task_range"] == f"0-{len(s.items) - 1}"
        assert entry["first_item"] == s.items[0].name
        assert entry["last_item"] == s.items[-1].name


def _read_report() -> ReadReport:
    """Reads for the diamond: one covered, one that added an edge, one external, and a read
    from a step the snapshot does not have."""

    def read(step_name: str, path: str, parm: str = "file") -> SceneRead:
        """A read of ``path`` by ``step_name``'s node."""
        return SceneRead(
            step=step_name, node_path=f"/obj/{step_name}", parm=parm, raw=path, path=path
        )

    return ReadReport(
        matches=(
            ReadMatch(read("d", "/out/c/0.bgeo"), "c", ReadVerdict.COVERED),
            ReadMatch(read("d", "/out/b/0.bgeo", "b"), "b", ReadVerdict.EDGE_ADDED, ("a",)),
            ReadMatch(read("b", "/assets/tex.png"), "", ReadVerdict.EXTERNAL),
            ReadMatch(read("ghost", "/assets/other.png"), "", ReadVerdict.EXTERNAL),
        ),
        data_edges={"d": frozenset({"b", "c"})},
        unportable=(read("b", "/home/artist/tex.png"),),
        attribute_variables=(
            AttributeVariableUse("a", "/obj/a", "sopoutput", "$WEDGE.bgeo", "WEDGE", "wedge"),
        ),
    )


def test_scene_reads_json_attributes_each_read(tmp_path: Path) -> None:
    """``scene_reads.json`` lists each step's reads with their producer and verdict, the data
    edges they gave, the unportable reads, the attribute variables, and a verdict summary."""
    _journal(tmp_path).snapshot_taken(_diamond_nodes(), _diamond(), _read_report())
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "scene_reads.json")
    assert data["version"] == 1
    assert data["available"] is True
    # One entry per step with a read or a data edge, including a step the snapshot lacks.
    by_step: Dict[str, Dict[str, Any]] = {s["step"]: s for s in data["steps"]}
    assert sorted(by_step) == ["b", "d", "ghost"]

    # ``d`` has a covered read and one that added an edge, sorted by node and parm.
    d: Dict[str, Any] = by_step["d"]
    assert d["dependencies"] == ["a", "b", "c"]
    assert d["data_edges"] == ["b", "c"]
    assert d["data_edges_added"] == ["b"]
    assert d["already_covered"] == ["c"]
    assert [r["parm"] for r in d["reads"]] == ["b", "file"]
    assert d["reads"][0]["ambiguous_with"] == ["a"]
    assert "ambiguous_with" not in d["reads"][1]
    assert d["reads"][1] == {
        "node": "/obj/d",
        "parm": "file",
        "raw": "/out/c/0.bgeo",
        "path": "/out/c/0.bgeo",
        "producer": "c",
        "verdict": "covered",
    }
    assert by_step["ghost"]["dependencies"] == []
    assert by_step["b"]["data_edges"] == []

    assert d["data_edges_refused_as_cyclic"] == []
    # The summary counts every verdict, including one no read had.
    assert data["summary"] == {
        "reads": 4,
        "self": 0,
        "covered": 1,
        "edge_added": 1,
        "external": 2,
        "unportable": 1,
        "attribute_variables": 1,
        "data_edges_refused_as_cyclic": 1,
    }
    # Unportable reads and attribute variables are listed with where they were found.
    assert data["unportable"] == [
        {
            "step": "b",
            "node": "/obj/b",
            "parm": "file",
            "raw": "/home/artist/tex.png",
            "path": "/home/artist/tex.png",
        }
    ]
    assert data["attribute_variables"] == [
        {
            "step": "a",
            "node": "/obj/a",
            "parm": "sopoutput",
            "raw": "$WEDGE.bgeo",
            "variable": "WEDGE",
            "attribute": "wedge",
        }
    ]


def test_scene_reads_json_lists_a_data_edge_refused_as_cyclic(tmp_path: Path) -> None:
    """A step whose data edge was refused because it would close a cycle shows that refusal."""
    # A data edge with no reads behind it still gives its step an entry.
    report: ReadReport = ReadReport(data_edges={"a": frozenset({"d"})})
    _journal(tmp_path).snapshot_taken([], _diamond(), report)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "scene_reads.json")
    assert data["steps"][0]["step"] == "a"
    assert data["steps"][0]["data_edges_refused_as_cyclic"] == ["d"]


def test_scene_reads_json_is_written_even_without_a_scene_walk(tmp_path: Path) -> None:
    """With no read report, ``scene_reads.json`` still exists and says the reads are unavailable."""
    _journal(tmp_path).snapshot_taken([], _diamond(), None)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "scene_reads.json")
    assert data == {"version": 1, "available": False, "steps": [], "summary": {}}


def test_a_failing_artifact_is_logged_and_does_not_stop_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A step with no items has no task range, so ``snapshot.json`` cannot be built. The
    journal warns, and still writes ``scene_reads.json``."""
    # A step with no items, which synthesis never puts in a snapshot, makes snapshot.json fail.
    broken: GraphSnapshot = GraphSnapshot(
        steps=(StepSpec(name="empty", dependencies=(), items=()),)
    )
    with caplog.at_level(logging.WARNING, logger=PACKAGE_LOGGER):
        _journal(tmp_path).snapshot_taken([], broken, None)
    assert "Could not write snapshot.json" in caplog.text
    assert not (_expected_dir(tmp_path) / "snapshot.json").exists()
    assert (_expected_dir(tmp_path) / "scene_reads.json").is_file()


# -- graph.json and debug levels -----------------------------------------------


def _artifacts_at(level: int) -> Set[str]:
    """The file names a full cook leaves at ``level``: snapshot, two items, a failure, a close."""
    with tempfile.TemporaryDirectory() as wd:
        # The log handler joins the package logger, as the cook attaches it.
        journal: CookJournal = _journal(wd, debug_level=level)
        logging.getLogger(PACKAGE_LOGGER).addHandler(journal.log_handler())
        try:
            # Every call a cook makes, in the order it makes them.
            journal.snapshot_taken(_diamond_nodes(), _diamond(), _read_report())
            journal.bundle_dir()
            journal.job_submitted("job-1")
            journal.item_scheduled(_handle("a_0", "a", 0, _native("a_0")))
            journal.item_failed(TaskKey("a", 0), lambda: ["boom"])
            journal.closed(_summary(job_id="job-1", task_count=8, succeeded=8))
        finally:
            logging.getLogger(PACKAGE_LOGGER).removeHandler(journal.log_handler())
        return {p.name for p in _expected_dir(wd).iterdir()}


def test_each_debug_level_adds_to_the_one_below_and_level_two_matches_level_one() -> None:
    """Level 0 writes the always-on files and the bundle; level 1 adds the graph dump and the
    scheduled-item files; level 2 adds nothing more, so the levels are monotone."""
    # Each level's files must contain the files of the level below.
    zero: Set[str] = _artifacts_at(0)
    one: Set[str] = _artifacts_at(1)
    assert zero == ALWAYS_ON | {"bundle"}
    assert one == zero | {"graph.json", "scheduled_items.jsonl", "scheduled_items_summary.json"}
    assert _artifacts_at(2) == one


def test_the_graph_dump_reports_what_reduction_would_drop_but_keeps_the_submitted_edges(
    tmp_path: Path,
) -> None:
    """``d``'s edge to ``a`` is implied through ``b`` and ``c``, so the dump says reduction would
    drop it, yet still lists it among the dependencies the job was submitted with. A data edge
    is never reported as droppable."""
    _journal(tmp_path, debug_level=1).snapshot_taken(_diamond_nodes(), _diamond(), None)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "graph.json")
    # ``d`` keeps all three edges, and only the implied, unprotected one is reported.
    assert data["version"] == 1
    d: Dict[str, Any] = _graph_node(data, "d")
    assert d["dependencies"] == ["a", "b", "c"]
    assert d["dependencies_reduction_would_drop"] == ["a"]
    assert d["data_dependencies"] == ["c"]
    assert _graph_node(data, "a")["data_dependencies_refused_as_cyclic"] == ["d"]
    assert _graph_node(data, "partition")["becomes_step"] is False
    assert _graph_node(data, "d")["becomes_step"] is True


def test_the_graph_dump_counts_the_whole_graph(tmp_path: Path) -> None:
    """The dump's totals count every node, item, step, task, edge, and declared file, and list
    each input that no task produces once."""
    _journal(tmp_path, debug_level=1).snapshot_taken(_diamond_nodes(), _diamond(), None)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "graph.json")
    # Totals over the five nodes, then one item's entry in full.
    assert data["totals"] == {
        "nodes": 5,
        "work_items": 9,
        "steps": 4,
        "tasks": 8,
        "step_dependency_edges": 5,
        "declared_input_refs": 9,
        "declared_output_refs": 9,
        "item_dependency_edges": 11,
    }
    assert data["unproduced_input_files"] == [{"path": "/assets/tex.png", "tag": "file/image"}]
    assert data["unproduced_input_file_count"] == 1
    c: Dict[str, Any] = _graph_node(data, "c")
    assert (c["item_count"], c["declared_input_refs"], c["item_dependency_edges"]) == (3, 3, 3)
    assert c["items"][1] == {
        "name": "c_1",
        "pdg_index": 1,
        "task_index": 1,
        "item_id": 1,
        "is_out_of_process": False,
        "is_batch": False,
        "batch_parent": "",
        "command": "hython job.py",
        "depends_on": {"count": 1, "names": ["a_0"]},
        "declared_inputs": [{"path": "/assets/tex.png", "tag": "file/image"}],
        "declared_outputs": [{"path": "/out/c/1.bgeo", "tag": "file"}],
    }


@settings(max_examples=30, deadline=None)
@given(step_graphs())
def test_the_graph_dump_dependencies_equal_the_snapshots(
    graph: Tuple[Dict[str, Set[str]], Dict[str, Set[str]]],
) -> None:
    """For any snapshot, each step's dependencies in the dump are the snapshot's own, never
    recomputed, so the dump cannot disagree with the job."""
    # Dump the drawn snapshot, then compare each node's lists with its step.
    snap: GraphSnapshot = _snapshot_of(*graph)
    with tempfile.TemporaryDirectory() as wd:
        _journal(wd, debug_level=1).snapshot_taken(_step_nodes(snap), snap, None)
        data: Dict[str, Any] = _read_json(_expected_dir(wd) / "graph.json")
    for entry in data["nodes"]:
        step: Optional[StepSpec] = snap.step(entry["name"])
        assert step is not None
        assert entry["dependencies"] == sorted(step.dependencies)
        assert entry["data_dependencies"] == sorted(step.data_dependencies)


@settings(max_examples=100, deadline=None)
@given(step_graphs())
def test_reduction_drops_exactly_the_implied_unprotected_edges(
    graph: Tuple[Dict[str, Set[str]], Dict[str, Set[str]]],
) -> None:
    """For any acyclic step graph, reduction would drop exactly the edges that are not data
    edges and that another dependency already reaches. What remains keeps every step's
    reachability, reducing it again drops nothing, and protecting every edge drops nothing."""
    deps: Dict[str, Set[str]]
    data: Dict[str, Set[str]]
    deps, data = graph
    dropped: Dict[str, Set[str]] = _would_drop(_snapshot_of(deps, data))

    # An independent model: an edge is implied when another direct dependency reaches it.
    for name, direct in deps.items():
        implied: Set[str] = {
            u for u in direct - data[name] if any(u in _reach(deps, o) for o in direct if o != u)
        }
        assert dropped[name] == implied

    # What remains reaches the same steps, keeps every data edge, and reduces to itself.
    reduced: Dict[str, Set[str]] = {n: deps[n] - dropped[n] for n in deps}
    assert all(_reach(reduced, n) == _reach(deps, n) for n in deps)
    assert all(data[n] <= reduced[n] for n in deps)
    assert all(not d for d in _would_drop(_snapshot_of(reduced, data)).values())
    assert all(not d for d in _would_drop(_snapshot_of(deps, deps)).values())


def test_the_graph_dump_of_a_cyclic_snapshot_still_terminates(tmp_path: Path) -> None:
    """A snapshot is acyclic, but a cycle in one must not hang the cook or lose the dump."""
    # ``a`` and ``b`` depend on each other, and ``c`` on both.
    snap: GraphSnapshot = GraphSnapshot(
        steps=(_step("a", 1, ("b",)), _step("b", 1, ("a",)), _step("c", 1, ("a", "b")))
    )
    _journal(tmp_path, debug_level=1).snapshot_taken(_step_nodes(snap), snap, None)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "graph.json")
    assert _graph_node(data, "c")["dependencies"] == ["a", "b"]
    assert _graph_node(data, "a")["dependencies"] == ["b"]


def test_the_graph_dump_caps_items_at_two_hundred_per_node(tmp_path: Path) -> None:
    """A node with 201 items shows 200 and is listed as truncated; one with 200 shows all of
    them. Counts stay exact either way."""
    # One node just over the cap and one exactly at it.
    nodes: List[NodeInfo] = [_node("big", items=201), _node("full", items=200)]
    snap: GraphSnapshot = GraphSnapshot(steps=(_step("big", 201), _step("full", 200)))
    _journal(tmp_path, debug_level=1).snapshot_taken(nodes, snap, None)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "graph.json")
    big: Dict[str, Any] = _graph_node(data, "big")
    assert (big["item_count"], big["items_shown"], len(big["items"])) == (201, 200, 200)
    assert big["items"][-1]["name"] == "big_199"
    assert _graph_node(data, "full")["items_shown"] == 200
    assert data["nodes_with_truncated_items"] == ["big"]
    assert data["item_detail_limit"] == 200


def test_the_graph_dump_caps_edge_names_at_fifty_per_item(tmp_path: Path) -> None:
    """An item with 51 dependencies lists 50 names and says it was truncated; one with 50 lists
    all of them with no truncation flag. The count stays exact."""
    # One item just over the cap and one exactly at it.
    many: Tuple[str, ...] = tuple(f"u{i}" for i in range(51))
    nodes: List[NodeInfo] = [
        replace(_node("wide"), items=(RawItem("w0", 0, 0, "c", depends_on=many),)),
        replace(_node("fifty"), items=(RawItem("f0", 0, 1, "c", depends_on=many[:50]),)),
    ]
    snap: GraphSnapshot = GraphSnapshot(steps=(_step("wide", 1), _step("fifty", 1)))
    _journal(tmp_path, debug_level=1).snapshot_taken(nodes, snap, None)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "graph.json")
    wide: Dict[str, Any] = _graph_node(data, "wide")["items"][0]["depends_on"]
    assert wide == {"count": 51, "names": list(many[:50]), "truncated": True}
    fifty: Dict[str, Any] = _graph_node(data, "fifty")["items"][0]["depends_on"]
    assert fifty == {"count": 50, "names": list(many[:50])}
    assert data["edge_list_limit"] == 50


def test_the_graph_dump_lists_at_most_two_hundred_unproduced_inputs(tmp_path: Path) -> None:
    """With 201 distinct inputs that no task produces, 200 are listed in path order and the
    count says 201."""
    # One item that reads one more scene file than the cap allows.
    inputs: Tuple[DeclaredFile, ...] = tuple(
        DeclaredFile(f"/assets/{i:03d}.png", "file") for i in range(201)
    )
    nodes: List[NodeInfo] = [
        replace(_node("a"), items=(RawItem("a0", 0, 0, "c", declared_inputs=inputs),))
    ]
    _journal(tmp_path, debug_level=1).snapshot_taken(
        nodes, GraphSnapshot(steps=(_step("a", 1),)), None
    )
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "graph.json")
    assert data["unproduced_input_file_count"] == 201
    assert [f["path"] for f in data["unproduced_input_files"]] == [f.path for f in inputs[:200]]


def test_the_graph_dump_explains_why_each_node_is_or_is_not_a_step(tmp_path: Path) -> None:
    """Each node's ``why`` says what decided it: a known flag, its items' commands, or having
    generated no items."""
    # One node for each reason a node is or is not a step.
    nodes: List[NodeInfo] = [
        _node("flagged", items=2),
        _node("inside", schedulable=False),
        _bare("mixed", ("x", "")),
        _bare("none", ("",)),
        _bare("promoted", ()),
        _bare("plain", ()),
        _node("empty", items=0),
    ]
    snap: GraphSnapshot = GraphSnapshot(
        steps=(_step("flagged", 2), _step("mixed", 1), _step("promoted", 1)),
        steps_without_items=("empty",),
    )
    _journal(tmp_path, debug_level=1).snapshot_taken(nodes, snap, None)
    data: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "graph.json")
    why: Dict[str, str] = {n["name"]: n["why"] for n in data["nodes"]}
    assert why == {
        "flagged": "runs a command on a worker (2 item(s) generated)",
        "inside": "has no command for a worker, so it runs inside PDG",
        "mixed": "1 of 2 item(s) carry a command",
        "none": "none of 1 item(s) carry a command",
        "promoted": "becomes a step",
        "plain": "none of 0 item(s) carry a command",
        "empty": "runs a command on a worker, but generated no items",
    }


# -- scheduled items ----------------------------------------------------------


def test_no_items_are_described_at_debug_level_zero(tmp_path: Path) -> None:
    """At the default level, scheduling an item writes nothing at all."""
    journal: CookJournal = _journal(tmp_path)
    journal.item_scheduled(_handle("a_0", "a", 0, _native("a_0")))
    assert not (tmp_path / "pdg_debug").exists()


def test_the_first_three_items_of_each_node_are_described(tmp_path: Path) -> None:
    """Only the first three items PDG offers from each node are described, so one large node
    cannot spend every slot and the cost stays bounded."""
    journal: CookJournal = _journal(tmp_path, debug_level=1)
    # Five items from one node and one from another.
    for i in range(5):
        journal.item_scheduled(_handle(f"a_{i}", "a", i, _native(f"a_{i}")))
    journal.item_scheduled(_handle("b_0", "b", 0, _native("b_0")))
    records: List[Dict[str, Any]] = _scheduled(_expected_dir(tmp_path))
    assert [r["name"] for r in records] == ["a_0", "a_1", "a_2", "b_0"]
    assert [(r["node"], r["index"]) for r in records] == [("a", 0), ("a", 1), ("a", 2), ("b", 0)]
    assert records[0]["described_from"] == "work_item"
    assert records[0]["item"]["name"] == "a_0"

    # The summary counts every offer, described or not.
    journal.closed(_summary(job_id="job-1", task_count=6, succeeded=6))
    roll_up: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "scheduled_items_summary.json")
    assert roll_up["per_node_limit"] == 3
    assert roll_up["items_offered"] == 6
    assert roll_up["items_offered_per_node"] == {"a": 5, "b": 1}
    assert roll_up["logged_items"] == 4
    assert roll_up["logged_per_node"] == {"a": 3, "b": 1}


@settings(max_examples=40, deadline=None)
@given(st.lists(st.sampled_from(["a", "b", "c"]), max_size=20))
def test_any_offer_sequence_describes_the_first_three_of_each_node(offers: List[str]) -> None:
    """For any order in which PDG offers items from several nodes, exactly the first three
    offers of each node are described, in offer order, and the summary counts every offer."""
    with tempfile.TemporaryDirectory() as wd:
        journal: CookJournal = _journal(wd, debug_level=1)
        journal.job_submitted("job-1")
        # Offer the items, numbering each node's items in the order they arrive.
        seen: Dict[str, int] = {}
        expected: List[str] = []
        for node_name in offers:
            # Only a node's first three offers are expected in the sample.
            index: int = seen.get(node_name, 0)
            seen[node_name] = index + 1
            name: str = f"{node_name}_{index}"
            if index < 3:
                expected.append(name)
            journal.item_scheduled(_handle(name, node_name, index, _native(name)))
        journal.closed(_summary(job_id="job-1", task_count=1, succeeded=1))

        # The sample and the summary, against counts kept by the test itself.
        directory: Path = _expected_dir(wd)
        described: List[str] = [r["name"] for r in _scheduled(directory)] if expected else []
        assert described == expected
        if offers:
            roll_up: Dict[str, Any] = _read_json(directory / "scheduled_items_summary.json")
            assert roll_up["items_offered_per_node"] == seen
            assert roll_up["logged_per_node"] == {n: min(c, 3) for n, c in seen.items()}


def test_the_diff_reports_what_changed_since_generation(tmp_path: Path) -> None:
    """Each described item is compared with what generation declared: inputs gained, inputs
    with no PDG token, outputs gained or lost, and a changed command. An item generation
    never saw is marked unknown. The summary counts each kind over the sample."""
    journal: CookJournal = _journal(tmp_path, debug_level=1)
    # One item whose command and files changed since generation, one that lost its output, and one generation never saw.
    journal.snapshot_taken([], _diamond(), None)
    changed: SimpleNamespace = _native(
        "a_0",
        command="rewritten",
        inputs=("/abs/in.bgeo", "__PDG_DIR__/in2.bgeo"),
        outputs=("/out/a/0.bgeo", "/out/a/extra.bgeo"),
    )
    journal.item_scheduled(_handle("a_0", "a", 0, changed))
    journal.item_scheduled(_handle("b_0", "b", 0, _native("b_0")))
    journal.item_scheduled(_handle("ghost", "zzz", 0, _native("ghost")))
    first: Dict[str, Any]
    same: Dict[str, Any]
    ghost: Dict[str, Any]
    first, same, ghost = _scheduled(_expected_dir(tmp_path))

    assert first["diff"] == {
        "known_at_generation": True,
        "gained_input_files": ["/abs/in.bgeo", "__PDG_DIR__/in2.bgeo"],
        "untokenized_input_files": ["/abs/in.bgeo"],
        "gained_declared_outputs": ["/out/a/extra.bgeo"],
        "lost_declared_outputs": [],
        "command_changed": True,
        "command_at_generation": "c",
    }
    assert first["counts"] == {
        "input_files": 2,
        "expected_input_files": 0,
        "output_files": 0,
        "expected_output_files": 2,
    }
    assert same["diff"] == {
        "known_at_generation": True,
        "gained_input_files": [],
        "untokenized_input_files": [],
        "gained_declared_outputs": [],
        "lost_declared_outputs": ["/out/b/0.bgeo"],
        "command_changed": False,
        "command_at_generation": "c",
    }
    assert ghost["diff"] == {"known_at_generation": False}

    # The summary counts each kind of change once per item that has it.
    journal.closed(_summary(job_id="job-1", task_count=8, succeeded=8))
    roll_up: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "scheduled_items_summary.json")
    assert roll_up == {
        "per_node_limit": 3,
        "items_offered": 3,
        "items_offered_per_node": {"a": 1, "b": 1, "zzz": 1},
        "logged_items": 3,
        "logged_per_node": {"a": 1, "b": 1, "zzz": 1},
        "sampled_unknown_at_generation": 1,
        "sampled_gained_input_files": 1,
        "sampled_untokenized_inputs": 1,
        "sampled_gained_declared_outputs": 1,
        "sampled_lost_declared_outputs": 1,
        "sampled_command_changed": 1,
    }


class _Unreadable:
    """A live work item whose every attribute raises, as PDG's object can once it is gone."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"cannot read {name}")


@pytest.mark.parametrize("native", [None, _Unreadable()], ids=["missing", "unreadable"])
def test_an_item_that_cannot_be_read_is_described_from_its_handle_with_no_spurious_change(
    tmp_path: Path, native: Any
) -> None:
    """When PDG's work item cannot be read, the item is described from what the scheduler was
    offered, so the diff does not report every output generation declared as lost."""
    journal: CookJournal = _journal(tmp_path, debug_level=1)
    journal.snapshot_taken([], _diamond(), None)
    # Item ``a_0`` is offered with the output generation declared for it.
    journal.item_scheduled(_handle("a_0", "a", 0, native))
    record: Dict[str, Any] = _scheduled(_expected_dir(tmp_path))[0]
    assert record["described_from"] == "handle"
    assert record["item"]["name"] == "a_0"
    assert record["diff"]["lost_declared_outputs"] == []
    assert record["diff"]["gained_declared_outputs"] == []

    # The summary counts no lost outputs either.
    journal.closed(_summary(job_id="job-1", task_count=8, succeeded=8))
    roll_up: Dict[str, Any] = _read_json(_expected_dir(tmp_path) / "scheduled_items_summary.json")
    assert roll_up["sampled_lost_declared_outputs"] == 0


def test_no_item_summary_is_written_when_no_item_was_described(tmp_path: Path) -> None:
    """At level 1, a cook that described no items writes no item summary."""
    journal: CookJournal = _journal(tmp_path, debug_level=1)
    # A cook that made its directory but described no item.
    journal.job_submitted("job-1")
    journal.closed(_summary(job_id="job-1", task_count=1, succeeded=1))
    assert not (_expected_dir(tmp_path) / "scheduled_items_summary.json").exists()


@settings(max_examples=30, deadline=None)
@given(
    count=st.integers(min_value=0, max_value=6),
    partial=st.text(alphabet='{"ab:', max_size=6),
)
def test_scheduled_items_parse_back_despite_a_trailing_partial_line(
    count: int, partial: str
) -> None:
    """``scheduled_items.jsonl`` is appended one line per item, so when a crash leaves a partial
    last line, every complete line still parses back to its item, in order."""
    with tempfile.TemporaryDirectory() as wd:
        journal: CookJournal = _journal(wd, debug_level=1)
        journal.job_submitted("job-1")
        path: Path = _expected_dir(wd) / "scheduled_items.jsonl"
        # One item per node, so each is described, then a partial line as a crash would leave.
        for i in range(count):
            journal.item_scheduled(_handle(f"n{i}_0", f"n{i}", 0, _native(f"n{i}_0")))
        with open(path, "a", encoding="utf-8") as f:
            f.write(partial)
        complete: List[str] = path.read_text(encoding="utf-8").split("\n")[:-1]
        assert [json.loads(line)["name"] for line in complete] == [f"n{i}_0" for i in range(count)]


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
    journal: CookJournal = _journal(tmp_path, debug_level=1, events=events)
    journal.job_submitted("job-1")
    journal.log_handler().handle(_record("before"))
    directory: Path = _expected_dir(tmp_path)
    # Make the directory read-only, then call every method.
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        # None of these can create a file, and none may raise.
        journal.snapshot_taken(_diamond_nodes(), _diamond(), _read_report())
        journal.item_scheduled(_handle("a_0", "a", 0, _native("a_0")))
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
    journal: CookJournal = _journal(tmp_path, debug_level=1, events=events)
    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        # A file named ``bundle`` blocks the bundle directory, so it falls back elsewhere.
        journal.job_submitted("job-1")
        directory: Path = _expected_dir(tmp_path)
        (directory / "bundle").write_text("")
        fallback: str = journal.bundle_dir()
        shutil.rmtree(fallback)

        # A read-only directory fails the item record and the cook.json merge, and the
        # telemetry call raises.
        directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            journal.item_scheduled(_handle("a_0", "a", 0, _native("a_0")))
            journal.closed(_summary(job_id="job-1", task_count=1, succeeded=1))
        finally:
            directory.chmod(stat.S_IRWXU)

    with_traceback: Set[str] = {
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and r.exc_info
    }
    assert with_traceback >= {
        "Could not create the bundle directory",
        "Could not record a scheduled work item",
        "Could not write cook.json",
        "Could not record the cook outcome event",
    }


def test_a_debug_level_that_is_not_a_number_reads_as_zero(tmp_path: Path) -> None:
    """A debug level that cannot be read as a number falls back to the default level instead
    of failing the cook."""
    journal: CookJournal = _journal(tmp_path, debug_level="high")
    # Level 0 describes no item, and cook.json records the level as 0.
    journal.item_scheduled(_handle("a_0", "a", 0, _native("a_0")))
    assert not (tmp_path / "pdg_debug").exists()
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
