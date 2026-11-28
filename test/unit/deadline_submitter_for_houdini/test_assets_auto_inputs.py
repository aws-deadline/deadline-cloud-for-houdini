# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Tests for the ROP's auto-detected inputs: the Skip output folders, keep loaded caches toggle
and the excluded input paths, on small fakes of hou nodes over real files in a temporary folder."""
from __future__ import annotations

import fnmatch
import ntpath
import os
import types
from pathlib import Path
from typing import Any, Iterator, Optional
from unittest import mock

import pytest

from .mock_hou import hou_module as hou
from deadline.client.job_bundle.submission import AssetReferences
from deadline.houdini_submitter.python.deadline_cloud_for_houdini import _assets, submitter
from deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets import (
    EXCLUDED_INPUT_PATHS_ENV_VAR,
    _get_scene_asset_references,
    _parse_files,
    _set_excluded_input_path_rows,
)


def FakeParm(name: str, value: Any = "", raw: Optional[str] = None, frames=None) -> Any:
    """A parm whose unexpanded text is raw, whose value is value, and whose value on each frame
    is frames[frame]."""
    parm = mock.Mock(value=value, raw=str(value) if raw is None else raw, frames=frames or {})
    parm._node = None
    parm.name.return_value = name
    parm.node.side_effect = lambda: parm._node
    parm.path.side_effect = lambda: f"{parm._node.path()}/{name}"
    parm.eval.side_effect = lambda: parm.value
    parm.evalAsString.side_effect = lambda: str(parm.value)
    parm.unexpandedString.side_effect = lambda: parm.raw
    parm.evalAtFrame.side_effect = lambda frame: parm.frames[frame]
    parm.set.side_effect = lambda v: parm.configure_mock(value=v, raw=str(v))
    return parm


class FakeMultiParm(list):
    multiParmInstances = list.copy
    multiParmInstancesCount = list.__len__
    removeMultiParmInstance = list.__delitem__

    def set(self, count: int) -> None:
        # Like Houdini, changing the count keeps the existing rows.
        self[:] = self[:count] + [FakeParm("row#") for _ in range(count - len(self))]

    def values(self) -> list[str]:
        return [p.raw for p in self]


def FakeNode(path: str, type_name: str = "Object/null", **parms: Any) -> Any:
    """A node with a path, a type, parms by name, and the ROPs it renders after."""
    node = mock.Mock(parms={}, ancestors=[])
    node.path.return_value = path
    node.type.return_value.nameWithCategory.return_value = type_name
    node.parm.side_effect = lambda name: node.parms.get(name)
    node.inputAncestors.side_effect = lambda: node.ancestors

    def add(name: str, parm: Any) -> Any:
        parm._node = node
        node.parms[name] = parm
        return parm

    node.add.side_effect = add
    for name, parm in parms.items():
        node.add(name, parm)
    return node


_LISTS = ("input_filenames", "input_directories", "output_directories")


def make_rop(skip_outputs_keep_caches: int, patterns: Optional[list[str]] = None) -> Any:
    rop = FakeNode("/out/deadline_cloud1", "Driver/deadline_cloud")
    rop.add(
        "skip_outputs_keep_caches", FakeParm("skip_outputs_keep_caches", skip_outputs_keep_caches)
    )
    rop.add("excluded_input_paths", FakeMultiParm(FakeParm("row#", p) for p in patterns or []))
    for name in _LISTS + tuple(f"auto_{n}" for n in _LISTS):
        rop.add(name, FakeMultiParm())
    return rop


def touch(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def ref(node_path: str, path: Path, raw: Optional[str] = None, folder: bool = False) -> Any:
    """A file or folder parm on its own node, evaluating to path, which it creates."""
    path.mkdir(parents=True, exist_ok=True) if folder else touch(path)
    return FakeNode(node_path).add("file", FakeParm("file", str(path), raw))


def karma_rop(picture: Path) -> Any:
    return FakeNode("/out/karma1", "Driver/karma", picture=FakeParm("picture", str(picture)))


@pytest.fixture
def scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Patch hou's scene functions. Tests put (parm, ref) pairs in scene["refs"]."""
    hip: Path = tmp_path / "shot" / "scene.hip"
    touch(hip)
    state: dict[str, Any] = {"refs": [], "env": {"HIP": str(hip.parent)}, "hip": hip}
    monkeypatch.delenv(EXCLUDED_INPUT_PATHS_ENV_VAR, raising=False)

    def expand(text: str) -> str:
        for name, value in state["env"].items():
            text = text.replace(f"${name}", value)
        return text

    hou.hipFile.path.return_value = str(hip)
    hou.fileReferences.side_effect = lambda: [(p, r) for p, r in state["refs"]]
    hou.text.expandString.side_effect = expand
    with (
        mock.patch.object(hou, "getenv", lambda k, d="": state["env"].get(k, d), create=True),
        mock.patch.object(hou, "frame", lambda: 1001.0, create=True),
    ):
        yield state
    hou.fileReferences.side_effect = None
    hou.text.expandString.side_effect = None


def file_cache(root: Path, type_name: str = "Sop/filecache::2.0", load: int = 1, **parms) -> Any:
    """A node that loads v3 of cache/sh010, which also holds v1 and v2, from its sopoutput
    parm unless parms says otherwise. Returns its Base Folder parm."""
    base: Path = root / "cache" / "sh010"
    for version in ("v1", "v2", "v3"):
        touch(*(base / version / f"fx.{frame}.bgeo.sc" for frame in (1001, 1002, 1003)))
    frames = {f: str(base / "v3" / f"fx.{int(f)}.bgeo.sc") for f in (1001.0, 1002.0)}
    read = parms or {"sopoutput": FakeParm("sopoutput", frames[1001.0], frames=frames)}
    node = FakeNode("/obj/fx/sim", type_name, loadfromdisk=FakeParm("loadfromdisk", load), **read)
    return node.add("basedir", FakeParm("basedir", str(base)))


def v3_files(root: Path) -> set[str]:
    return {str(root / "cache" / "sh010" / "v3" / f"fx.{f}.bgeo.sc") for f in (1001, 1002, 1003)}


# The toggle and the excluded input paths, alone and together, on one scene.


def build_matrix_scene(scene: dict[str, Any], skip_outputs_keep_caches: int) -> tuple[Any, Path]:
    """A File Cache loading v3 of a versioned cache, a render root holding the job's output in
    v004, a texture, a hand-added file under the cache folder, and the hand-added render root."""
    root: Path = scene["hip"].parent
    touch(*(root / "render" / v / "beauty.1001.exr" for v in ("v001", "v002", "v003")))
    render = ref("/obj/pipeline", root / "render", folder=True)
    wood = ref("/mat/wood", root / "tex" / "wood.png", "$HIP/tex/wood.png")
    scene["refs"] = [
        (file_cache(root), "$HIP/cache/sh010"),
        (render, "$HIP/render"),
        (wood, wood.raw),
    ]
    rop: Any = make_rop(skip_outputs_keep_caches)
    rop.ancestors = [karma_rop(root / "render" / "v004" / "beauty.1001.exr")]
    touch(root / "cache" / "notes" / "plan.txt")
    rop.parm("input_filenames").append(FakeParm("f#", str(root / "cache" / "notes" / "plan.txt")))
    rop.parm("input_directories").append(FakeParm("d#", str(root / "render")))
    return rop, root


def matrix_expected(
    root: Path, hip: Path, skip_outputs_keep_caches: int, pattern: bool, files_on_node: bool
) -> tuple[set, set]:
    files: set[str] = {str(root / "cache" / "notes" / "plan.txt"), str(hip)}
    dirs: set[str] = {str(root / "render")}
    files.add(str(root / "tex" / "wood.png"))
    if not skip_outputs_keep_caches:
        # Without the read files, $HIP/cache also drops the cache v3 the render reads. The
        # auto-detected render root is the same path as the hand-added one.
        return files, dirs | (set() if pattern else {str(root / "cache" / "sh010")})
    # The auto-detected render root holds the output, so only the hand-added one stays.
    # The node's lists keep the sequence as one row, and the bundle has its files.
    files |= (
        {str(root / "cache" / "sh010" / "v3" / "fx.$F4.bgeo.sc")}
        if files_on_node
        else v3_files(root)
    )
    return files, dirs | (set() if pattern else {str(root / "cache" / "sh010")})


@pytest.mark.parametrize("pattern", [False, True], ids=["no pattern", "pattern"])
@pytest.mark.parametrize("skip_outputs_keep_caches", [0, 1], ids=["toggle off", "toggle on"])
def test_the_toggle_and_the_excluded_input_paths_work_alone_and_together(
    scene: dict[str, Any], skip_outputs_keep_caches: int, pattern: bool
) -> None:
    """Off with no pattern lists what mainline lists. Each control alone applies only its own
    rule, and together the v3 files stay under the excluded cache folder. Hand-added paths and
    the hip stay, and an entry the pattern matches leaves the lists on the next Parse Files."""
    rop, root = build_matrix_scene(scene, skip_outputs_keep_caches)
    _parse_files(rop)
    if pattern:
        _set_excluded_input_path_rows(rop, ["$HIP/cache"])
        _parse_files(rop)

    files, dirs = matrix_expected(root, scene["hip"], skip_outputs_keep_caches, pattern, True)
    assert set(rop.parm("input_filenames").values()) == files
    assert set(rop.parm("input_directories").values()) == dirs


@pytest.mark.parametrize("skip_outputs_keep_caches", [0, 1])
def test_save_bundle_rescans_so_a_new_pattern_is_honored(
    scene: dict[str, Any], skip_outputs_keep_caches: int
) -> None:
    """Save Bundle runs Parse Files first, with the toggle on or off, so a pattern added after the
    last parse is in the bundle. With nothing changed, it writes the lists as they were, which is
    what Save Bundle wrote before it re-scanned."""
    rop, root = build_matrix_scene(scene, skip_outputs_keep_caches)
    rop.add("name", FakeParm("name", "job"))
    _parse_files(rop)
    before: AssetReferences = _assets._get_evaluated_asset_references(rop)
    written: list[AssetReferences] = []
    with (
        mock.patch.object(submitter, "create_job_history_bundle_dir", return_value="/b"),
        mock.patch.object(submitter, "_create_job_bundle", lambda n, d, a: written.append(a)),
    ):
        submitter.save_bundle_callback({"node": rop})
        _set_excluded_input_path_rows(rop, ["$HIP/cache"])
        submitter.save_bundle_callback({"node": rop})

    assert written[0] == before
    files, dirs = matrix_expected(root, scene["hip"], skip_outputs_keep_caches, True, False)
    assert (written[1].input_filenames, written[1].input_directories) == (files, dirs)


# Each input is the path its parm evaluates to, with the toggle off or on.


@pytest.mark.parametrize("skip_outputs_keep_caches", [0, 1])
def test_a_hip_file_input_is_stored_as_its_evaluated_path_for_every_caller(
    scene: dict[str, Any], skip_outputs_keep_caches: int
) -> None:
    """The Submit dialog and the headless API attach inputs as they are, and deadline-cloud
    doesn't expand $HIP, so the scan and Parse Files store $HIP/assets/rock.bgeo.sc evaluated."""
    root: Path = scene["hip"].parent
    rock = ref("/obj/rock", root / "assets" / "rock.bgeo.sc", "$HIP/assets/rock.bgeo.sc")
    scene["refs"] = [(rock, rock.raw)]
    rop: Any = make_rop(skip_outputs_keep_caches)

    with mock.patch.object(hou, "node", return_value=rop):
        headless: AssetReferences = submitter.HoudiniSubmitter(rop.path()).get_asset_references(
            submitter.HoudiniSubmitterSettings()
        )
    _parse_files(rop)

    expected: set[str] = {str(scene["hip"]), str(root / "assets" / "rock.bgeo.sc")}
    assert headless.input_filenames == expected
    assert set(rop.parm("input_filenames").values()) == expected


def save_bundle(rop: Any) -> AssetReferences:
    """Run Save Bundle on the ROP and return the asset references it writes into the bundle."""
    rop.parms.setdefault("name", FakeParm("name", "job"))
    written: list[AssetReferences] = []
    with (
        mock.patch.object(submitter, "create_job_history_bundle_dir", return_value="/b"),
        mock.patch.object(submitter, "_create_job_bundle", lambda n, d, a: written.append(a)),
    ):
        submitter.save_bundle_callback({"node": rop})
    return written[0]


def two_nodes_with_frames(root: Path, typed: str, layout: str) -> list[Any]:
    """Nodes a and b, each reading its own frames 1001 to 1003 from geo/ as layout gives them,
    with the ref typed the same on both, as a node-local token or a channel reference allows."""
    nodes: list[Any] = []
    for name in ("a", "b"):
        files = [root / "geo" / layout.format(name=name, f=f) for f in (1001, 1002, 1003)]
        touch(*files)
        frames = {1001.0: str(files[0]), 1002.0: str(files[1])}
        nodes.append(
            FakeNode(f"/obj/{name}").add("file", FakeParm("file", str(files[0]), typed, frames))
        )
    return nodes


@pytest.mark.parametrize(
    ("typed", "layout"),
    [
        pytest.param("$HIP/geo/$OS/$F4.bgeo", "{name}/{f}.bgeo", id="$OS in a folder"),
        pytest.param("$HIP/geo/$OS.$F4.bgeo", "{name}.{f}.bgeo", id="$OS in a file name"),
        pytest.param(
            '$HIP/geo/`chs("../name")`.$F4.bgeo', "{name}.{f}.bgeo", id="a channel reference"
        ),
    ],
)
def test_a_frame_sequence_with_node_local_tokens_gets_only_its_own_nodes_frames(
    scene: dict[str, Any], typed: str, layout: str
) -> None:
    """$OS and a channel reference only expand on their own parm. Expanded anywhere else, the ref
    becomes geo//*.bgeo or geo/*.*.bgeo, which also finds the other node's frames."""
    root: Path = scene["hip"].parent
    a, b = two_nodes_with_frames(root, typed, layout)
    scene["refs"] = [(a, typed), (b, typed)]
    # Outside a parm, $OS expands to nothing.
    scene["env"]["OS"] = ""
    rop: Any = make_rop(0)

    refs: AssetReferences = _get_scene_asset_references(rop)
    _parse_files(rop)

    frames = {
        str(root / "geo" / layout.format(name=n, f=f)) for n in "ab" for f in (1001, 1002, 1003)
    }
    assert refs.input_filenames == {str(scene["hip"])} | frames
    sequences = {str(root / "geo" / layout.format(name=n, f="$F4")) for n in "ab"}
    assert set(rop.parm("input_filenames").values()) == {str(scene["hip"])} | sequences
    assert save_bundle(rop).input_filenames == {str(scene["hip"])} | frames


_ROCK_FRAMES: list[str] = [f"geo/rock.{f}.bgeo" for f in (1001, 1002, 1003)]
_LONG_SEQUENCE: list[str] = [f"cache/sim.{f:04d}.bgeo.sc" for f in range(1, 2001)]
_FX_FRAMES: list[str] = [f"fx.{f}.bgeo" for f in (1001, 1002, 1003)]
_FOLDER_FRAMES: list[str] = [f"v{f}/a.{f}.b" for f in (1001, 1002, 1003)]
_OFFSET_FRAMES: list[str] = [f"v{f}/a.{f + 1}.b" for f in (1001, 1002, 1003)]


@pytest.mark.parametrize(
    ("read", "on_disk", "pattern", "expected", "row"),
    [
        pytest.param(
            _LONG_SEQUENCE[1000:1002],
            _LONG_SEQUENCE,
            None,
            _LONG_SEQUENCE,
            "cache/sim.$F4.bgeo.sc",
            id="2000 frames",
        ),
        pytest.param(_ROCK_FRAMES[:2], _ROCK_FRAMES, "$HIP/geo", [], None, id="the folder"),
        pytest.param(
            _ROCK_FRAMES[:2],
            _ROCK_FRAMES,
            "$HIP/geo/rock.1002.bgeo",
            _ROCK_FRAMES[::2],
            None,
            id="one frame",
        ),
        pytest.param(
            _ROCK_FRAMES[:2],
            _ROCK_FRAMES,
            "$HIP/**/*.100[23].bgeo",
            _ROCK_FRAMES[:1],
            None,
            id="two frames",
        ),
        pytest.param(
            _FX_FRAMES[:2],
            [*_FX_FRAMES, "fx.backup.bgeo", "fx_v2.1001.bgeo", "fx.1001.bgeo.bak"],
            None,
            _FX_FRAMES,
            "fx.$F4.bgeo",
            id="files next to the sequence",
        ),
        pytest.param(
            _FOLDER_FRAMES[:2],
            [*_FOLDER_FRAMES, "v1001/a.1002.b", "v1003/a.1001.b"],
            None,
            _FOLDER_FRAMES,
            "v$F4/a.$F4.b",
            id="frame in the folder and the file",
        ),
        pytest.param(
            _OFFSET_FRAMES[:2],
            [*_OFFSET_FRAMES, "v1001/a.1001.b", "v1002/a.1002.b"],
            None,
            _OFFSET_FRAMES,
            None,
            id="the file one frame ahead of the folder",
        ),
        pytest.param(
            [_ROCK_FRAMES[0], _ROCK_FRAMES[0]],
            [*_ROCK_FRAMES, "geo/rock.backup.bgeo"],
            None,
            _ROCK_FRAMES[:1],
            None,
            id="the same file on adjacent frames",
        ),
    ],
)
def test_a_frame_sequence_is_one_row_on_the_node_and_the_same_files_for_every_caller(
    scene: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    read: list[str],
    on_disk: list[str],
    pattern: Optional[str],
    expected: list[str],
    row: Optional[str],
) -> None:
    """The parm reads read on frames 1001 and 1002. Parse Files writes the sequence as one $F row
    (row), so the parm pane and the .hip stay small, or as its files when one row can't say them.
    The Submit dialog, the headless API and Save Bundle all get the same files, because
    deadline-cloud doesn't expand $F. Files that aren't frames, frames whose folder and file
    numbers don't match, and frames an excluded path matches are left out of all of them."""
    root: Path = scene["hip"].parent
    touch(*(root / name for name in {*read, *on_disk}))
    first, second = (str(root / name) for name in read)
    # Any ref with $F reads a sequence. The parm's values decide which files.
    sequence = FakeNode("/obj/fx").add(
        "file", FakeParm("file", first, "$HIP/sequence.$F4", {1001.0: first, 1002.0: second})
    )
    scene["refs"] = [(sequence, sequence.raw)]
    rop: Any = make_rop(0, patterns=[pattern] if pattern else [])

    dialog: AssetReferences = _get_scene_asset_references(rop)
    dialog_output: str = capsys.readouterr().out
    bundle: AssetReferences = save_bundle(rop)
    bundle_output: str = capsys.readouterr().out
    rop.parmTuple.return_value = None
    with (
        mock.patch.object(hou, "node", return_value=rop),
        mock.patch.object(hou, "playbar", create=True),
        mock.patch.object(submitter, "read_common_job_settings"),
    ):
        houdini_submitter = submitter.HoudiniSubmitter(rop.path())
        settings = houdini_submitter.get_settings()
        headless: AssetReferences = houdini_submitter.get_asset_references(settings)

    hip: str = str(scene["hip"])
    files: set[str] = {hip} | {str(root / name) for name in expected}
    assert dialog.input_filenames == bundle.input_filenames == headless.input_filenames == files
    assert set(settings.input_filenames) == files
    # Save Bundle parses first, so it prints the same lines as the dialog's scan.
    assert bundle_output.startswith(dialog_output)
    left_out: list[str] = [
        line.split()[6] for line in dialog_output.splitlines() if "left out" in line
    ]
    all_frames: set[str] = {str(root / name) for name in (*read, *on_disk)}
    assert left_out == (sorted(all_frames - files) if pattern else [])
    rows: list[str] = sorted({hip, str(root / row)} if row else files)
    assert sorted(rop.parm("input_filenames").values()) == rows
    assert sorted(rop.parm("auto_input_filenames").values()) == rows


@pytest.mark.parametrize("written_by", ["the user", "an older version"])
def test_a_frame_sequence_row_no_scan_wrote_keeps_mainlines_glob(
    scene: dict[str, Any], written_by: str
) -> None:
    """A $F row the user typed, or one an older version wrote as a ref such as
    $HIP/geo/rock.$F4.bgeo, keeps mainline's glob of every match. A Submit check reads an older
    version's lists without parsing first."""
    root: Path = scene["hip"].parent
    names = ("rock.1001.bgeo", "rock.1002.bgeo", "rock.backup.bgeo")
    touch(*(root / "geo" / name for name in names))
    rop: Any = make_rop(0)
    expected: set[str] = {str(root / "geo" / name) for name in names}
    if written_by == "the user":
        rop.parm("input_filenames").append(FakeParm("f#", str(root / "geo" / "rock.$F4.bgeo")))
        references: AssetReferences = save_bundle(rop)
        expected.add(str(scene["hip"]))
    else:
        for list_name in ("input_filenames", "auto_input_filenames"):
            row = FakeParm("f#", raw="$HIP/geo/rock.$F4.bgeo")
            row.eval.side_effect = lambda row=row: hou.text.expandString(row.raw)
            rop.parm(list_name).append(row)
        references = _assets._get_evaluated_asset_references(rop)

    # Mainline's glob joins with os.sep, so on Windows its paths mix separators.
    assert {_assets._path_key(f) for f in references.input_filenames} == {
        _assets._path_key(f) for f in expected
    }


@pytest.mark.parametrize(
    ("separator", "typed_name"),
    [
        pytest.param("/", "Rock", id="forward slashes and another case"),
        pytest.param("/", "rock", id="forward slashes"),
        pytest.param("\\", "rock", id="backslashes"),
    ],
)
def test_on_windows_a_frame_sequence_is_one_row_and_its_files_in_the_parms_form(
    scene: dict[str, Any], monkeypatch: pytest.MonkeyPatch, separator: str, typed_name: str
) -> None:
    """Windows glob matches names without case, gives them in the case on disk, and joins them to
    the folder with a backslash, so C:/proj/geo/Rock.*.bgeo finds C:/proj/geo\\rock.1002.bgeo.
    The files and the node's row keep the parm's form, and other names the glob finds aren't
    frames."""
    path = types.SimpleNamespace(**{n: getattr(ntpath, n) for n in ntpath.__all__})
    path.isfile = lambda f: f.endswith((".bgeo", ".hip")) and "folder" not in f
    path.isdir = lambda f: False
    windows_os = types.SimpleNamespace(path=path, sep="\\", altsep="/", pathsep=";", environ={})
    monkeypatch.setattr(_assets, "os", windows_os)
    on_disk = [f"rock.{f}.bgeo" for f in (1001, 1002, 1003)] + [
        "rock.backup.bgeo",
        "rock.folder.bgeo",
    ]

    def windows_glob(pattern: str) -> list[str]:
        folder, name = pattern.replace("\\", "/").rsplit("/", 1)
        return [ntpath.join(folder, n) for n in on_disk if fnmatch.fnmatch(n.lower(), name.lower())]

    monkeypatch.setattr(_assets.glob, "glob", windows_glob)
    scene["env"]["HIP"] = "C:/proj"

    def parm_path(frame: object) -> str:
        return f"C:/proj/geo/{typed_name}.{frame}.bgeo".replace("/", separator)

    rock = FakeNode("/obj/rock").add(
        "file",
        FakeParm(
            "file",
            parm_path(1001),
            f"$HIP/geo/{typed_name}.$F4.bgeo",
            {1001.0: parm_path(1001), 1002.0: parm_path(1002)},
        ),
    )
    scene["refs"] = [(rock, rock.raw)]
    rop: Any = make_rop(0)

    refs: AssetReferences = _get_scene_asset_references(rop)
    _parse_files(rop)

    hip: str = str(scene["hip"])
    assert refs.input_filenames - {hip} == {parm_path(f) for f in (1001, 1002, 1003)}
    assert set(rop.parm("auto_input_filenames").values()) - {hip} == {parm_path("$F4")}
    assert save_bundle(rop).input_filenames == refs.input_filenames


# The filtered detection, with the toggle on.


def test_a_node_without_the_toggle_parm_is_scanned_with_it_off(scene: dict[str, Any]) -> None:
    folder = ref("/obj/a", scene["hip"].parent / "tex", folder=True)
    scene["refs"] = [(folder, "$HIP/tex")]
    node = FakeNode("/tasks/topnet1/scheduler", "Top/deadline_cloud_scheduler")
    assert _get_scene_asset_references(node).input_directories == {folder.value}


@pytest.mark.parametrize(
    ("input_folder", "outputs", "reason", "on_windows"),
    [
        pytest.param("/proj/render/v4", ["/proj/render/v4"], "is", False, id="equal"),
        pytest.param("/proj/render/v4/", ["/proj/render/v4"], "is", False, id="trailing separator"),
        pytest.param("/proj/render/x/../v4", ["/proj/render/v4"], "is", False, id="dot-dot"),
        pytest.param("/proj/render", ["/proj/render/v4"], "contains", False, id="holds"),
        pytest.param("/proj/render/v1", ["/proj/render"], "is inside", False, id="inside"),
        pytest.param("/proj/render2", ["/proj/render"], None, False, id="sibling sharing a prefix"),
        pytest.param(
            "/proj/render",
            ["/proj/render/v4", "/proj/a/x", "/proj/render/v5"],
            "contains",
            False,
            id="the first of several outputs",
        ),
        pytest.param("/proj/tex", ["/proj"], None, False, id="inside $HIP"),
        pytest.param("/job/tex", ["/job"], None, False, id="inside $JOB"),
        pytest.param("/hipdir/tex", ["/hipdir"], None, False, id="inside the hip's folder"),
        pytest.param(
            r"c:\Proj\Render\sh010",
            ["C:/proj/render/sh010"],
            "is",
            True,
            id="windows case and slashes",
        ),
        pytest.param(
            r"c:\Proj\render", ["C:/proj/Render/sh010/v004"], "contains", True, id="windows holds"
        ),
        pytest.param(r"C:\proj\render2", ["c:/proj/render"], None, True, id="windows sibling"),
    ],
)
def test_an_input_folder_that_overlaps_an_output_is_left_out_and_printed_with_why(
    scene: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    input_folder: str,
    outputs: list[str],
    reason: Optional[str],
    on_windows: bool,
) -> None:
    """An input folder that is, holds, or is inside an output is left out, and the first output
    it overlaps, listed first here, is printed. A folder inside an output that is $HIP, $JOB or
    the .hip's folder is kept, because default outputs such as $HIP/$OS.$F.bgeo.sc write
    straight into those."""
    scene["env"].update(HIP="/proj", JOB="/job")
    monkeypatch.setattr(hou.hipFile.path, "return_value", "/hipdir/scene.hip")
    if on_windows:
        monkeypatch.setattr(_assets, "os", types.SimpleNamespace(path=ntpath, sep="\\"))
    refs = AssetReferences(input_directories={input_folder}, output_directories=set(outputs))

    _assets._drop_inputs_in_outputs(refs, {_assets._path_key(input_folder): "/obj/a/file"})

    out: str = capsys.readouterr().out
    assert refs.input_directories == (set() if reason else {input_folder})
    assert out == (
        f"Deadline Cloud: left out input directory {input_folder} (from /obj/a/file): "
        f"it {reason} the output directory {_assets._path_key(outputs[0])}\n"
        if reason
        else ""
    )


def test_toggle_on_the_submit_path_gets_the_paths_each_parm_reads(scene: dict[str, Any]) -> None:
    """The headless and Submit dialog scan gives an expression folder as its evaluated path, not
    a ref that widens on the ROP, and a $F sequence as each of its existing frames."""
    root: Path = scene["hip"].parent
    cache = ref("/obj/p", root / "cache" / "sh010", '$HIP/cache/`chs("shot")`', folder=True)
    frames: list[Path] = [root / "geo" / f"rock.{f}.bgeo" for f in (1001, 1002, 1003)]
    touch(*frames, root / "geo" / "rock_old.bgeo")
    rock = ref("/obj/rock", frames[0], "$HIP/geo/rock.$F.bgeo")
    rock.frames = {1001.0: str(frames[0]), 1002.0: str(frames[1])}
    scene["refs"] = [(cache, cache.raw), (rock, rock.raw)]
    rop: Any = make_rop(1)

    with mock.patch.object(hou, "node", return_value=rop):
        refs: AssetReferences = submitter.HoudiniSubmitter(rop.path()).get_asset_references(
            submitter.HoudiniSubmitterSettings()
        )

    assert refs.input_directories == {cache.value}
    assert refs.input_filenames == {str(scene["hip"])} | {str(p) for p in frames}


@pytest.mark.parametrize(
    ("frames", "on_disk", "expected", "sequence"),
    [
        pytest.param(
            ("fx.1001.bgeo", "fx.1002.bgeo"),
            ["fx.1003.bgeo", "fx.backup.bgeo", "fx_v2.1001.bgeo"],
            ["fx.1001.bgeo", "fx.1002.bgeo", "fx.1003.bgeo"],
            "fx.$F4.bgeo",
            id="other files next to the sequence",
        ),
        pytest.param(
            ("a.9.b", "a.10.b"), ["a.11.b"], ["a.9.b", "a.10.b", "a.11.b"], "a.$F1.b", id="width"
        ),
        pytest.param(
            ("v1001/a.1001.b", "v1002/a.1002.b"),
            ["v1003/a.1003.b", "v1001/a.1002.b"],
            ["v1001/a.1001.b", "v1002/a.1002.b", "v1003/a.1003.b"],
            "v$F4/a.$F4.b",
            id="frame in the folder and the file",
        ),
        pytest.param(
            ("a.1001_v2.b", "a.1002_v2.b"),
            [],
            ["a.1001_v2.b", "a.1002_v2.b"],
            "a.${F4}_v2.b",
            id="a name right after the frame",
        ),
        pytest.param(
            ("[v3]/a.9.b", "[v3]/a.10.b"),
            [],
            ["[v3]/a.9.b", "[v3]/a.10.b"],
            "[v3]/a.$F1.b",
            id="glob chars",
        ),
        pytest.param(
            ("a.-001.b", "a.0000.b"),
            ["a.0001.b"],
            ["a.-001.b", "a.0000.b", "a.0001.b"],
            "a.$F3.b",
            id="negative",
        ),
        pytest.param(
            ("a_left.b", "a_right.b"), ["a_mid.b"], ["a_left.b", "a_right.b"], None, id="text"
        ),
        pytest.param(("static.b", "static.b"), ["static_old.b"], ["static.b"], None, id="one file"),
    ],
)
def test_toggle_on_a_frame_sequence_is_every_existing_frame_and_nothing_else(
    scene: dict[str, Any],
    frames: tuple[str, str],
    on_disk: list[str],
    expected: list[str],
    sequence: Optional[str],
) -> None:
    """Every number that changes between two frames is a frame number, and the numbers keep the
    offsets they have on the current frame. The sequence is one path with $F and the frame width,
    and a path whose text changes, or doesn't change, is each file as itself."""
    root: Path = scene["hip"].parent
    touch(*(root / name for name in [*frames, *on_disk]))
    read = FakeParm(
        "file", "", frames={1001.0: str(root / frames[0]), 1002.0: str(root / frames[1])}
    )
    files = {str(root / name) for name in expected}
    assert _assets._get_frame_sequences(read) == (
        {str(root / sequence): files} if sequence else {f: {f} for f in files}
    )


@pytest.mark.parametrize(
    ("type_name", "load", "read_parm", "attached"),
    [
        ("Sop/filecache", 1, "sopoutput", True),
        ("Sop/rbdio::2.0", 1, "sopoutput", True),
        ("Sop/vellumio::2.0", 1, "sopoutput", True),
        ("Sop/filecache", 1, "file", True),
        ("Sop/filecache", 1, "static", True),
        ("Sop/filecache::2.0", 0, "sopoutput", False),
        ("Sop/file", 1, "sopoutput", False),
        ("Sop/filecache::2.0", 1, "other", False),
    ],
)
def test_toggle_on_attaches_only_the_files_a_loading_file_cache_reads(
    scene: dict[str, Any], type_name: str, load: int, read_parm: str, attached: bool
) -> None:
    """A File Cache, RBD I/O or Vellum I/O node with Load from Disk on attaches the frames it
    reads from sopoutput, or file on the first File Cache version, and no other version."""
    root: Path = scene["hip"].parent
    v3 = sorted(v3_files(root))
    # A path that is the same on every frame reads one file, not a sequence.
    static: bool = read_parm == "static"
    name: str = "file" if static else read_parm
    frames = {1001.0: v3[0], 1002.0: v3[0 if static else 1]}
    basedir = file_cache(root, type_name, load, **{name: FakeParm(name, v3[0], frames=frames)})
    scene["refs"] = [(basedir, "$HIP/cache/sh010")]

    refs: AssetReferences = _get_scene_asset_references(make_rop(1))

    read: set[str] = {v3[0]} if static else set(v3)
    assert refs.input_filenames == {str(scene["hip"])} | (read if attached else set())


# The excluded input paths, which apply with the toggle off and on.


@pytest.mark.parametrize("skip_outputs_keep_caches", [0, 1])
def test_a_pattern_leaves_out_inputs_at_or_under_it_and_prints_each(
    scene: dict[str, Any], capsys, monkeypatch: pytest.MonkeyPatch, skip_outputs_keep_caches: int
) -> None:
    """Patterns come from the node and the env var, separated by os.pathsep, and blank ones are
    ignored. Each ref matches as its parm evaluated it, so $HIP/render/sh010 matches the ref
    $HIP/render/`chs("shot")`, and $HIP/cache_old stays."""
    root: Path = scene["hip"].parent
    render = ref("/obj/p", root / "render" / "sh010", '$HIP/render/`chs("shot")`', folder=True)
    old = ref("/obj/old", root / "cache_old", "$HIP/cache_old", folder=True)
    sim = ref("/obj/sim", root / "cache" / "a.bgeo", "$HIP/cache/a.bgeo")
    scene["refs"] = [(render, render.raw), (old, old.raw), (sim, sim.raw)]
    monkeypatch.setenv(EXCLUDED_INPUT_PATHS_ENV_VAR, os.pathsep.join(["", " $HIP/cache "]))
    rop: Any = make_rop(skip_outputs_keep_caches, patterns=["$HIP/render/sh010", "  "])

    refs: AssetReferences = _get_scene_asset_references(rop)

    assert refs.input_directories == {old.value}
    assert refs.input_filenames == {str(scene["hip"])}
    assert capsys.readouterr().out.splitlines() == [
        f"Deadline Cloud: left out input file {sim.value} (from /obj/sim/file): it is at or under "
        f"the excluded input path {root}/cache",
        f"Deadline Cloud: left out input directory {render.value} (from /obj/p/file): it is at "
        f"or under the excluded input path {root}/render/sh010",
    ]


@pytest.mark.parametrize(
    ("other_on_disk", "pattern", "left_out"),
    [
        pytest.param(True, "$HIP/geo/cache_*", 0, id="one value matches"),
        pytest.param(True, "$HIP/geo/*", 2, id="every value matches"),
        pytest.param(False, "$HIP/geo/cache_*", 1, id="the other value doesn't exist"),
    ],
)
def test_a_shared_ref_is_left_out_only_when_every_existing_value_matches(
    scene: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    other_on_disk: bool,
    pattern: str,
    left_out: int,
) -> None:
    """With refs kept, as the PDG scheduler scans, $HIP/geo/$OS.bgeo.sc held by two nodes is left
    out only when both values match, and otherwise the console names the value that didn't. A
    value that doesn't exist didn't make the ref an input, so it can't keep the ref."""
    root: Path = scene["hip"].parent
    typed = "$HIP/geo/$OS.bgeo.sc"
    sim = ref("/obj/cache_sim", root / "geo" / "cache_sim.bgeo.sc")
    rock_path: Path = root / "geo" / "rock.bgeo.sc"
    rock = (
        ref("/obj/rock", rock_path)
        if other_on_disk
        else FakeNode("/obj/rock").add("file", FakeParm("file", str(rock_path)))
    )
    scene["refs"] = [(sim, typed), (rock, typed)]

    refs: AssetReferences = _get_scene_asset_references(
        make_rop(0, patterns=[pattern]), keep_refs=True
    )

    out: str = capsys.readouterr().out
    dropped: bool = left_out > 0
    assert refs.input_filenames == {str(scene["hip"])} | (set() if dropped else {typed})
    assert out.count("left out input file") == left_out
    kept = (
        f"kept input file {typed}, because these paths match no excluded input path: {rock.value}"
    )
    assert (kept in out) is not dropped


def test_on_windows_the_hip_and_file_cache_files_are_found_in_any_slash_or_case(
    scene: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A parm can name the scene file or a File Cache's file with backslashes or another case.
    Both still count as the hip and as a File Cache file, so a folder pattern keeps them."""
    hou.hipFile.path.return_value = "C:/proj/scene.hip"
    loaded = "C:/Proj/Cache/v3/a.bgeo"
    file_cache = FakeNode(
        "/obj/fc",
        "Sop/filecache",
        loadfromdisk=FakeParm("loadfromdisk", 1),
        file=FakeParm("file", loaded, frames={1001.0: loaded, 1002.0: loaded}),
    )
    refs = AssetReferences(
        input_filenames={"C:/proj/scene.hip", r"C:\Proj\Scene.hip", r"C:\proj\cache\v3\a.bgeo"}
    )
    path = types.SimpleNamespace(**{n: getattr(ntpath, n) for n in ntpath.__all__})
    path.isfile = lambda f: True
    windows_os = types.SimpleNamespace(path=path, sep="\\", altsep="/", pathsep=";", environ={})
    with mock.patch.object(_assets, "os", windows_os):
        cache_files = _assets._add_file_cache_files({"/obj/fc": file_cache}, refs, {}, {})
        _assets._drop_excluded_inputs(
            make_rop(1, patterns=["C:/proj"]), refs, {}, {}, cache_files, False
        )

    assert refs.input_filenames == {
        "C:/proj/scene.hip",
        r"C:\Proj\Scene.hip",
        r"C:\proj\cache\v3\a.bgeo",
        loaded,
    }
    assert capsys.readouterr().out.count("kept the scene file") == 2


def test_patterns_keep_the_scene_file_and_outputs_and_match_usd_dependencies_as_paths(
    scene: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root: Path = scene["hip"].parent
    layer = ref("/stage/usd", root / "a.usda", "$HIP/a.usda")
    rock: str = str(root / "assets" / "rock.bgeo.sc")
    scene["refs"] = [(layer, layer.raw)]
    rop: Any = make_rop(0, patterns=["$HIP/**"])
    rop.ancestors = [karma_rop(root / "render" / "beauty.exr")]

    with mock.patch.object(_assets, "_get_usd_asset_references", return_value=({rock}, set(), [])):
        refs: AssetReferences = _get_scene_asset_references(rop)

    assert refs.input_filenames == {str(scene["hip"])}
    assert refs.output_directories == {str(root / "render")}
    out: str = capsys.readouterr().out
    assert f"kept the scene file {scene['hip']}, which every job needs" in out
    assert f"left out input file {rock} (from the scene)" in out


@pytest.mark.parametrize(
    ("rows", "written"),
    [
        (["$HIP/cache", "$HIP/render"], []),
        (["$HIP/cache", "  ", "$HIP/render "], []),
        (["$HIP/cache", "$HIP/renders"], ["$HIP/renders"]),
        (["$HIP/cache", "$HIP/render", "**/tmp"], ["**/tmp"]),
        (["$HIP/cache"], []),
    ],
)
def test_saving_rows_is_one_undo_entry_that_writes_only_the_changed_rows(rows, written) -> None:
    """The box saves on every keystroke, so a save is one undo entry, or none if nothing changed."""
    rop = make_rop(0, patterns=["$HIP/cache", "$HIP/render"])
    old_rows = list(rop.parm("excluded_input_paths"))
    hou.undos.group.reset_mock()

    _set_excluded_input_path_rows(rop, rows)

    expected = [r.strip() for r in rows if r.strip()]
    assert rop.parm("excluded_input_paths").values() == expected
    changed: bool = expected != ["$HIP/cache", "$HIP/render"]
    assert hou.undos.group.call_args_list == (
        [mock.call("Edit excluded input paths")] if changed else []
    )
    assert [
        r.set.call_args.args[0] for r in rop.parm("excluded_input_paths") if r.set.called
    ] == written
    assert all(r in old_rows for r in rop.parm("excluded_input_paths")[: len(old_rows)])
