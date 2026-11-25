# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Tests for the Deadline Cloud ROP's Filter Auto-Detected Inputs toggle.

The scene is built from small fakes of hou.Node and hou.Parm, over real folders and files in a
temporary directory, so each test runs the scan the way Houdini would.
"""
from __future__ import annotations

import ntpath
import os
import re
import types
from pathlib import Path
from typing import Any, Iterator, Optional
from unittest import mock

import pytest

from .mock_hou import hou_module as hou
from deadline.client.job_bundle.submission import AssetReferences
from deadline.houdini_submitter.python.deadline_cloud_for_houdini import _assets, submitter
from deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets import (
    _get_scene_asset_references,
    _parse_files,
)


class FakeParm:
    """A string or toggle parm. raw is what unexpandedString() returns and value what it evaluates
    to; frames, when set, maps a frame to the value evalAtFrame() returns."""

    def __init__(
        self,
        name: str,
        value: Any = "",
        raw: Optional[str] = None,
        node: Optional["FakeNode"] = None,
        frames: Optional[dict[float, str]] = None,
    ) -> None:
        # Hold the parm's text, its value, its node, and its value per frame.
        self._name: str = name
        self.value: Any = value
        self.raw: str = str(value) if raw is None else raw
        self._node: Optional[FakeNode] = node
        self._frames: dict[float, str] = frames or {}

    def name(self) -> str:
        return self._name

    def node(self) -> Optional["FakeNode"]:
        return self._node

    def path(self) -> str:
        return f"{self._node.path() if self._node else ''}/{self._name}"

    def eval(self) -> Any:
        return self.value

    def evalAsString(self) -> str:
        return str(self.value)

    def unexpandedString(self) -> str:
        return self.raw

    def evalAtFrame(self, frame: float) -> str:
        return self._frames[frame]

    def set(self, value: str) -> None:
        self.value = value
        self.raw = value


class FakeMultiParm:
    """A multiparm list whose instances are FakeParms holding literal strings."""

    def __init__(self, name: str, values: Optional[list[Any]] = None) -> None:
        self._name: str = name
        self.instances: list[FakeParm] = [
            v if isinstance(v, FakeParm) else FakeParm(f"{name}#", v) for v in (values or [])
        ]

    def multiParmInstances(self) -> list[FakeParm]:
        return list(self.instances)

    def multiParmInstancesCount(self) -> int:
        return len(self.instances)

    def removeMultiParmInstance(self, index: int) -> None:
        del self.instances[index]

    def set(self, count: int) -> None:
        self.instances = [FakeParm(f"{self._name}#") for _ in range(count)]

    def values(self) -> list[str]:
        return [p.unexpandedString() for p in self.instances]


class FakeNode:
    """A node with a type name, a path, and parms by name."""

    def __init__(self, path: str, type_name: str, parms: Optional[dict[str, Any]] = None) -> None:
        # Hold the node's path, its type, its parms, and the ROPs it renders after.
        self._path: str = path
        self._type: mock.Mock = mock.Mock()
        self._type.nameWithCategory.return_value = type_name
        self.parms: dict[str, Any] = {}
        for parm_name, parm in (parms or {}).items():
            self.add(parm_name, parm)
        self.ancestors: list[FakeNode] = []

    def add(self, parm_name: str, parm: Any) -> Any:
        if isinstance(parm, FakeParm):
            parm._node = self
        self.parms[parm_name] = parm
        return parm

    def path(self) -> str:
        return self._path

    def type(self) -> mock.Mock:
        return self._type

    def parm(self, parm_name: str) -> Any:
        return self.parms.get(parm_name)

    def inputAncestors(self) -> list["FakeNode"]:
        return self.ancestors


def make_rop(filter_inputs: int, excluded: Optional[list[Any]] = None) -> FakeNode:
    """A Deadline Cloud ROP with the filter toggle set, and empty Job Attachments lists."""
    # The toggle, the Excluded Inputs list, and the Job Attachments lists the scan reads.
    rop: FakeNode = FakeNode("/out/deadline_cloud1", "Driver/deadline_cloud")
    rop.add("filter_auto_inputs", FakeParm("filter_auto_inputs", filter_inputs))
    rop.add("excluded_inputs", FakeMultiParm("excluded_input", excluded))
    for list_name in (
        "input_filenames",
        "input_directories",
        "output_directories",
        "auto_input_filenames",
        "auto_input_directories",
        "auto_output_directories",
    ):
        rop.add(list_name, FakeMultiParm(list_name))
    return rop


def folder_parm(node_path: str, parm_name: str, path: Path, raw: Optional[str] = None) -> FakeParm:
    """A folder parm on its own node, evaluating to path. Creates the folder."""
    path.mkdir(parents=True, exist_ok=True)
    node: FakeNode = FakeNode(node_path, "Object/null")
    return node.add(parm_name, FakeParm(parm_name, str(path), raw=raw))


def karma_rop(picture: Path) -> FakeNode:
    """A Karma ROP that writes picture."""
    return FakeNode("/out/karma1", "Driver/karma", {"picture": FakeParm("picture", str(picture))})


def touch(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


@pytest.fixture
def scene(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Patch hou's scene functions. Tests put (parm, ref) pairs in scene["refs"]."""
    # A saved .hip in $HIP, the scene's file references, $HIP and $JOB, and the current frame.
    hip: Path = tmp_path / "shot" / "scene.hip"
    touch(hip)
    state: dict[str, Any] = {"refs": [], "env": {"HIP": str(hip.parent)}, "hip": hip}
    hou.hipFile.path.return_value = str(hip)
    hou.fileReferences.side_effect = lambda: list(state["refs"])
    with (
        mock.patch.object(
            hou, "getenv", lambda key, default="": state["env"].get(key, default), create=True
        ),
        mock.patch.object(hou, "frame", lambda: 1001.0, create=True),
    ):
        yield state
    hou.fileReferences.side_effect = None


def test_toggle_off_keeps_todays_unexpanded_refs_and_overlapping_inputs(
    scene: dict[str, Any],
) -> None:
    """With the toggle off, an input folder that holds the job's output is kept as its unexpanded
    ref, exactly as before. Catches a filter that runs, or stores evaluated paths, when off."""
    # GIVEN
    root: Path = scene["hip"].parent
    render_root: FakeParm = folder_parm(
        "/obj/pipeline", "render_root", root / "render" / "sh010", raw='$HIP/render/`chs("shot")`'
    )
    scene["refs"] = [(render_root, render_root.raw)]
    rop: FakeNode = make_rop(0, excluded=[str(root / "render" / "sh010")])
    rop.ancestors = [karma_rop(root / "render" / "sh010" / "v004" / "beauty.1001.exr")]

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(rop)

    # THEN
    assert refs.input_directories == {'$HIP/render/`chs("shot")`'}
    assert refs.output_directories == {str(root / "render" / "sh010" / "v004")}


def test_toggle_on_drops_an_input_folder_that_contains_an_output_and_keeps_its_sibling(
    scene: dict[str, Any],
) -> None:
    """With the toggle on, render/sh010 (holding the output render/sh010/v004 and every earlier
    version) is left out, render/sh020 is kept, and kept folders are the paths each evaluated to
    on its own parm, not refs that widen to render/ when evaluated on the ROP."""
    # GIVEN
    root: Path = scene["hip"].parent
    sh010: FakeParm = folder_parm(
        "/obj/pipeline", "render_root", root / "render" / "sh010", raw='$HIP/render/`chs("shot")`'
    )
    sh020: FakeParm = folder_parm(
        "/obj/other", "render_root", root / "render" / "sh020", raw='$HIP/render/`chs("shot")`'
    )
    touch(root / "render" / "sh010" / "v001" / "beauty.1001.exr")
    scene["refs"] = [(sh010, sh010.raw), (sh020, sh020.raw)]
    rop: FakeNode = make_rop(1)
    rop.ancestors = [karma_rop(root / "render" / "sh010" / "v004" / "beauty.1001.exr")]

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(rop)

    # THEN
    assert refs.input_directories == {str(root / "render" / "sh020")}
    assert refs.output_directories == {str(root / "render" / "sh010" / "v004")}


@pytest.mark.parametrize(
    ("input_folder", "output_file", "dropped"),
    [
        pytest.param("render/v004", "render/v004/beauty.exr", True, id="equals an output"),
        pytest.param("render/v004/", "render/v004/beauty.exr", True, id="trailing separator"),
        pytest.param("render/x/../v004", "render/v004/beauty.exr", True, id="dot-dot segment"),
        pytest.param("render", "render/v004/beauty.exr", True, id="contains an output"),
        pytest.param("render/v001", "render/beauty.exr", True, id="inside an output"),
        pytest.param("render2", "render/beauty.exr", False, id="sibling sharing a prefix"),
        pytest.param("tex", "beauty.exr", False, id="inside $HIP when $HIP is the output"),
        pytest.param("textures", "render/beauty.exr", False, id="unrelated folder"),
    ],
)
def test_toggle_on_output_overlap_rule(
    scene: dict[str, Any], input_folder: str, output_file: str, dropped: bool
) -> None:
    """Each case of the overlap rule on normalized paths: equal, containing, and inside an output
    are left out; a prefix-only sibling is kept; and a folder inside $HIP is kept when the output
    writes straight into $HIP, as the Geometry ROP's default path does."""
    # GIVEN
    root: Path = scene["hip"].parent
    (root / "render" / "x").mkdir(parents=True)
    folder: FakeParm = folder_parm("/obj/a", "dir", root / input_folder)
    folder.value = str(root) + os.sep + input_folder
    scene["refs"] = [(folder, "$HIP/" + input_folder)]
    rop: FakeNode = make_rop(1)
    rop.ancestors = [karma_rop(root / output_file)]

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(rop)

    # THEN
    assert (refs.input_directories == set()) is dropped


@pytest.mark.parametrize(
    ("input_folder", "output_folder", "excluded", "reason"),
    [
        pytest.param("render", "render/v004", False, "contains the output directory {out}"),
        pytest.param("render/v004", "render/v004", False, "is the output directory {out}"),
        pytest.param("render/v001", "render", False, "is inside the output directory {out}"),
        pytest.param("tex", "render", True, "is in the Excluded Inputs list"),
    ],
)
def test_toggle_on_reports_each_left_out_folder_with_its_parm_and_reason(
    scene: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    input_folder: str,
    output_folder: str,
    excluded: bool,
    reason: str,
) -> None:
    """Each left-out folder is printed with its path, its source parm, and why, for each reason a
    folder is left out, so the artist can see what the job leaves out. Catches a filter that drops
    folders silently, or prints the wrong reason."""
    # GIVEN
    root: Path = scene["hip"].parent
    folder: FakeParm = folder_parm("/obj/pipeline", "render_root", root / input_folder)
    scene["refs"] = [(folder, "$HIP/" + input_folder)]
    rop: FakeNode = make_rop(1, excluded=[str(root / input_folder)] if excluded else None)
    rop.ancestors = [karma_rop(root / output_folder / "beauty.exr")]

    # WHEN
    _get_scene_asset_references(rop)

    # THEN
    out: str = capsys.readouterr().out
    expected: str = (
        f"left out input directory {root / input_folder} (from /obj/pipeline/render_root): it "
        + reason.format(out=root / output_folder)
    )
    assert expected in out


def test_toggle_on_reports_each_excluded_file_with_its_parm(
    scene: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """An excluded file is printed with its path, its source parm, and why. Catches a file that is
    left out silently."""
    # GIVEN
    root: Path = scene["hip"].parent
    image: Path = root / "image.png"
    touch(image)
    node: FakeNode = FakeNode("/obj/b", "Object/null")
    image_parm: FakeParm = node.add("file", FakeParm("file", str(image), raw="$HIP/image.png"))
    scene["refs"] = [(image_parm, "$HIP/image.png")]

    # WHEN
    _get_scene_asset_references(make_rop(1, excluded=["$HIP/image.png"]))

    # THEN
    assert (
        f"left out input file {image} (from /obj/b/file): it is in the Excluded Inputs list"
        in capsys.readouterr().out
    )


def make_file_cache(
    root: Path, load: int, type_name: str = "Sop/filecache::2.0"
) -> tuple[FakeNode, FakeParm]:
    """A File Cache, or a node of type_name with the same parms, that loads version v3 of a frame
    sequence under its Base Folder cache/sh010, which also holds v1 and v2. Returns the node and
    its Base Folder parm."""
    # Three versions on disk, and a file of another kind next to v3's frames.
    base: Path = root / "cache" / "sh010"
    for version in ("v1", "v2", "v3"):
        for frame in (1001, 1002, 1003):
            touch(base / "fx" / version / f"fx_{version}.{frame}.bgeo.sc")
    touch(base / "fx" / "v3" / "notes.txt")
    frames: dict[float, str] = {
        f: str(base / "fx" / "v3" / f"fx_v3.{int(f)}.bgeo.sc") for f in (1001.0, 1002.0)
    }
    node: FakeNode = FakeNode(
        "/obj/fx/fx_sim",
        type_name,
        {
            "loadfromdisk": FakeParm("loadfromdisk", load),
            "sopoutput": FakeParm("sopoutput", frames[1001.0], frames=frames),
            "basedir": FakeParm("basedir", str(base), raw='`chs("/obj/pipeline/cache_root")`'),
        },
    )
    return node, node.parm("basedir")


@pytest.mark.parametrize(
    "type_name", ["Sop/filecache", "Sop/filecache::2.0", "Sop/rbdio::2.0", "Sop/vellumio::2.0"]
)
def test_toggle_on_attaches_the_loaded_cache_version_when_its_base_folder_is_excluded(
    scene: dict[str, Any], type_name: str
) -> None:
    """A loading File Cache's frames of the version it reads are attached as files, and so are
    those of RBD I/O and Vellum I/O, which have the same Load from Disk and read path parms. With
    its Base Folder excluded, the render keeps v3 and no v1 or v2 file is attached. Catches a fix
    that lets the exclusion drop the cache the render reads."""
    # GIVEN
    root: Path = scene["hip"].parent
    node, basedir = make_file_cache(root, load=1, type_name=type_name)
    scene["refs"] = [(basedir, '$HIP/cache/`chs("shot")`')]
    rop: FakeNode = make_rop(1, excluded=[str(root / "cache" / "sh010")])

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(rop)

    # THEN
    v3: Path = root / "cache" / "sh010" / "fx" / "v3"
    assert refs.input_directories == set()
    assert refs.input_filenames == {str(scene["hip"])} | {
        str(v3 / f"fx_v3.{frame}.bgeo.sc") for frame in (1001, 1002, 1003)
    }


def test_toggle_on_adds_no_cache_files_for_a_file_cache_that_does_not_load(
    scene: dict[str, Any],
) -> None:
    """A File Cache with Load from Disk off reads nothing, so none of its files are attached."""
    # GIVEN
    root: Path = scene["hip"].parent
    node, basedir = make_file_cache(root, load=0)
    scene["refs"] = [(basedir, "$HIP/cache/sh010")]

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(make_rop(1))

    # THEN
    assert refs.input_filenames == {str(scene["hip"])}
    assert refs.input_directories == {str(root / "cache" / "sh010")}


def test_toggle_on_reads_a_single_cache_file_that_does_not_change_per_frame(
    scene: dict[str, Any],
) -> None:
    """A File Cache whose path is the same on every frame attaches just that one file."""
    # GIVEN
    root: Path = scene["hip"].parent
    single: Path = root / "cache" / "static.bgeo.sc"
    touch(single, root / "cache" / "static_old.bgeo.sc")
    frames: dict[float, str] = {1001.0: str(single), 1002.0: str(single)}
    node: FakeNode = FakeNode(
        "/obj/fx/static",
        "Sop/filecache",
        {
            "loadfromdisk": FakeParm("loadfromdisk", 1),
            "file": FakeParm("file", str(single), frames=frames),
            "descriptivelabel": FakeParm("descriptivelabel", "static.bgeo.sc"),
        },
    )
    scene["refs"] = [(node.parm("descriptivelabel"), "static.bgeo.sc")]

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(make_rop(1))

    # THEN
    assert refs.input_filenames == {str(scene["hip"]), str(single)}


@pytest.mark.parametrize(
    ("path", "next_path", "expected"),
    [
        pytest.param("/c/fx_v3.1001.bgeo.sc", "/c/fx_v3.1002.bgeo.sc", "/c/fx_v3.*.bgeo.sc"),
        pytest.param("/c/fx_v3.1009.bgeo.sc", "/c/fx_v3.1010.bgeo.sc", "/c/fx_v3.*.bgeo.sc"),
        pytest.param("/c/f1001/a.bgeo", "/c/f1002/a.bgeo", "/c/f*/a.bgeo"),
        pytest.param("/c/[v3]/a.9.bgeo", "/c/[v3]/a.10.bgeo", "/c/[[]v3]/a.*.bgeo"),
        pytest.param("/c/a.10010.b", "/c/a.10020.b", "/c/a.*.b", id="shared trailing digit"),
        pytest.param("1001.b", "1002.b", "*.b", id="frame at the start"),
        pytest.param("1.b", "2.b", "*.b", id="first character differs"),
    ],
)
def test_frame_glob_covers_the_whole_frame_number(path: str, next_path: str, expected: str) -> None:
    """The glob replaces the whole frame number, including digits the two frames share on either
    side, wherever it sits, and escapes glob characters in the rest of the path. Catches a glob
    that only matches the frames sharing the first frame's leading digits."""
    # WHEN / THEN
    assert _assets._get_frame_glob(path, next_path) == expected


def test_toggle_on_leaves_out_an_excluded_folder_and_files(scene: dict[str, Any]) -> None:
    """Auto-detected inputs in the Excluded Inputs list are left out. The folder and image.png
    match by the path the entry evaluates to on the ROP, which differs from their $HIP refs, and
    the frame sequence matches by its text. The kept file is given as the path it evaluates to.
    Catches an exclusion list that is saved but ignored, or that compares only the text."""
    # GIVEN
    root: Path = scene["hip"].parent
    textures: FakeParm = folder_parm("/obj/a", "dir", root / "textures")
    image: Path = root / "image.png"
    frame: Path = root / "seq.1001.png"
    touch(image, frame, root / "kept.png")
    node: FakeNode = FakeNode("/obj/b", "Object/null")
    image_parm: FakeParm = node.add("file", FakeParm("file", str(image), raw="$HIP/image.png"))
    frames: dict[float, str] = {1001.0: str(frame), 1002.0: str(root / "seq.1002.png")}
    frame_parm: FakeParm = node.add(
        "seq", FakeParm("seq", str(frame), raw="$HIP/seq.$F4.png", frames=frames)
    )
    kept_parm: FakeParm = node.add("kept", FakeParm("kept", str(root / "kept.png")))
    scene["refs"] = [
        (textures, "$HIP/textures"),
        (image_parm, "$HIP/image.png"),
        (frame_parm, "$HIP/seq.$F4.png"),
        (kept_parm, "$HIP/kept.png"),
    ]
    excluded_folder: FakeParm = FakeParm(
        "excluded_input#", str(root / "textures"), raw="$HIP/textures"
    )
    excluded_sequence: FakeParm = FakeParm(
        "excluded_input#", "/elsewhere/seq.1001.png", raw="$HIP/seq.$F4.png"
    )
    rop: FakeNode = make_rop(1, excluded=[excluded_folder, str(image), excluded_sequence])

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(rop)

    # THEN
    assert refs.input_directories == set()
    assert refs.input_filenames == {str(scene["hip"]), str(root / "kept.png")}


@pytest.mark.parametrize(
    ("type_name", "parms"),
    [
        pytest.param("Sop/file", ["sopoutput"], id="a node that is not a File Cache"),
        pytest.param("Sop/filecache::2.0", [], id="a File Cache with no read path parm"),
    ],
)
def test_toggle_on_adds_no_cache_files_without_a_file_cache_read_path(
    scene: dict[str, Any], type_name: str, parms: list[str]
) -> None:
    """Only a File Cache's read path adds cache files: another node type with Load from Disk on
    adds none, and a File Cache without a sopoutput or file parm adds none and doesn't fail."""
    # GIVEN
    root: Path = scene["hip"].parent
    cache: Path = root / "cache" / "a.1001.bgeo"
    touch(cache, root / "cache" / "a.1002.bgeo")
    frames: dict[float, str] = {1001.0: str(cache), 1002.0: str(root / "cache" / "a.1002.bgeo")}
    node: FakeNode = FakeNode("/obj/geo/node", type_name)
    node.add("loadfromdisk", FakeParm("loadfromdisk", 1))
    for parm_name in parms:
        node.add(parm_name, FakeParm(parm_name, str(cache), frames=frames))
    label: FakeParm = node.add("descriptivelabel", FakeParm("descriptivelabel", "a.$F4.bgeo"))
    scene["refs"] = [(label, "a.$F4.bgeo")]

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(make_rop(1))

    # THEN
    assert refs.input_filenames == {str(scene["hip"])}


def test_parse_files_toggle_off_neither_applies_nor_deletes_the_exclusion_list(
    scene: dict[str, Any],
) -> None:
    """With the toggle off, an excluded folder is detected again, and the list is kept, so turning
    the toggle back on restores the exclusions."""
    # GIVEN
    root: Path = scene["hip"].parent
    textures: FakeParm = folder_parm("/obj/a", "dir", root / "textures")
    scene["refs"] = [(textures, "$HIP/textures")]
    rop: FakeNode = make_rop(0, excluded=[str(root / "textures")])

    # WHEN
    _parse_files(rop)

    # THEN
    assert rop.parm("input_directories").values() == ["$HIP/textures"]
    assert rop.parm("excluded_inputs").values() == [str(root / "textures")]


def test_an_auto_detected_input_deleted_from_the_lists_stays_excluded_on_every_scan(
    scene: dict[str, Any],
) -> None:
    """An auto-detected folder the artist deleted from the node's Input Directories list is left
    out by the Submit dialog's scan right away, and Parse Files saves it to the Excluded Inputs
    list, so it stays out after the lists are rebuilt. Catches a scan that re-adds it."""
    # GIVEN
    root: Path = scene["hip"].parent
    textures: FakeParm = folder_parm("/obj/a", "dir", root / "textures")
    geo: FakeParm = folder_parm("/obj/b", "dir", root / "geo")
    scene["refs"] = [(textures, "$HIP/textures"), (geo, "$HIP/geo")]
    rop: FakeNode = make_rop(1)
    _parse_files(rop)
    assert rop.parm("input_directories").values() == [str(root / "geo"), str(root / "textures")]

    # The artist deletes textures from the node's Input Directories list.
    rop.parm("input_directories").removeMultiParmInstance(1)

    # THEN the Submit dialog's scan leaves it out before any parse,
    assert _get_scene_asset_references(rop).input_directories == {str(root / "geo")}
    # Parse Files saves it to the Excluded Inputs list,
    _parse_files(rop)
    assert rop.parm("excluded_inputs").values() == [str(root / "textures")]
    assert rop.parm("input_directories").values() == [str(root / "geo")]
    # and a later parse, with the lists rebuilt, still leaves it out.
    _parse_files(rop)
    assert rop.parm("input_directories").values() == [str(root / "geo")]
    assert rop.parm("excluded_inputs").values() == [str(root / "textures")]


def test_parse_files_toggle_on_keeps_a_manually_added_folder_that_holds_an_output(
    scene: dict[str, Any],
) -> None:
    """The overlap rule applies only to auto-detected folders: a folder the artist added to Input
    Directories stays, even when it holds a detected output."""
    # GIVEN
    root: Path = scene["hip"].parent
    (root / "render").mkdir()
    rop: FakeNode = make_rop(1)
    rop.parm("input_directories").instances = [FakeParm("input_dir#", str(root / "render"))]
    rop.ancestors = [karma_rop(root / "render" / "v004" / "beauty.exr")]

    # WHEN
    _parse_files(rop)

    # THEN
    assert rop.parm("input_directories").values() == [str(root / "render")]


def test_submit_path_gets_evaluated_folders_without_outputs(scene: dict[str, Any]) -> None:
    """The headless and Submit dialog scan (get_asset_references) with the toggle on returns the
    evaluated folders, which deadline-cloud can walk, and leaves out the one holding the output.
    Catches a Submit path that still gets refs such as $HIP/cache/`chs("shot")`, which attach
    nothing."""
    # GIVEN
    root: Path = scene["hip"].parent
    cache: FakeParm = folder_parm(
        "/obj/pipeline", "cache_root", root / "cache" / "sh010", raw='$HIP/cache/`chs("shot")`'
    )
    render: FakeParm = folder_parm(
        "/obj/pipeline", "render_root", root / "render" / "sh010", raw='$HIP/render/`chs("shot")`'
    )
    scene["refs"] = [(cache, cache.raw), (render, render.raw)]
    rop: FakeNode = make_rop(1)
    rop.ancestors = [karma_rop(root / "render" / "sh010" / "v004" / "beauty.1001.exr")]

    # WHEN
    with mock.patch.object(hou, "node", return_value=rop):
        refs: AssetReferences = submitter.HoudiniSubmitter(rop.path()).get_asset_references(
            submitter.HoudiniSubmitterSettings()
        )

    # THEN
    assert refs.input_directories == {str(root / "cache" / "sh010")}
    assert all(os.path.isdir(d) for d in refs.input_directories)


@pytest.mark.parametrize("filter_inputs", [0, 1])
def test_save_bundle_rescans_only_with_the_toggle_on(
    scene: dict[str, Any], filter_inputs: int
) -> None:
    """With the toggle on, Save Bundle re-scans first, so an exclusion added after the last parse
    is honored in the bundle. With it off, Save Bundle writes the lists as they are, as before."""
    # GIVEN
    root: Path = scene["hip"].parent
    textures: FakeParm = folder_parm("/obj/a", "dir", root / "textures")
    scene["refs"] = [(textures, "$HIP/textures")]
    rop: FakeNode = make_rop(filter_inputs)
    rop.add("name", FakeParm("name", "job"))
    _parse_files(rop)
    # The artist excludes textures after the last parse.
    rop.parm("excluded_inputs").instances = [FakeParm("excluded_input#", str(root / "textures"))]

    # WHEN
    with (
        mock.patch.object(submitter, "create_job_history_bundle_dir", return_value="/b"),
        mock.patch.object(submitter, "_create_job_bundle") as create_bundle,
    ):
        submitter.save_bundle_callback({"node": rop})

    # THEN
    written: AssetReferences = create_bundle.call_args.args[2]
    expected: set[str] = set() if filter_inputs else {"$HIP/textures"}
    assert written.input_directories == expected


def test_a_node_without_the_toggle_is_scanned_as_before(scene: dict[str, Any]) -> None:
    """A node without the toggle parm, such as the PDG scheduler node, is scanned with the
    toggle off."""
    # GIVEN
    root: Path = scene["hip"].parent
    textures: FakeParm = folder_parm("/obj/a", "dir", root / "textures")
    scene["refs"] = [(textures, "$HIP/textures")]
    node: FakeNode = FakeNode("/tasks/topnet1/scheduler", "Top/deadline_cloud_scheduler")

    # THEN
    assert _get_scene_asset_references(node).input_directories == {"$HIP/textures"}
    assert _assets._is_filter_auto_inputs_enabled(node) is False


def test_toggle_on_gives_input_files_as_the_paths_their_parms_read(scene: dict[str, Any]) -> None:
    """With the toggle on, the Submit dialog and headless scan give each input file as the path it
    evaluates to on its own parm, and a $F4 sequence as each of its existing frames. Catches a
    scan that gives refs such as $HIP/tex/wood.png, which deadline-cloud doesn't expand, so the
    file the render reads is not uploaded."""
    # GIVEN
    root: Path = scene["hip"].parent
    wood: Path = root / "tex" / "wood.png"
    sequence: list[Path] = [root / "geo" / f"rock.{frame}.bgeo" for frame in (1001, 1002, 1003)]
    touch(wood, *sequence, root / "geo" / "rock_old.bgeo")
    node: FakeNode = FakeNode("/mat/wood", "Vop/principledshader")
    texture: FakeParm = node.add("tex", FakeParm("tex", str(wood), raw="$HIP/tex/wood.png"))
    frames: dict[float, str] = {1001.0: str(sequence[0]), 1002.0: str(sequence[1])}
    rock: FakeParm = node.add(
        "rock", FakeParm("rock", str(sequence[0]), raw="$HIP/geo/rock.$F.bgeo", frames=frames)
    )
    scene["refs"] = [(texture, texture.raw), (rock, rock.raw)]
    rop: FakeNode = make_rop(1)

    # WHEN
    with mock.patch.object(hou, "node", return_value=rop):
        refs: AssetReferences = submitter.HoudiniSubmitter(rop.path()).get_asset_references(
            submitter.HoudiniSubmitterSettings()
        )

    # THEN
    assert refs.input_filenames == {str(scene["hip"]), str(wood)} | {str(p) for p in sequence}


DIALOG_SCRIPT: Path = (
    Path(__file__).parents[3]
    / "src"
    / "deadline"
    / "houdini_submitter"
    / "otls"
    / "deadline_cloud.hda"
    / "Driver_1deadline__cloud"
    / "DialogScript"
)


def _parm_block(dialog_script: str, parm_name: str) -> str:
    """The text of one parm block of a DialogScript, up to the end of its first line group."""
    match: Optional[re.Match[str]] = re.search(
        r"\bparm\s*\{\s*name\s+\"" + re.escape(parm_name) + r"\"(.*?)\n\s*\}", dialog_script, re.S
    )
    assert match is not None, parm_name
    return match.group(1)


def test_the_rop_has_the_filter_toggle_off_by_default_and_a_string_exclusion_list() -> None:
    """The ROP's Filter Auto-Detected Inputs toggle defaults to off, so a new or old ROP submits as
    before. Its Excluded Input entries are string parms, which hou.fileReferences() does not
    report, so one ROP's exclusions are never scanned as another ROP's inputs. Catches a default
    of on, or exclusions attached as inputs."""
    # GIVEN
    dialog_script: str = DIALOG_SCRIPT.read_text(encoding="utf8")

    # WHEN
    toggle: str = _parm_block(dialog_script, "filter_auto_inputs")
    excluded: str = _parm_block(dialog_script, "excluded_input#")

    # THEN
    assert re.search(r"\btype\s+toggle\b", toggle)
    assert re.search(r'\bdefault\s*\{\s*"0"\s*\}', toggle)
    assert re.search(r"\btype\s+string\b", excluded)


@pytest.mark.parametrize(
    ("input_folder", "output_folder", "dropped"),
    [
        pytest.param(r"c:\Proj\Render\sh010", "C:/proj/render/sh010", True, id="equal"),
        pytest.param(r"c:\Proj\render", "C:/proj/Render/sh010/v004", True, id="contains"),
        pytest.param(r"C:\proj\render2", "c:/proj/render", False, id="prefix sibling"),
    ],
)
def test_windows_drive_case_and_slashes_do_not_defeat_the_overlap_rule(
    input_folder: str, output_folder: str, dropped: bool
) -> None:
    """On Windows, paths that differ only in letter case or slash direction are the same folder,
    and a folder that only shares a prefix is not. The reporter's workstation runs Windows.
    Catches a comparison that skips normcase or the separator check."""
    # GIVEN Windows path rules.
    windows_os: types.SimpleNamespace = types.SimpleNamespace(path=ntpath, sep="\\")

    # WHEN
    with mock.patch.object(_assets, "os", windows_os):
        reason: str = _assets._get_output_overlap_reason(
            _assets._normalize_path(input_folder), [_assets._normalize_path(output_folder)], set()
        )

    # THEN
    assert bool(reason) is dropped


@pytest.mark.parametrize("scene_root", ["JOB", "the .hip's folder"])
def test_a_folder_inside_a_scene_root_output_is_kept(
    scene: dict[str, Any], scene_root: str
) -> None:
    """A folder inside an output that writes straight into $JOB, or into the folder that holds the
    .hip, is kept, each checked where it differs from $HIP. Catches a scene-root list that is
    missing $JOB or the .hip's folder, which would drop every input folder of the project."""
    # GIVEN
    root: Path = scene["hip"].parent
    # The output writes into $JOB, or into the .hip's folder while $HIP points elsewhere.
    project: Path = root.parent / "project" if scene_root == "JOB" else root
    if scene_root == "JOB":
        scene["env"]["JOB"] = str(project)
    else:
        scene["env"]["HIP"] = str(root.parent / "elsewhere")
    folder: FakeParm = folder_parm("/obj/a", "dir", project / "tex")
    scene["refs"] = [(folder, "$JOB/tex")]
    rop: FakeNode = make_rop(1)
    rop.ancestors = [karma_rop(project / "beauty.exr")]

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(rop)

    # THEN
    assert refs.input_directories == {str(project / "tex")}


def test_an_auto_detected_file_deleted_from_the_list_stays_excluded(scene: dict[str, Any]) -> None:
    """An auto-detected file the artist deleted from the node's Input Filenames list is left out
    by the Submit dialog's scan, and Parse Files saves it to the Excluded Inputs list, so it stays
    out. Catches a deletion check that looks only at the Input Directories list."""
    # GIVEN
    root: Path = scene["hip"].parent
    image: Path = root / "image.png"
    touch(image)
    node: FakeNode = FakeNode("/obj/b", "Object/null")
    image_parm: FakeParm = node.add("file", FakeParm("file", str(image), raw="$HIP/image.png"))
    scene["refs"] = [(image_parm, image_parm.raw)]
    rop: FakeNode = make_rop(1)
    _parse_files(rop)
    assert rop.parm("input_filenames").values() == sorted([str(scene["hip"]), str(image)])

    # The artist deletes image.png from the node's Input Filenames list.
    rop.parm("input_filenames").instances = [
        p for p in rop.parm("input_filenames").instances if p.raw != str(image)
    ]

    # THEN the Submit dialog's scan leaves it out,
    assert _get_scene_asset_references(rop).input_filenames == {str(scene["hip"])}
    # and Parse Files saves it to the Excluded Inputs list.
    _parse_files(rop)
    assert rop.parm("excluded_inputs").values() == [str(image)]
    assert rop.parm("input_filenames").values() == [str(scene["hip"])]


def test_turning_the_toggle_on_after_a_parse_with_it_off_excludes_nothing(
    scene: dict[str, Any],
) -> None:
    """After a parse with the toggle off, the lists hold refs. Turning the toggle on gives the
    same inputs as evaluated paths, and none of them counts as deleted by the artist. Catches a
    deletion check that compares the displayed refs with the new evaluated paths, which would
    put every input in Excluded Inputs."""
    # GIVEN
    root: Path = scene["hip"].parent
    cache: FakeParm = folder_parm(
        "/obj/pipeline", "cache_root", root / "cache" / "sh010", raw='$HIP/cache/`chs("shot")`'
    )
    image: Path = root / "image.png"
    touch(image)
    node: FakeNode = FakeNode("/obj/b", "Object/null")
    image_parm: FakeParm = node.add("file", FakeParm("file", str(image), raw="$HIP/image.png"))
    scene["refs"] = [(cache, cache.raw), (image_parm, image_parm.raw)]
    rop: FakeNode = make_rop(0)
    _parse_files(rop)
    rop.parm("filter_auto_inputs").value = 1

    # WHEN
    refs: AssetReferences = _get_scene_asset_references(rop)
    _parse_files(rop)

    # THEN
    assert refs.input_directories == {str(root / "cache" / "sh010")}
    assert refs.input_filenames == {str(scene["hip"]), str(image)}
    assert rop.parm("input_directories").values() == [str(root / "cache" / "sh010")]
    assert rop.parm("excluded_inputs").values() == []


def test_a_deleted_expression_folder_from_a_parse_with_the_toggle_off_stays_excluded(
    scene: dict[str, Any],
) -> None:
    """A folder saved by a parse with the toggle off is held as its ref, $HIP/tex/`chs("shot")`.
    When the artist turns the toggle on and deletes it, it matches by that ref on every scan, and
    the ref's widened value on the ROP ($HIP/tex) doesn't leave out the real $HIP/tex folder.
    Catches a deletion that is ignored because the ref is not the evaluated path."""
    # GIVEN
    root: Path = scene["hip"].parent
    shot_tex: FakeParm = folder_parm(
        "/obj/pipeline", "tex_root", root / "tex" / "sh010", raw='$HIP/tex/`chs("shot")`'
    )
    tex: FakeParm = folder_parm("/obj/lib", "tex", root / "tex")
    scene["refs"] = [(shot_tex, shot_tex.raw), (tex, "$HIP/tex")]
    rop: FakeNode = make_rop(0)
    _parse_files(rop)
    rop.parm("filter_auto_inputs").value = 1
    # The artist deletes the expression folder. On the ROP its ref widens to $HIP/tex.
    rop.parm("input_directories").instances = [FakeParm("input_dir#", "$HIP/tex")]
    rop.parm("auto_input_directories").instances = [
        FakeParm("auto_input_dir#", str(root / "tex"), raw='$HIP/tex/`chs("shot")`'),
        FakeParm("auto_input_dir#", str(root / "tex"), raw="$HIP/tex"),
    ]

    # THEN the Submit dialog's scan leaves out only the deleted folder,
    assert _get_scene_asset_references(rop).input_directories == {str(root / "tex")}
    # and so do the next parses, which save its ref to Excluded Inputs.
    _parse_files(rop)
    assert rop.parm("excluded_inputs").values() == ['$HIP/tex/`chs("shot")`']
    _parse_files(rop)
    assert rop.parm("input_directories").values() == [str(root / "tex")]
