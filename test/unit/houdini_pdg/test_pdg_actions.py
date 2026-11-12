# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the PDG worker actions.

Shell items run real commands. The in-process cook runs in a child process, with
small stand-ins for Houdini's ``hou``, ``pdg`` and ``pdgjob`` on its path.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import gzip
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Type

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from openjd.adaptor_runtime.adaptors import PathMappingRule as RuntimeRule
from openjd.adaptor_runtime_client import PathMappingRule as ClientRule

from deadline.houdini_adaptor import pdg_actions
from deadline.houdini_adaptor.pdg_actions import (
    EXIT_CODE_MARKER,
    TokenResolver,
    WorkItemPayload,
    PdgWorkItemRunner,
    download_pdg_work_item,
    publish_pdg_outputs,
)
from deadline.houdini_pdg.errors import PayloadTransferError
from deadline.houdini_pdg.graph import PDG_DIR_TOKEN
from deadline.houdini_pdg.ports import OutputFile, TaskAddress, TaskStorage
from deadline.houdini_pdg.task_storage import S3TaskStorage

from .test_task_storage import FakeS3

# Every token the resolver knows, apart from the item ID, which needs a value.
TOKENS: Tuple[str, ...] = tuple(
    f"__PDG_{name}__"
    for name in "HYTHON PYTHON SCRIPTDIR HFS DIR SHARED_ROOT TEMP SHARED_TEMP ITEM_NAME"
    " ITEM_LABEL RESULT_SERVER".split()
)

# The variables the worker sets itself, written out so the test doesn't read the code's own list.
RESERVED: Tuple[str, ...] = tuple(
    f"PDG_{name}"
    for name in "ITEM_NAME ITEM_ID INDEX ITEM_DATA_SOURCE TEMP DIR OUTPUT_DIR SCRIPTDIR"
    " RESULT_SERVER".split()
)


def attrib(value: Any, flag: int = 0, kind: int = 2) -> Dict[str, Any]:
    """One attribute as PDG serializes it. Kind 0 is int, 1 is float, 2 is string."""
    return {"type": kind, "flag": flag, "own": True, "concat": False, "value": value}


def payload(
    command: str = "echo hi",
    *,
    name: Optional[str] = None,
    node: str = "ropfetch1",
    item_id: int = 7,
    attributes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A work item payload shaped like the ones PDG writes, with the command in its attribute."""
    # Flag 137 is how PDG marks its own command attribute: internal, not exported.
    attrs: Dict[str, Any] = {"__pdg_commandstring": attrib([command], flag=137)} if command else {}
    attrs.update(attributes or {})
    item: Dict[str, Any] = {"nodeName": node, "id": item_id, "index": 3, "attributes": attrs}
    if name is not None:
        item["name"] = name
    return {"version": 7, "workitem": item, "graph": {}}


def rule(source: str, dest: str, source_format: str = "POSIX", runtime: bool = False) -> Any:
    """A rule mapping to a POSIX path, as the client sends it, or as the runtime applies it."""
    kind: Any = RuntimeRule if runtime else ClientRule
    return kind(
        source_path_format=source_format,
        source_path=source,
        destination_path=dest,
        destination_os="POSIX",
    )


def test_item_name_prefers_the_work_item_name_then_node_and_id_then_top_level() -> None:
    """The payload file is named after the item, so the wrong pick would write it where PDG never looks."""
    # Each fallback is used only when the ones before it are missing.
    assert WorkItemPayload(payload(name="given")).item_name == "given"
    assert WorkItemPayload(payload(node="n", item_id=4)).item_name == "n_4"
    assert WorkItemPayload(payload(node="n", item_id=0)).item_name == "n_0"
    assert WorkItemPayload({"name": "top"}).item_name == "top"
    assert WorkItemPayload({"workitem": {"nodeName": "n"}, "name": "top"}).item_name == "top"
    assert WorkItemPayload({"workitem": "garbage"}).item_name == ""


def test_command_prefers_the_work_item_command_then_the_command_attribute() -> None:
    """The worker runs whatever this returns, so a wrong fallback order runs the wrong command."""
    data: Dict[str, Any] = payload("from attribute")
    data["command"] = "top"
    assert WorkItemPayload(data).command == "from attribute"
    data["workitem"]["command"] = "explicit"
    assert WorkItemPayload(data).command == "explicit"
    assert WorkItemPayload({"command": "top"}).command == "top"
    assert WorkItemPayload(payload("")).command == ""
    # An empty first value isn't a command, so the next source is used.
    empty_first: Dict[str, Any] = payload("", attributes={"__pdg_commandstring": attrib([""])})
    empty_first["command"] = "top"
    assert WorkItemPayload(empty_first).command == "top"


def test_environment_formats_values_by_type() -> None:
    """A wedge value often ends up in a file name, so it must read exactly as PDG prints it."""
    data: Dict[str, Any] = payload(
        attributes={
            "INT": attrib([1], flag=2, kind=0),
            "FLOAT": attrib([0], flag=2, kind=1),
            "VEC": attrib([1, 2.5, 3], flag=2, kind=1),
            "TEXT": attrib(["a b"], flag=2),
            "BADINT": attrib(["x"], flag=2, kind=0),
            "WHOLE": attrib([2.0], flag=2, kind=0),
            "INTERNAL": attrib(["x"], flag=2 | 8),
            "PLAIN": attrib(["x"], flag=0),
            "FILE": attrib([{"data": "__PDG_DIR__/a", "tag": "file"}], flag=2, kind=3),
            "EMPTY": attrib([], flag=2),
            "BADFLAG": {"flag": "nope", "value": ["x"]},
            "NOTADICT": 5,
        }
    )
    assert WorkItemPayload(data).exported_variables() == {
        "INT": "1",
        "FLOAT": "0.0",
        "VEC": "1.0 2.5 3.0",
        "TEXT": "a b",
        "BADINT": "x",
        "WHOLE": "2",
    }


def test_a_serialized_environment_block_takes_precedence() -> None:
    """When PDG did serialize an environment, the worker must use it rather than rebuild one."""
    # The block wins over the attributes, both inside the work item and at the top level.
    data: Dict[str, Any] = payload(attributes={"FROM_ATTR": attrib(["1"], flag=2)})
    data["workitem"]["environment"] = {"FROM_BLOCK": 2}
    assert WorkItemPayload(data).exported_variables() == {"FROM_BLOCK": "2"}
    top: Dict[str, Any] = payload(attributes={"FROM_ATTR": attrib(["1"], flag=2)})
    top["environment"] = {"FROM_TOP": "3"}
    assert WorkItemPayload(top).exported_variables() == {"FROM_TOP": "3"}


def test_exported_attributes_never_override_reserved_names() -> None:
    """PDG's loader reads the reserved variables, so an attribute named like one would break the item."""
    # Reserved and unsettable names are dropped from both the attributes and the blocks.
    attributes: Dict[str, Any] = {name: attrib(["evil"], flag=2) for name in RESERVED}
    attributes["WEDGE"] = attrib(["1"], flag=2)
    data: Dict[str, Any] = payload(attributes=attributes)
    data["environment"] = {"PDG_DIR": "/elsewhere"}
    data["workitem"]["environment"] = {
        "PDG_ITEM_NAME": "other",
        "OK": "yes",
        "BAD=NAME": "x",
        "NUL\0NAME": "x",
        "NULVALUE": "a\0b",
        "": "empty",
    }
    assert WorkItemPayload(data).exported_variables() == {"OK": "yes"}
    assert WorkItemPayload(payload(attributes=attributes)).exported_variables() == {"WEDGE": "1"}


scalars: st.SearchStrategy[Any] = st.one_of(
    st.integers(-(10**6), 10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(alphabet="abcxyz", min_size=1, max_size=4),
)


@given(
    st.dictionaries(
        st.text(alphabet="ABCDEFG_", min_size=1, max_size=6),
        st.tuples(
            st.integers(0, 0xFFFF),
            st.sampled_from([0, 1, 2]),
            st.lists(st.integers(-1000, 1000), max_size=4),
            st.booleans(),
        ),
        max_size=8,
    )
)
def test_exported_keys_are_exactly_the_env_export_and_not_internal_attributes(
    spec: Dict[str, Tuple[int, int, List[int], bool]],
) -> None:
    """For any flags, exactly the EnvExport-and-not-Internal attributes with plain values are exported."""
    attributes: Dict[str, Any] = {}
    for name, (flag, kind, values, as_file) in spec.items():
        value: List[Any] = [{"data": "/f"}] if as_file else list(values)
        attributes[name] = attrib(value, flag=flag, kind=kind)
    env: Dict[str, str] = WorkItemPayload(
        payload(command="", attributes=attributes)
    ).exported_variables()
    expected: Set[str] = {
        name
        for name, (flag, _, values, as_file) in spec.items()
        if flag & 2 and not flag & 8 and not as_file and values
    }
    assert set(env) == expected
    # Integral floats print with ".0", and k components print as k space-separated parts.
    for name in expected:
        _flag, kind, values, _as_file = spec[name]
        parts: List[str] = env[name].split(" ")
        assert len(parts) == len(values)
        if kind == 1:
            assert parts == [f"{v}.0" for v in values]
        else:
            assert parts == [str(v) for v in values]


@given(
    st.dictionaries(
        st.text(alphabet="ABC", min_size=1, max_size=3),
        st.tuples(st.integers(0, 0xFFFF), st.lists(scalars, min_size=1, max_size=3)),
        max_size=6,
    ),
    st.integers(0, 0xFFFF).map(lambda m: m & ~0b1010),
)
def test_flags_other_than_env_export_and_internal_change_nothing(
    spec: Dict[str, Tuple[int, List[Any]]], mask: int
) -> None:
    """For any other flag bits, the exported environment is the same, so only the two bits PDG uses count."""

    def env(extra: int) -> Dict[str, str]:
        attributes: Dict[str, Any] = {
            n: attrib(v, flag=f | extra, kind=0) for n, (f, v) in spec.items()
        }
        return WorkItemPayload(payload(command="", attributes=attributes)).exported_variables()

    assert env(mask) == env(0)


json_values: st.SearchStrategy[Any] = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(max_size=5), children, max_size=3),
    max_leaves=12,
)


@given(json_values, json_values)
def test_environment_never_raises_on_malformed_input(attributes: Any, environment: Any) -> None:
    """For any JSON in the attributes, the worker gets strings back instead of a crash mid-task."""
    data: Dict[str, Any] = {"workitem": {"attributes": attributes, "environment": environment}}
    env: Dict[str, str] = WorkItemPayload(data).exported_variables()
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())


def test_payload_changes_outside_the_command_and_exports_leave_both_unchanged() -> None:
    """The worker never branches on node name or type, so changing them changes nothing it runs."""
    # Change everything the worker should ignore, and check what it runs stays the same.
    base: Dict[str, Any] = payload("run it", attributes={"W": attrib([1], flag=2, kind=0)})
    changed: Dict[str, Any] = copy.deepcopy(base)
    changed["workitem"]["nodeName"] = "other"
    changed["workitem"]["type"] = 3
    changed["workitem"]["attributes"]["hidden"] = attrib(["x"], flag=1)
    for data in (base, changed):
        assert WorkItemPayload(data).command == "run it"
        assert WorkItemPayload(data).exported_variables() == {"W": "1"}


segments: st.SearchStrategy[Any] = st.text(alphabet="abc", min_size=1, max_size=3)
posix_paths: st.SearchStrategy[Any] = st.lists(segments, min_size=1, max_size=4).map(
    lambda parts: "/" + "/".join(parts)
)
# Payload data with no character that can start an embedded path, so each string maps whole or not at all.
mappable: st.SearchStrategy[Any] = st.recursive(
    st.one_of(posix_paths, st.text(alphabet="abc/", max_size=8), st.integers()),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(posix_paths | segments, children, max_size=3),
    max_leaves=10,
)


def first_match(value: Any, rules: Sequence[RuntimeRule]) -> Any:
    """Every string leaf mapped by the first runtime rule that matches it, as the adaptor maps one path."""
    # Recurse through lists and objects, and map each string leaf on its own.
    if isinstance(value, str):
        for runtime_rule in rules:
            changed: bool
            mapped: str
            changed, mapped = runtime_rule.apply(path=value)
            if changed:
                return mapped
        return value
    if isinstance(value, dict):
        return {k: first_match(v, rules) for k, v in value.items()}
    if isinstance(value, list):
        return [first_match(v, rules) for v in value]
    return value


@given(mappable, st.lists(st.tuples(posix_paths, posix_paths), max_size=4))
def test_mapping_rewrites_each_string_with_the_first_matching_rule(
    data: Any, pairs: List[Tuple[str, str]]
) -> None:
    """For any payload and rules, each string maps as the runtime maps it, and keys are untouched."""
    # The same rules as the client sends them and as the runtime applies them.
    mapped: WorkItemPayload = WorkItemPayload({"root": data}).map_paths([rule(*p) for p in pairs])
    assert mapped.data == {"root": first_match(data, [rule(*p, runtime=True) for p in pairs])}
    if not pairs:
        assert mapped.data == {"root": data}


def test_mapping_accepts_the_runtimes_own_rules() -> None:
    """The adaptor may hand over rules that already know how to apply themselves."""
    rules: List[Any] = [rule("/a", "/b", runtime=True)]
    assert WorkItemPayload({"p": "/a/x"}).map_paths(rules).data == {"p": "/b/x"}


def test_mapping_rewrites_paths_embedded_in_a_command_line() -> None:
    """ImageMagick items carry their whole argument list as one string, with workstation paths inside."""
    # Quoted, after `=`, after `(`, `,` and `;`: each is mapped. A sibling folder and a glued path aren't.
    show: ClientRule = rule("/home/artist/show", "/sessions/s/assetroot")
    arguments: str = (
        '-mode concatenate "__PDG_DIR__/in.png" -background black '
        '"/home/artist/show/proj/out.0001.png" --log=/home/artist/show/log.txt '
        "/home/artist/shows/other.png x/home/artist/show/glued.png "
        "(/home/artist/show/a ,/home/artist/show/b ;/home/artist/show"
    )
    mapped: str = WorkItemPayload({"args": arguments}).map_paths([show]).data["args"]
    assert mapped == (
        '-mode concatenate "__PDG_DIR__/in.png" -background black '
        '"/sessions/s/assetroot/proj/out.0001.png" --log=/sessions/s/assetroot/log.txt '
        "/home/artist/shows/other.png x/home/artist/show/glued.png "
        "(/sessions/s/assetroot/a ,/sessions/s/assetroot/b ;/sessions/s/assetroot"
    )
    # Rules are tried in order, so the first one that maps a path wins, and a later one still applies.
    first_wins: WorkItemPayload = WorkItemPayload({"c": "'/a/b/x' '/ab/y' /c/z"}).map_paths(
        [rule("/a", "/one"), rule("/a/b", "/two"), rule("/c", "/three")]
    )
    assert first_wins.data == {"c": "'/one/b/x' '/ab/y' /three/z"}
    # Houdini writes Windows paths with forward slashes, so a backslash source maps them too.
    tiles: WorkItemPayload = WorkItemPayload({"c": "-tile 2x2 C:/proj/in.png -o C:/proj/out.png"})
    assert tiles.map_paths([rule("C:\\proj", "/mnt/proj", "WINDOWS")]).data == {
        "c": "-tile 2x2 /mnt/proj/in.png -o /mnt/proj/out.png"
    }
    # A doubled slash or a "." segment inside the source, as `$JOB/` then `/shots` gives, maps too.
    shots: WorkItemPayload = WorkItemPayload({"c": "-o C:/proj//shots/a.png -i C:/./proj/shots/b"})
    assert shots.map_paths([rule("C:\\proj\\shots", "/mnt/shots", "WINDOWS")]).data == {
        "c": "-o /mnt/shots/a.png -i /mnt/shots/b"
    }
    # The rule gets the path as written, so a POSIX rule still refuses //a/f and \a\f inside a command line.
    spellings: WorkItemPayload = WorkItemPayload({"c": "-o //a/f -i \\a\\f -x /a//f"})
    assert spellings.map_paths([rule("/a", "/m")]).data == {"c": "-o //a/f -i \\a\\f -x /m/f"}


def test_mapping_reaches_windows_sources() -> None:
    """A scene saved on Windows names its paths with drive letters, in either slash and any case."""
    windows: ClientRule = rule("C:\\Proj", "/mnt/proj", "WINDOWS")
    mapped: Any = WorkItemPayload({"p": "c:/proj/geo/a.bgeo", "q": "D:\\proj"}).map_paths([windows])
    assert mapped.data == {"p": "/mnt/proj/geo/a.bgeo", "q": "D:\\proj"}


def test_an_embedded_windows_path_matches_only_in_the_rules_case() -> None:
    """Inside a longer string a Windows source matches in its own case, so another case stays."""
    windows: ClientRule = rule("C:\\Proj", "/mnt/proj", "WINDOWS")
    mapped: Any = WorkItemPayload({"c": "run C:\\Proj\\a c:\\proj\\b"}).map_paths([windows])
    assert mapped.data == {"c": "run /mnt/proj/a c:\\proj\\b"}


token_free: st.SearchStrategy[Any] = st.text(alphabet="abcxyz /.-\"'", max_size=10)


def resolver(platform: str = "linux", **overrides: str) -> TokenResolver:
    """A resolver on the given platform, with plain values and any of them overridden."""
    # The class lets a subclass name the platform, so no test changes sys.platform.
    on_platform: Any = type("Resolver", (TokenResolver,), {"_platform": platform})
    values: Dict[str, str] = dict(
        hfs="/hfs", pdg_dir="/w", pdg_temp="/w/t", item_name="item", python_version="3.11"
    )
    values.update(overrides)
    return on_platform(**values)


@given(
    st.lists(st.one_of(token_free, st.sampled_from(TOKENS)), max_size=8),
    st.tuples(token_free, token_free, token_free, token_free).filter(
        lambda t: all("__" not in v for v in t)
    ),
)
def test_every_known_token_is_replaced_with_its_value(
    parts: List[str], values: Tuple[str, str, str, str]
) -> None:
    """For any command, no known token survives, each gets its value, and resolving again changes nothing."""
    # Resolve a random mix of text and tokens, then check each token's value directly.
    hfs, pdg_dir, pdg_temp, name = values
    r: TokenResolver = resolver(hfs=hfs, pdg_dir=pdg_dir, pdg_temp=pdg_temp, item_name=name)
    once: str = r.resolve("|".join(parts))
    assert not any(t in once for t in TOKENS)
    assert r.resolve(once) == once
    assert r.resolve("__PDG_DIR__|__PDG_SHARED_ROOT__") == f"{pdg_dir}|{pdg_dir}"
    assert r.resolve("__PDG_TEMP__|__PDG_SHARED_TEMP__") == f"{pdg_temp}|{pdg_temp}"
    assert r.resolve("__PDG_ITEM_NAME__") == name
    assert r.resolve("__PDG_RESULT_SERVER__") == ""
    assert r.resolve("__PDG_HFS__") == hfs


@given(
    st.lists(st.one_of(token_free, st.sampled_from(TOKENS)), max_size=6).map("|".join),
    st.text(alphabet="ABCXYZ", min_size=1, max_size=6).map(lambda s: f"__PDG_UNKNOWN{s}__"),
)
def test_unknown_tokens_are_left_verbatim(command: str, unknown: str) -> None:
    """For any command, an unknown token stays as written, so the failure it causes names it."""
    r: TokenResolver = resolver()
    assert r.resolve(command + "|" + unknown) == r.resolve(command) + "|" + unknown


def test_tokens_name_houdinis_own_executables() -> None:
    """A stock job runs Houdini's hython and Python, so a wrong path fails every item on that platform."""
    assert resolver().resolve('"__PDG_HYTHON__" "__PDG_SCRIPTDIR__/rop.py" json') == (
        '"/hfs/bin/hython" "/hfs/houdini/python3.11libs/pdgjob/rop.py" json'
    )
    assert resolver().resolve("__PDG_PYTHON__") == "/hfs/python/bin/python"
    assert resolver("win32").resolve("__PDG_PYTHON__ __PDG_HYTHON__") == (
        "/hfs/python311/python.exe /hfs/bin/hython.exe"
    )
    assert resolver("darwin").resolve("__PDG_PYTHON__ __PDG_HYTHON__") == (
        "/hfs/../../../../Python.framework/Versions/3.11/bin/python3.11 /hfs/bin/hython"
    )
    # Without HFS the shell finds the programs on PATH.
    bare: TokenResolver = resolver(hfs="")
    assert bare.resolve("__PDG_HYTHON__|__PDG_PYTHON__|__PDG_SCRIPTDIR__") == "hython|python|"


def test_the_item_id_and_label_tokens_carry_the_items_values_or_their_defaults() -> None:
    """A command that names its item by ID or label must get that item's values.

    A label defaults to the item name, as in PDG, and an unknown ID stays as written,
    because a made-up ID would hide a broken payload.
    """
    # With values, then with neither, where the label falls back and the ID token stays.
    given_values: TokenResolver = resolver(item_id="42", item_label="hero shot")
    assert given_values.resolve("__PDG_ITEM_ID__|__PDG_ITEM_LABEL__") == "42|hero shot"
    defaults: TokenResolver = resolver(item_name="render_3")
    assert defaults.resolve("__PDG_ITEM_ID__|__PDG_ITEM_LABEL__") == "__PDG_ITEM_ID__|render_3"


@pytest.mark.parametrize(
    "name, accepted",
    [(n, True) for n in ("ropfetch1_7", "a.b", "...", "hero shot")]
    + [(n, False) for n in ("", ".", "..", "a/b", "a\\b", "c:x", "a\0b")],
)
def test_an_item_name_must_be_one_file_name(name: str, accepted: bool) -> None:
    """The name becomes a file name in the temp folder, so a path in it would write somewhere else."""
    # A plain name, dots included, is accepted. Anything that is a path is refused.
    if accepted:
        pdg_actions._check_item_name(name)
    else:
        with pytest.raises(ValueError):
            pdg_actions._check_item_name(name)


@given(
    st.lists(
        st.one_of(
            st.tuples(
                st.just("result"),
                st.text(alphabet="abc/._", min_size=1, max_size=8),
                st.text(alphabet="file/geo", max_size=8),
            ),
            st.tuples(st.just("noise"), st.text(alphabet="abc ;:", max_size=10), st.just("")),
            st.tuples(st.just("short"), st.text(alphabet="abc", max_size=4), st.just("")),
        ),
        max_size=10,
    )
)
def test_results_are_parsed_in_order_with_pdg_dir_resolved(
    entries: List[Tuple[str, str, str]],
) -> None:
    """For any mix of result and noise lines, the outputs come back in order with their real paths."""
    # Build the lines and the expected outputs side by side, in the same order.
    lines: List[str] = []
    expected: List[OutputFile] = []
    for kind, text, tag in entries:
        if kind == "result":
            lines.append(f"[12:00:00.000] PDG_RESULT: 7;-1;__PDG_DIR__/{text};{tag};0\n")
            expected.append(OutputFile(f"/w/{text}", tag))
        elif kind == "short":
            lines.append(f"PDG_RESULT: 7;{text}\n")
        else:
            lines.append(text + "\n")
    assert pdg_actions._parse_pdg_results(lines, "/w") == expected


def test_results_skip_an_empty_path_and_keep_an_empty_tag() -> None:
    """A line with no path names no file, a missing tag stays empty, and a tag is kept whole."""
    lines: List[str] = [
        "PDG_RESULT: 1;-1;;file/geo;0",
        "PDG_RESULT: 1;-1;/x/a.exr",
        "PDG_RESULT_ARRAY: 1;2;3",
        "PDG_RESULT: 1;-1;/x/b.exr;file/image",
        "PDG_RESULT: 1;-1;/x/c.exr;said PDG_RESULT: twice;0",
    ]
    assert pdg_actions._parse_pdg_results(lines, "/w") == [
        OutputFile("/x/a.exr", ""),
        OutputFile("/x/b.exr", "file/image"),
        OutputFile("/x/c.exr", "said PDG_RESULT: twice"),
    ]


@given(
    st.lists(st.text(alphabet="abc", min_size=1, max_size=3), min_size=0, max_size=3),
    st.sampled_from(["", "x", "-old", ".bak"]),
    st.sampled_from(["/", "\\"]),
)
def test_tokenizing_round_trips_and_never_touches_sibling_prefixes(
    rest: List[str], sibling: str, sep: str
) -> None:
    """For any output under the work folder, tokenizing then resolving gives it back, and siblings stay."""
    # A path under the folder, with either separator, and a sibling folder that shares the prefix.
    pdg_dir: str = "/work/pdg"
    path: str = pdg_dir + "".join(sep + part for part in rest)
    tokenized: str = pdg_actions._tokenize(path, pdg_dir)
    assert tokenized.startswith(PDG_DIR_TOKEN)
    # Windows writes its own separator as /, so the round trip holds up to that.
    assert os.path.normpath(tokenized.replace(PDG_DIR_TOKEN, pdg_dir, 1)) == os.path.normpath(path)
    assert pdg_actions._tokenize(path, pdg_dir + "/") == tokenized
    if sibling:
        other: str = pdg_dir + sibling + "".join("/" + part for part in rest)
        assert pdg_actions._tokenize(other, pdg_dir) == other
    assert pdg_actions._tokenize("/elsewhere", "/") == "/elsewhere"
    # Houdini writes / even on Windows, so a / path under a folder spelled with \ must still match.
    native_dir: str = os.path.normpath(pdg_dir)
    assert (
        pdg_actions._tokenize(native_dir.replace(os.sep, "/") + "/a.exr", native_dir)
        == PDG_DIR_TOKEN + "/a.exr"
    )


def test_scan_lists_regular_files_except_hidden_payload_and_hip(tmp_path: Path) -> None:
    """Only the item's own outputs are reported, not its payload, the scene link, or hidden files."""
    root: Path = tmp_path / "pdg"
    (root / "geo").mkdir(parents=True)
    (root / "pdgtemp" / "data").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "geo" / "b.bgeo.sc").write_text("b")
    (root / "a.exr").write_text("a")
    (root / ".dotfile").write_text("x")
    (root / ".hidden" / "c").write_text("x")
    (root / "pdgtemp" / "data" / "item.json.gz").write_text("x")
    # The worker links the scene here. A plain file stands in, so the test runs without symlink rights.
    (root / "scene.hip").write_text("hip")
    outputs: List[OutputFile] = pdg_actions._scan_outputs(
        str(root), exclude=(str(root / "pdgtemp"), str(root / "scene.hip"), "")
    )
    assert outputs == [OutputFile(str(root / "a.exr")), OutputFile(str(root / "geo" / "b.bgeo.sc"))]
    assert pdg_actions._scan_outputs(str(tmp_path / "missing")) == []

    locked: List[Path] = []

    def lock(folder: Path) -> None:
        # Windows can't lock a folder with chmod, so deny everyone the right to list it.
        if os.name == "nt":
            subprocess.run(
                ["icacls", folder, "/deny", "*S-1-1-0:(RD)"], check=True, capture_output=True
            )
        else:
            folder.chmod(0)
        locked.append(folder)

    # A folder the scan can't list fails it, so a partial list is never recorded as a success.
    # One inside a hidden or excluded folder is never listed, so it changes nothing.
    try:
        lock(root / ".hidden")
        lock(root / "pdgtemp" / "data")
        assert pdg_actions._scan_outputs(str(root), exclude=(str(root / "pdgtemp"),)) == [
            OutputFile(str(root / "a.exr")),
            OutputFile(str(root / "geo" / "b.bgeo.sc")),
            OutputFile(str(root / "scene.hip")),
        ]
        lock(root / "geo")
        # Root can list any folder, so there the scan can't be made to fail.
        with contextlib.suppress(PermissionError):
            os.listdir(root / "geo")
            pytest.skip("this user can list a locked folder")
        with pytest.raises(PermissionError):
            pdg_actions._scan_outputs(str(root), exclude=(str(root / "pdgtemp"),))
    finally:
        for folder in locked:
            if os.name == "nt":
                subprocess.run(["icacls", folder, "/remove:d", "*S-1-1-0"], check=True)
            else:
                folder.chmod(0o700)


def test_scan_skips_files_older_than_the_item(tmp_path: Path) -> None:
    """Inputs synced before the task started are not outputs, but a file stamped at the start is."""
    # One file before the start, one stamped at the start's time, and one after it.
    for name, seconds in (("old", 1_000_000), ("edge", 1_500_000), ("new", 2_000_000)):
        (tmp_path / name).write_text("x")
        os.utime(tmp_path / name, (seconds, seconds))
    since: int = 1_500_000 * 10**9
    assert pdg_actions._scan_outputs(str(tmp_path), since=since) == [
        OutputFile(str(tmp_path / "edge")),
        OutputFile(str(tmp_path / "new")),
    ]


# The task the run data names, and the IDs that name it there.
ADDRESS: TaskAddress = TaskAddress("farm-1", "queue-2", "job-3", "step-4", "task-4-0")
IDS: Dict[str, str] = dataclasses.asdict(ADDRESS)
STOCK: str = '"__PDG_HYTHON__" "__PDG_SCRIPTDIR__/rop.py" json'

# Small programs the shell items run, so each test sees what a real command saw.
_SCRIPTS: Dict[str, str] = {
    # Print each argument on its own line, then exit with the first one.
    "say.py": "import sys\nprint(*sys.argv[2:], sep='\\n', flush=True)\nsys.exit(int(sys.argv[1]))\n",
    # Write the working directory, the environment and the other arguments to the first one.
    "dump.py": (
        "import json, os, sys\n"
        "with open(sys.argv[1], 'w') as f:\n"
        "    json.dump({'cwd': os.getcwd(), 'env': dict(os.environ), 'argv': sys.argv[2:]}, f)\n"
    ),
    # Print the other arguments, then wait up to 10 seconds for the file named by the first.
    "wait.py": (
        "import os, sys, time\n"
        "print(*sys.argv[2:], sep='\\n', flush=True)\n"
        "deadline = time.time() + 10\n"
        "while not os.path.exists(sys.argv[1]) and time.time() < deadline:\n"
        "    time.sleep(0.05)\n"
        "sys.exit(0 if os.path.exists(sys.argv[1]) else 1)\n"
    ),
    # Create the file named by the argument, in the working directory.
    "write.py": "import sys\nopen(sys.argv[1], 'w').write('x')\n",
}


class FakeStorage:
    """The task storage port, in memory. The worker never stores a payload or reads outputs."""

    def __init__(
        self,
        payload: Optional[Dict[str, Any]] = None,
        *,
        get_error: Optional[Exception] = None,
        put_error: Optional[Exception] = None,
    ) -> None:
        # What it returns or raises, and what it was asked for.
        self.payload: Optional[Dict[str, Any]] = payload
        self.get_error: Optional[Exception] = get_error
        self.put_error: Optional[Exception] = put_error
        self.fetched: List[TaskAddress] = []
        self.published: List[Tuple[TaskAddress, List[OutputFile]]] = []

    def put_payload(self, address: TaskAddress, payload_json: str) -> None:
        raise AssertionError("the worker never stores a payload")

    def get_payload(self, address: TaskAddress) -> Dict[str, Any]:
        self.fetched.append(address)
        if self.get_error:
            raise self.get_error
        return copy.deepcopy(self.payload or {})

    def put_outputs(self, address: TaskAddress, outputs: Sequence[OutputFile]) -> None:
        # The publish is recorded even when it then fails.
        self.published.append((address, list(outputs)))
        if self.put_error:
            raise self.put_error

    def get_outputs(self, address: TaskAddress) -> Optional[List[OutputFile]]:
        raise AssertionError("the worker never reads an output record")


def args(pdg_dir: str, **extra: Any) -> Dict[str, Any]:
    """The run data the adaptor downloads an item with: the task's run data plus the session's folder."""
    return {
        "step": "s",
        "task_index": 0,
        "record_outputs": False,
        **IDS,
        "pdg_dir": pdg_dir,
        **extra,
    }


class Session:
    """One worker session: the adaptor's process downloads and publishes, and the client runs the item."""

    def __init__(self, storage: TaskStorage) -> None:
        self.storage: TaskStorage = storage
        self.runner: PdgWorkItemRunner = PdgWorkItemRunner()
        self.output: str = ""

    def run(self, item_args: Dict[str, Any]) -> int:
        """Run one item and return the exit code the client printed. A failed publish raises."""
        out: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(out):
            client_args: Dict[str, Any]
            outputs_file: str
            client_args, outputs_file = download_pdg_work_item(self.storage, ADDRESS, item_args, [])
            # The client's arguments cross a socket as JSON.
            self.runner.run_work_item(json.loads(json.dumps(client_args)))
        self.output = out.getvalue()
        # Only a line that starts with the marker counts, as in the adaptor.
        codes: List[str] = re.findall(
            "^" + re.escape(EXIT_CODE_MARKER) + r"(-?\d+)", self.output, re.MULTILINE
        )
        assert len(codes) == 1, self.output
        # The adaptor publishes only for a recording step that exited 0.
        if client_args["record_outputs"] and codes[0] == "0":
            publish_pdg_outputs(self.storage, ADDRESS, outputs_file)
        return int(codes[0])


@pytest.fixture(scope="session")
def scripts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The folder holding the helper programs."""
    folder: Path = tmp_path_factory.mktemp("scripts")
    for name, text in _SCRIPTS.items():
        (folder / name).write_text(text)
    return folder


def run_script(scripts: Path, name: str, *arguments: str) -> str:
    """A shell command that runs one helper program with this test's Python."""
    quoted: str = " ".join(f'"{a}"' for a in arguments)
    return f'"{sys.executable}" -I -S "{scripts / name}" {quoted}'


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's own variables start unset, and Houdini is at a known place."""
    for name in RESERVED + ("HIP",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HFS", "/opt/hfs")


@pytest.fixture
def pdg_dir(tmp_path: Path) -> str:
    """The session's work folder, which downloading creates."""
    return str(tmp_path / "pdg")


@settings(max_examples=30, deadline=None)
@given(mappable, st.lists(st.tuples(posix_paths, posix_paths), max_size=3))
def test_downloading_writes_the_mapped_payload_where_pdgs_loader_looks(
    extra: Any, pairs: List[Tuple[str, str]]
) -> None:
    """For any payload and rules, the downloaded file holds the mapped payload where PDG's loader reads it."""
    # The adaptor passes its runtime rules, which carry apply().
    rules: List[RuntimeRule] = [rule(*p, runtime=True) for p in pairs]
    data: Dict[str, Any] = {**payload(), "extra": extra}
    storage: FakeStorage = FakeStorage(data)
    with tempfile.TemporaryDirectory() as tmp:
        # Download into a fresh folder, then read back the file PDG's loader would open.
        work: str = os.path.join(tmp, "pdg")
        client_args: Dict[str, Any]
        outputs_file: str
        client_args, outputs_file = download_pdg_work_item(
            storage, ADDRESS, args(work, scene_file="/s.hip"), rules
        )
        # The payload file PDG's loader opens, and the outputs file the client will write.
        written: Path = Path(work, "pdgtemp", "data", "ropfetch1_7.json.gz")
        loaded: Any = json.loads(gzip.decompress(written.read_bytes()))
    assert loaded == json.loads(json.dumps(first_match(data, rules)))
    assert storage.fetched == [ADDRESS]
    assert client_args == {
        "item_name": "ropfetch1_7",
        "pdg_dir": work,
        "pdg_temp": os.path.join(work, "pdgtemp"),
        "scene_file": "/s.hip",
        "record_outputs": False,
    }
    assert outputs_file == os.path.join(work, "pdgtemp", "data", "ropfetch1_7.outputs.json")


@pytest.mark.parametrize(
    "storage, error, message",
    [
        (FakeStorage({"workitem": {"attributes": {}}}), ValueError, "carries no work item name"),
        (FakeStorage(payload(name="../escape")), ValueError, "is not a single file name"),
        (
            FakeStorage(get_error=PayloadTransferError("ran before its payload was ready")),
            PayloadTransferError,
            "ran before",
        ),
    ],
)
def test_an_item_that_cannot_be_downloaded_fails_before_anything_runs(
    pdg_dir: str, storage: FakeStorage, error: Type[Exception], message: str
) -> None:
    """A task with no name, or no payload, fails, and the client runs nothing.

    Without that, the client would run whatever an earlier attempt left behind.
    """
    with pytest.raises(error, match=message):
        download_pdg_work_item(storage, ADDRESS, args(pdg_dir), [])
    assert not os.path.exists(os.path.join(pdg_dir, "pdgtemp"))
    # The client, given the arguments it would have had, fails the item instead of running it.
    client_args: Dict[str, Any] = {
        "item_name": "ropfetch1_7",
        "pdg_dir": pdg_dir,
        "pdg_temp": os.path.join(pdg_dir, "pdgtemp"),
    }
    out: io.StringIO = io.StringIO()
    with contextlib.redirect_stdout(out):
        PdgWorkItemRunner().run_work_item(client_args)
    assert "Traceback" in out.getvalue() and "ropfetch1_7.json.gz" in out.getvalue()
    assert out.getvalue().rstrip().endswith(f"{EXIT_CODE_MARKER}1")


def test_an_item_without_a_command_fails_with_a_distinct_error(pdg_dir: str) -> None:
    """An item with nothing to run must fail with a reason, not succeed silently."""
    session: Session = Session(FakeStorage(payload(command="")))
    assert session.run(args(pdg_dir)) == 1
    assert "carries no command" in session.output


def test_the_item_environment_has_pdgs_variables(
    pdg_dir: str, tmp_path: Path, scripts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDG's loader and job scripts read these variables and tokens, so each must hold this item's value.

    A session's PDG_TEMP wins, the result server is unset, and the worker's HFS beats an attribute.
    """
    scene: Path = tmp_path / "shot" / "scene.hip"
    scene.parent.mkdir()
    scene.write_text("hip")
    temp: str = str(tmp_path / "session_temp")
    monkeypatch.setenv("PDG_TEMP", temp)
    monkeypatch.setenv("PDG_RESULT_SERVER", "host:1")
    dump: Path = tmp_path / "env.json"
    command: str = run_script(
        scripts, "dump.py", str(dump), "__PDG_ITEM_ID__", "__PDG_ITEM_LABEL__"
    )
    data: Dict[str, Any] = payload(
        command,
        item_id=11,
        attributes={
            "WEDGE": attrib([0.5], flag=2, kind=1),
            "PDG_DIR": attrib(["/evil"], flag=2),
            "HFS": attrib(["/workstation/hfs"], flag=2),
            "__pdg_label": attrib(["hero shot"]),
        },
    )
    data["workitem"]["index"] = 5
    assert Session(FakeStorage(data)).run(args(pdg_dir, scene_file=str(scene))) == 0
    # The session's variables are back once the item is done.
    assert os.environ["PDG_RESULT_SERVER"] == "host:1"
    seen: Dict[str, Any] = json.loads(dump.read_text())
    assert {k: seen["env"].get(k) for k in RESERVED + ("HFS", "HIP", "WEDGE")} == {
        "PDG_ITEM_NAME": "ropfetch1_11",
        "PDG_ITEM_ID": "11",
        "PDG_INDEX": "5",
        "PDG_ITEM_DATA_SOURCE": "0",
        "PDG_TEMP": temp,
        "PDG_DIR": pdg_dir,
        "PDG_OUTPUT_DIR": pdg_dir,
        "PDG_SCRIPTDIR": f"/opt/hfs/houdini/python{sys.version_info[0]}.{sys.version_info[1]}libs/pdgjob",
        "PDG_RESULT_SERVER": None,
        "HFS": "/opt/hfs",
        "HIP": str(scene.parent),
        "WEDGE": "0.5",
    }
    assert seen["argv"] == ["11", "hero shot"]
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(pdg_dir)
    # The scene is linked where the item's hip attribute points. Windows may refuse the link.
    if os.name != "nt":
        assert os.path.realpath(os.path.join(pdg_dir, "scene.hip")) == os.path.realpath(scene)
    # An item with no ID or label leaves the ID token as written and is labeled by its name.
    bare: Dict[str, Any] = {"workitem": {"name": "bare", "command": command}}
    assert Session(FakeStorage(bare)).run(args(pdg_dir)) == 0
    assert json.loads(dump.read_text())["argv"] == ["__PDG_ITEM_ID__", "bare"]


item_exports: st.SearchStrategy[Any] = st.lists(
    st.dictionaries(
        st.sampled_from(["PDGT_A", "PDGT_B", "PDGT_C", "PDG_DIR", "PDG_INDEX"]),
        st.text(alphabet="xyz", min_size=1, max_size=3),
        max_size=3,
    ),
    min_size=1,
    max_size=4,
)


@settings(max_examples=15, deadline=None)
@given(item_exports, st.dictionaries(st.sampled_from(["PDGT_A", "PDGT_Z"]), st.just("base")))
def test_each_item_sees_the_session_environment_plus_its_own_values_only(
    scripts: Path, items: List[Dict[str, str]], baseline: Dict[str, str]
) -> None:
    """For any items in one session, each sees what it would see alone, so nothing leaks between tasks."""
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        # Each example gets its own folder and session baseline.
        for key, value in baseline.items():
            mp.setenv(key, value)
        dump: Path = Path(tmp, "env.json")
        command: str = run_script(scripts, "dump.py", str(dump))
        before: Dict[str, str] = dict(os.environ)
        exported: List[Dict[str, Any]] = [
            payload(command, item_id=i, attributes={k: attrib([v], flag=2) for k, v in e.items()})
            for i, e in enumerate(items)
        ]
        # All the items in one session, then each in a session of its own.
        storage: FakeStorage = FakeStorage()
        shared: Session = Session(storage)
        together: List[Dict[str, str]] = []
        for data in exported:
            storage.payload = data
            shared.run(args(os.path.join(tmp, "pdg")))
            together.append(json.loads(dump.read_text())["env"])
            assert dict(os.environ) == before
        for data, env in zip(exported, together):
            Session(FakeStorage(data)).run(args(os.path.join(tmp, "pdg")))
            assert json.loads(dump.read_text())["env"] == env


@settings(max_examples=30, deadline=None)
@given(st.one_of(st.integers(0, 255), st.none()), st.booleans())
@example(None, False)
def test_the_reported_code_is_the_commands_exit_code(code: Optional[int], no_newline: bool) -> None:
    """For any exit code, the client reports exactly the code the command exited with.

    The command first prints the exit code line itself, which must not end the task with 0.
    Output that ends without a newline must not hide the real code line from the adaptor.
    A command that can't start (None) reports 1 with its traceback, instead of ending the client.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # A Python program, so the same command runs under sh and cmd.exe.
        tail: str = "sys.stdout.write('no newline at end'); " if no_newline else ""
        program: str = f"import sys; print('{EXIT_CODE_MARKER}0'); {tail}sys.exit({code})"
        # No OS starts a command holding a NUL byte, so starting it raises.
        command: str = f'"{sys.executable}" -c "{program}"' if code is not None else "echo \0"
        session: Session = Session(FakeStorage(payload(command)))
        assert session.run(args(os.path.join(tmp, "pdg"))) == (1 if code is None else code)
        assert ("Traceback" in session.output) == (code is None)


@settings(max_examples=25, deadline=None)
@given(st.booleans(), st.integers(0, 3), st.booleans())
@example(True, 0, True)
def test_outputs_are_published_only_for_a_recording_step_that_succeeded(
    scripts: Path, record_outputs: bool, code: int, upload_raises: bool
) -> None:
    """For any step, outcome and storage failure, the outputs go up only after a recording step succeeds.

    A failed publish fails the task, because the next step would read a missing record as no outputs.
    """
    # The command prints one result line and exits with the drawn code.
    result: str = "PDG_RESULT: 7;-1;__PDG_DIR__/a.exr;file/image;0"
    storage: FakeStorage = FakeStorage(
        payload(run_script(scripts, "say.py", str(code), result)),
        put_error=RuntimeError("denied") if upload_raises else None,
    )
    with tempfile.TemporaryDirectory() as tmp:
        item_args: Dict[str, Any] = args(os.path.join(tmp, "pdg"), record_outputs=record_outputs)
        if record_outputs and code == 0 and upload_raises:
            with pytest.raises(RuntimeError, match="denied"):
                Session(storage).run(item_args)
        else:
            assert Session(storage).run(item_args) == code
        # The client writes a record for the adaptor only after a recording step succeeds.
        record: str = os.path.join(tmp, "pdg", "pdgtemp", "data", "ropfetch1_7.outputs.json")
        assert os.path.exists(record) == (record_outputs and code == 0)
    if record_outputs and code == 0:
        assert storage.published == [(ADDRESS, [OutputFile("__PDG_DIR__/a.exr", "file/image")])]
    else:
        assert storage.published == []


def test_a_silent_command_falls_back_to_the_scan(
    pdg_dir: str, tmp_path: Path, scripts: Path
) -> None:
    """A command that names no outputs still reports the files it wrote, but not the linked scene.

    If the record can't be written, the item fails rather than report a success with no outputs.
    """
    # The command writes one file and prints no PDG_RESULT line.
    scene: Path = tmp_path / "scene.hip"
    scene.write_text("hip")
    storage: FakeStorage = FakeStorage(payload(run_script(scripts, "write.py", "out.bgeo.sc")))
    assert Session(storage).run(args(pdg_dir, record_outputs=True, scene_file=str(scene))) == 0
    assert storage.published == [(ADDRESS, [OutputFile("__PDG_DIR__/out.bgeo.sc", "")])]
    # This command exits 0 after putting a folder where the record goes, relative to $PDG_DIR.
    record: str = "pdgtemp/data/ropfetch1_7.outputs.json"
    blocker: str = f'"{sys.executable}" -I -S -c "import os; os.mkdir(\'{record}\')"'
    blocked: FakeStorage = FakeStorage(payload(blocker))
    session: Session = Session(blocked)
    assert session.run(args(pdg_dir, record_outputs=True)) == 1
    assert "Traceback" in session.output and "PdgActions: recorded" not in session.output
    assert os.path.isdir(os.path.join(pdg_dir, record)) and blocked.published == []


def test_the_record_parses_with_the_storage_reader(pdg_dir: str, scripts: Path) -> None:
    """The scheduler reads what the worker writes, so a path tokenized here must come back whole there."""
    lines: Tuple[str, ...] = (
        "PDG_RESULT: 7;-1;__PDG_DIR__/geo/a.bgeo.sc;file/geo;0",
        "PDG_RESULT: 7;-1;__PDG_DIR__-sibling/b.exr;;0",
    )
    storage: S3TaskStorage = S3TaskStorage(s3_client=FakeS3(), bucket="bucket", root_prefix="root")
    storage.put_payload(ADDRESS, json.dumps(payload(run_script(scripts, "say.py", "0", *lines))))
    assert Session(storage).run(args(pdg_dir, record_outputs=True)) == 0
    # The sibling folder shares the prefix but isn't the work folder, so it stays a plain path.
    assert storage.get_outputs(ADDRESS) == [
        OutputFile("__PDG_DIR__/geo/a.bgeo.sc", "file/geo"),
        OutputFile(f"{pdg_dir}-sibling/b.exr", ""),
    ]


# Stand-ins for the parts of Houdini the in-process cook uses. Each writes what it was
# asked to do to a log, and reads how to behave from a config file.
_FAKE_HOUDINI: Dict[str, str] = {
    "_fake_houdini.py": (
        "import json, os\n"
        "DIR = os.environ['FAKE_HOUDINI_DIR']\n"
        "def config():\n"
        "    with open(os.path.join(DIR, 'config.json')) as f:\n"
        "        return json.load(f)\n"
        "def log(event, **details):\n"
        "    with open(os.path.join(DIR, 'log.jsonl'), 'a') as f:\n"
        "        f.write(json.dumps(dict(details, event=event)) + '\\n')\n"
    ),
    "hou.py": (
        "from _fake_houdini import config, log\n"
        "class LoadWarning(Exception): pass\n"
        "class hipFile:\n"
        "    def load(path):\n"
        "        log('hip_load', path=path)\n"
        "        if config().get('load_fails'):\n"
        "            raise RuntimeError('the scene is corrupt')\n"
        "def getenv(name): return ''\n"
        "def hscript(command):\n"
        "    log('hscript', command=command)\n"
        "    return '', ''\n"
    ),
    "pdg/__init__.py": (
        "from _fake_houdini import log\n"
        "class EvaluationContext:\n"
        "    def setGlobalWorkItem(item, flag):\n"
        "        log('global_item', cleared=item is None, flag=flag)\n"
    ),
    "pdgjob/__init__.py": "",
    "pdgjob/nocook.py": "",
    "pdgjob/pdgjson.py": (
        "import os\n"
        "from _fake_houdini import config, log\n"
        "class _File:\n"
        "    def __init__(self, path, tag): self.path, self.tag = path, tag\n"
        "class _WorkItem:\n"
        "    def __init__(self, settings):\n"
        "        self.outputFiles = [_File(p, t) for p, t in settings.get('outputs', [])]\n"
        "        self.hip = settings.get('hip')\n"
        "    def fileAttribValue(self, name):\n"
        "        return _File(self.hip, '') if name == 'hip' and self.hip else None\n"
        "    def localizePath(self, path):\n"
        "        return path.replace('__PDG_DIR__', os.environ['PDG_DIR'])\n"
        "class WorkItem:\n"
        "    def fromJobEnvironment(as_native=False):\n"
        "        name = os.environ['PDG_ITEM_NAME']\n"
        "        path = os.path.join(os.environ['PDG_TEMP'], 'data', name + '.json.gz')\n"
        "        log('load', name=name, cwd=os.getcwd(), as_native=as_native,\n"
        "            payload=os.path.isfile(path))\n"
        "        return _WorkItem(config())\n"
    ),
    "pdgjob/rop.py": (
        "from _fake_houdini import config, log\n"
        "class RopCooker:\n"
        "    hip_modtime = None\n"
        "def cook(work_item):\n"
        "    log('cook', hip_modtime=RopCooker.hip_modtime)\n"
        "    outcome = config().get('cook', 'none')\n"
        "    if outcome == 'raise':\n"
        "        raise RuntimeError('the job script failed')\n"
        "    if outcome.startswith('exit'):\n"
        "        raise SystemExit(int(outcome[4:]))\n"
        "    return {'none': None, 'true': True, 'false': False}[outcome]\n"
    ),
}

# The Houdini client's side of a session: it runs the downloaded items, in order, in one process.
_CLIENT: str = """
import json, sys
from deadline.houdini_adaptor.pdg_actions import PdgWorkItemRunner
runner = PdgWorkItemRunner()
with open(sys.argv[1]) as f:
    items = json.load(f)
for item in items:
    runner.run_work_item(dict(item))
# A real client stays up for the next task, so this one waits until the test is done.
sys.stdin.read()
"""


class HoudiniClientProcess:
    """A Houdini client in a child process, with the stand-ins above in place of Houdini's modules."""

    def __init__(self, root: Path) -> None:
        # Write the stand-ins and the client program into the test's folder.
        self.root: Path = root
        self.fakes: Path = root / "fakes"
        for name, text in _FAKE_HOUDINI.items():
            (self.fakes / name).parent.mkdir(parents=True, exist_ok=True)
            (self.fakes / name).write_text(text)
        (root / "client.py").write_text(_CLIENT)
        self.configure()

    def configure(self, **settings: Any) -> None:
        """Set how the stand-ins behave: the cook's outcome, the item's outputs and hip, a failing load."""
        (self.root / "config.json").write_text(json.dumps(settings))

    def download(
        self, storage: FakeStorage, items: List[Dict[str, Any]], **extra: Any
    ) -> List[Tuple[Dict[str, Any], str]]:
        """Download each payload in this process, as the adaptor does, for the client to run in order."""
        # Distinct payloads get distinct names, and the client reads the list from a file.
        downloaded: List[Tuple[Dict[str, Any], str]] = []
        for item in items:
            storage.payload = item
            downloaded.append(
                download_pdg_work_item(storage, ADDRESS, args(str(self.root / "pdg"), **extra), [])
            )
        (self.root / "items.json").write_text(json.dumps([c for c, _file in downloaded]))
        return downloaded

    def start(self) -> subprocess.Popen:
        """Start the client on the downloaded items. It runs them, then waits for its input to close."""
        # The client gets the fakes first on its path, plus whatever this process was given.
        env: Dict[str, str] = {k: v for k, v in os.environ.items() if k != "HOUDINI_PATHMAP"}
        env["FAKE_HOUDINI_DIR"] = str(self.root)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(self.fakes), env.get("PYTHONPATH")]))
        return subprocess.Popen(
            [sys.executable, str(self.root / "client.py"), str(self.root / "items.json")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(self.root),
        )

    def run(self, storage: FakeStorage, items: List[Dict[str, Any]], **extra: Any) -> List[int]:
        """Run the items in one client, publish the last one as the adaptor would, and return their exit codes."""
        # Closing the client's input at once lets it exit after the items.
        downloaded: List[Tuple[Dict[str, Any], str]] = self.download(storage, items, **extra)
        output: str
        output, _ = self.start().communicate(input="", timeout=120)
        self.output: str = output
        codes: List[int] = [
            int(c) for c in re.findall(re.escape(EXIT_CODE_MARKER) + r"(-?\d+)", output)
        ]
        # The adaptor publishes only for a recording step that exited 0.
        if extra.get("record_outputs") and codes[-1:] == [0]:
            publish_pdg_outputs(storage, ADDRESS, downloaded[-1][1])
        return codes

    def events(self, *kinds: str) -> List[Dict[str, Any]]:
        """What the stand-ins logged, of the given kinds, in order."""
        log: Path = self.root / "log.jsonl"
        entries: List[str] = log.read_text().splitlines() if log.exists() else []
        return [e for e in map(json.loads, entries) if e["event"] in kinds]


@pytest.fixture
def houdini(tmp_path: Path) -> HoudiniClientProcess:
    """A Houdini client process for one test."""
    return HoudiniClientProcess(tmp_path)


def test_the_shell_streams_output_and_collects_results(
    houdini: HoudiniClientProcess, scripts: Path
) -> None:
    """The adaptor reads each line as the command prints it, and its PDG_RESULT lines are its outputs.

    The adaptor waits for the exit code while the client stays up, so that line must arrive at once too.
    """
    # The command prints two lines, then waits for the test to create the gate file.
    gate: Path = houdini.root / "gate"
    result: str = "PDG_RESULT: 7;-1;__PDG_DIR__/geo/a.bgeo.sc;file/geo;0"
    storage: FakeStorage = FakeStorage()
    command: str = run_script(scripts, "wait.py", str(gate), "first", result)
    downloaded: List[Tuple[Dict[str, Any], str]] = houdini.download(
        storage, [payload(command)], record_outputs=True
    )
    client: subprocess.Popen = houdini.start()
    lines: "queue.Queue[str]" = queue.Queue()

    def read() -> None:
        # Read the client's output as it arrives, the way the adaptor's handlers do.
        for line in client.stdout or []:
            lines.put(line.rstrip("\r\n"))

    threading.Thread(target=read, daemon=True).start()
    try:
        # The command waits for the gate, so its line arrives in time only if it was passed on at once.
        while lines.get(timeout=10) != "first":
            pass
        gate.write_text("")
        while lines.get(timeout=10) != f"{EXIT_CODE_MARKER}0":
            pass
    finally:
        assert client.stdin is not None
        client.stdin.close()
        client.wait(timeout=30)
    publish_pdg_outputs(storage, ADDRESS, downloaded[0][1])
    assert storage.published == [(ADDRESS, [OutputFile("__PDG_DIR__/geo/a.bgeo.sc", "file/geo")])]


@pytest.mark.parametrize(
    "command, in_process",
    [
        (STOCK, True),
        ('"__PDG_HYTHON__" "__PDG_SCRIPTDIR__/nocook.py" json', False),
        ('"__PDG_HYTHON__" "__PDG_SCRIPTDIR__/missing.py" json', False),
        ('"__PDG_HYTHON__" "__PDG_SCRIPTDIR__/rop.py" json --batch', False),
        ('"__PDG_PYTHON__" "__PDG_SCRIPTDIR__/rop.py" json', False),
        ("echo __PDG_DIR__", False),
    ],
)
def test_only_a_stock_job_script_cooks_in_process(
    houdini: HoudiniClientProcess, command: str, in_process: bool
) -> None:
    """A stock job script cooks in the session's Houdini, as PDG's own in-process cook calls it.

    Anything else runs through the shell with its tokens resolved, because cooking an
    unknown script in process would mean guessing what it does.
    """
    pdg_dir: str = str(houdini.root / "pdg")
    houdini.configure(outputs=[[f"{pdg_dir}/geo/a.bgeo.sc", "file/geo"], ["__PDG_DIR__/b", None]])
    storage: FakeStorage = FakeStorage()
    houdini.run(storage, [payload(command)], record_outputs=True)
    cooks: List[Dict[str, Any]] = houdini.events("cook")
    if in_process:
        assert len(cooks) == 1
        (load,) = houdini.events("load")
        assert load["as_native"] and load["payload"]
        assert os.path.realpath(load["cwd"]) == os.path.realpath(pdg_dir)
        # The item's outputs are the work item's own output files.
        outputs: List[OutputFile] = [
            OutputFile("__PDG_DIR__/geo/a.bgeo.sc", "file/geo"),
            OutputFile("__PDG_DIR__/b"),
        ]
        assert storage.published == [(ADDRESS, outputs)]
    else:
        assert cooks == []
        (ran,) = [
            line for line in houdini.output.splitlines() if line.startswith("PdgActions: running")
        ]
        assert "__PDG_" not in ran


def test_the_global_work_item_is_cleared_before_each_load(houdini: HoudiniClientProcess) -> None:
    """Otherwise every `@attr` in a warm session reads the first item, so later wedges cook its values."""
    # Two stock items in one client. Each load follows a clear made as PDG's own service clients make it.
    items: List[Dict[str, Any]] = [payload(STOCK, item_id=1), payload(STOCK, item_id=2)]
    assert houdini.run(FakeStorage(), items) == [0, 0]
    calls: List[Tuple[str, Any, Any]] = [
        (e["event"], e.get("cleared"), e.get("flag")) for e in houdini.events("global_item", "load")
    ]
    assert calls == [("global_item", True, False), ("load", None, None)] * 2


@pytest.mark.parametrize(
    "outcome, code",
    [("none", 0), ("true", 0), ("false", 1), ("raise", 1), ("exit1", 1), ("exit0", 0)],
)
def test_the_in_process_outcome_follows_pdgs_cook_contract(
    houdini: HoudiniClientProcess, outcome: str, code: int
) -> None:
    """A job script succeeds by returning nothing or True, and fails by returning False, raising, or exiting non-zero."""
    houdini.configure(cook=outcome)
    assert houdini.run(FakeStorage(), [payload(STOCK)]) == [code]


def test_each_in_process_item_loads_and_remaps_a_fresh_scene(houdini: HoudiniClientProcess) -> None:
    """A scene kept between items keeps their simulations, so each item loads it again, remapped.

    The job script is told the scene is loaded, so it doesn't load it again without the remapping.
    """
    hip: Path = houdini.root / "pdg" / "scene.hip"
    hip.parent.mkdir()
    hip.write_text("hip")
    os.utime(hip, (1_000_000, 1_000_000))
    houdini.configure(hip="__PDG_DIR__/scene.hip")
    # A shell item between the two changes the scene's time, and loads nothing itself.
    touch: str = f'"{sys.executable}" -c "import os; os.utime(\'scene.hip\', (2000000, 2000000))"'
    items: List[Dict[str, Any]] = [
        payload(c, item_id=i) for i, c in enumerate((STOCK, touch, STOCK))
    ]
    assert houdini.run(FakeStorage(), items) == [0, 0, 0]
    loaded: str = str(hip).replace("\\", "/")
    assert [e["path"] for e in houdini.events("hip_load")] == [loaded, loaded]
    assert [e["command"] for e in houdini.events("hscript")].count("varchange") == 2
    assert [e["hip_modtime"] for e in houdini.events("cook")] == [1_000_000, 2_000_000]


def test_a_scene_that_fails_to_load_is_left_to_the_job_script(
    houdini: HoudiniClientProcess,
) -> None:
    """The job script loads the scene itself and reports a bad one in its own words, so the item still cooks."""
    # The scene exists, but loading it raises.
    (houdini.root / "pdg").mkdir()
    (houdini.root / "pdg" / "scene.hip").write_text("hip")
    houdini.configure(hip="__PDG_DIR__/scene.hip", load_fails=True)
    assert houdini.run(FakeStorage(), [payload(STOCK)]) == [0]
    assert len(houdini.events("hip_load")) == 1
    assert [e["hip_modtime"] for e in houdini.events("cook")] == [None]


# -- the node's extra output folders -------------------------------------------


def test_a_silent_command_reports_what_it_wrote_in_an_output_directory(
    pdg_dir: str, tmp_path: Path, scripts: Path
) -> None:
    """Without the scan, a file written there was uploaded but never reached the step below.

    It is reported by the workstation's path, which is how the scheduler and PDG know the folder.
    """
    # The command writes one file in the output folder, as the worker sees it.
    worker: Path = tmp_path / "mnt" / "renders"
    worker.mkdir(parents=True)
    (worker / "old.exr").write_text("before the item")
    os.utime(worker / "old.exr", (0, 0))
    command: str = run_script(scripts, "write.py", str(worker / "beauty" / "b.1.exr"))
    (worker / "beauty").mkdir()
    storage: FakeStorage = FakeStorage(payload(command))
    rules: List[RuntimeRule] = [rule("/studio/renders", str(worker), runtime=True)]
    item_args: Dict[str, Any] = args(
        pdg_dir, record_outputs=True, output_directories=["/studio/renders"]
    )
    out: io.StringIO = io.StringIO()
    with contextlib.redirect_stdout(out):
        client_args, outputs_file = download_pdg_work_item(storage, ADDRESS, item_args, rules)
        PdgWorkItemRunner().run_work_item(json.loads(json.dumps(client_args)))
    publish_pdg_outputs(storage, ADDRESS, outputs_file)
    assert storage.published == [(ADDRESS, [OutputFile("/studio/renders/beauty/b.1.exr", "")])]


# -- the scan's start time ------------------------------------------------------


def _run_item(command: str, item_args: Dict[str, Any], rules: List[RuntimeRule]) -> FakeStorage:
    """Download, run and publish one item, and return its storage."""
    # The adaptor downloads, the client runs the item from JSON, as over the socket.
    storage: FakeStorage = FakeStorage(payload(command))
    out: io.StringIO = io.StringIO()
    with contextlib.redirect_stdout(out):
        client_args, outputs_file = download_pdg_work_item(storage, ADDRESS, item_args, rules)
        runner: PdgWorkItemRunner = PdgWorkItemRunner()
        runner.run_work_item(json.loads(json.dumps(client_args)))
    # The adaptor publishes only after the item exits 0.
    if EXIT_CODE_MARKER + "0" in out.getvalue():
        publish_pdg_outputs(storage, ADDRESS, outputs_file)
    return storage


def test_no_run_leaves_the_start_marker_behind(pdg_dir: str, tmp_path: Path, scripts: Path) -> None:
    """The marker the start time is read from would be an output of the item, or of a later one."""
    # A failed item, then one that lists both folders while it runs and writes nothing.
    worker: Path = tmp_path / "mnt" / "renders"
    worker.mkdir(parents=True)
    rules: List[RuntimeRule] = [rule("/studio/renders", str(worker), runtime=True)]
    item_args: Dict[str, Any] = args(
        pdg_dir, record_outputs=True, output_directories=["/studio/renders"]
    )
    _run_item(f'"{sys.executable}" -I -S -c "raise SystemExit(3)"', item_args, rules)
    listing: str = str(tmp_path / "listing.txt")
    lister: str = (
        f'"{sys.executable}" -I -S -c "import os, sys; '
        f"open(sys.argv[1], 'w').write(repr(os.listdir('.') + os.listdir(sys.argv[2])))\" "
        f'"{listing}" "{worker}"'
    )
    storage: FakeStorage = _run_item(lister, item_args, rules)
    # Neither run left a file in the folders, and the command saw none there.
    assert storage.published == [(ADDRESS, [])]
    assert sorted(os.listdir(pdg_dir)) == ["pdgtemp"] and os.listdir(worker) == []
    assert Path(listing).read_text() == repr(["pdgtemp"])
