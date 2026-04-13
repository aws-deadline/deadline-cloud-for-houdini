# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import os
from pathlib import Path
from unittest import mock

import pytest
from pytest import param

from .mock_hou import hou_module as hou
from deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets import (
    _get_evaluated_asset_references,
    _get_scene_asset_references,
    _get_output_directories,
    _get_usd_asset_references,
    _get_usd_output_directories,
    _houdini_time_vars_to_glob,
    _parse_files,
)
from deadline.client.job_bundle.submission import AssetReferences


def test_get_scene_asset_references():
    hou.hscript.return_value = (
        "1 [ ] /out/mantra1 \t( 1 5 1 )\n2 [ 1 ] /out/karma1/lopnet/rop_usdrender \t( 1 5 1 )\n",
        "",
    )
    node = hou.node
    hou.node.type().name.return_value = "deadline-cloud"
    mock_parm = hou.Parm
    hou.Parm.node.return_value = node
    hou.Parm.name.return_value = "shadowmap_file"
    hou.node.type().nameWithCategory.return_value = "Driver/ifd"
    hou.hipFile.path.return_value = "/some/path/test.hip"
    hou.node.parm().eval.return_value = "/tmp/foo.$F.exr"

    dir_parm = mock.Mock()
    dir_parm.node.return_value = None
    dir_parm.evalAsString.return_value = "/some_expanded_path/assets/"

    file_parm = mock.Mock()
    file_parm.node.return_value = None
    file_parm.evalAsString.return_value = "/some_expanded_path/image.png"

    hou.fileReferences.return_value = (
        # These references should be resolved and added as job attachments
        (dir_parm, "$HIP/assets/"),
        (file_parm, "$HIP/image.png"),
        # These references should all be skipped based on their reference prefix
        (mock_parm, "opdef:$OS.rat"),
        (mock_parm, "oplib:$OS.rat"),
        (mock_parm, "temp:$OS.rat"),
        (mock_parm, "op:$OS.rat"),
    )
    mock_os = mock.Mock()
    mock_os.path.isdir = lambda path: path.endswith("/")
    mock_os.path.isfile = lambda path: not path.endswith("/")
    mock_os.path.splitext = os.path.splitext
    mock_os.path.realpath = os.path.realpath

    with mock.patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.os", mock_os
    ):
        asset_refs = _get_scene_asset_references(node)

    assert asset_refs.input_filenames == {"$HIP/image.png", "/some/path/test.hip"}
    assert asset_refs.input_directories == {"$HIP/assets/"}
    assert asset_refs.output_directories == set()


def test_get_output_directories():
    """
    Test that given a node, the type name and category are mapped correctly to
    determine the parm to get the output directory from and return it.
    """
    node = hou.node
    node.type().nameWithCategory.return_value = "Driver/geometry"
    node.parm().eval.return_value = "/test/directory/detection/output.png"

    output_directories = _get_output_directories(node)

    node.parm.assert_called_with("sopoutput")
    assert output_directories == {"/test/directory/detection"}


@pytest.mark.parametrize(
    ("node_type", "output_parm_name"), [("Driver/fetch", "source"), ("Driver/wedge", "driver")]
)
def test_get_recursive_output_directories(node_type: str, output_parm_name: str):
    """
    Test output directory detection for fetch and wedge nodes that recursively
    find the output directories.
    """
    inner_node = mock.MagicMock()
    inner_node.type().nameWithCategory.return_value = "Driver/ifd"
    inner_node.parm().eval.return_value = "/test/output/directory/mantra/test.png"
    node = hou.node
    node.type().nameWithCategory.return_value = node_type
    node.node.return_value = inner_node

    out_dirs = _get_output_directories(node)

    node.parm.assert_called_once_with(output_parm_name)
    inner_node.parm.assert_called_with("vm_picture")
    assert out_dirs == {"/test/output/directory/mantra"}


@pytest.mark.parametrize(
    "auto_detected_assets, current_assets, prev_auto_detected_assets, expected_input_filenames, expected_input_directories, expected_output_directories",
    [
        pytest.param(
            AssetReferences(), AssetReferences(), AssetReferences(), [], [], [], id="no assets"
        ),
        pytest.param(
            AssetReferences(input_filenames={"/users/testuser/input.png"}),
            AssetReferences(),
            AssetReferences(),
            ["/users/testuser/input.png"],
            [],
            [],
            id="single auto detected asset",
        ),
        pytest.param(
            AssetReferences(
                input_filenames={"/users/testuser/input.png"},
                input_directories={"/users/testuser/input"},
            ),
            AssetReferences(
                input_filenames={
                    "/users/testuser/someotherfile.png",
                    "/users/testuser/input.png",
                    "/users/testuser/manuallyaddedfile.jpg",
                },
                output_directories={"/user/testuser/render"},
            ),
            AssetReferences(),
            [
                "/users/testuser/manuallyaddedfile.jpg",
                "/users/testuser/someotherfile.png",
                "/users/testuser/input.png",
            ],
            ["/users/testuser/input"],
            ["/user/testuser/render"],
            id="multiple auto detected and manual assets",
        ),
        pytest.param(
            AssetReferences(
                input_filenames={"/users/testuser/input_1.png"},
                input_directories={"/users/testuser/input_1"},
                output_directories={"/users/testuser/output_1"},
            ),
            AssetReferences(
                input_filenames={"/users/testuser/input_1.png", "/users/testuser/input_2.png"},
                input_directories={"/users/testuser/input_1", "/users/testuser/input_2"},
                output_directories={
                    "/users/testuser/output_1",
                    "/users/testuser/output_2",
                    "/users/testuser/manual_output_1",
                },
            ),
            AssetReferences(
                input_filenames={"/users/testuser/input_1.png", "/users/testuser/input_2.png"},
                input_directories={"/users/testuser/input_1", "/users/testuser/input_2"},
                output_directories={"/users/testuser/output_1", "/users/testuser/output_2"},
            ),
            ["/users/testuser/input_1.png"],
            ["/users/testuser/input_1"],
            ["/users/testuser/manual_output_1", "/users/testuser/output_1"],
            id="Removal of previously auto detected assets",
        ),
    ],
)
def test_parse_files_manually_added(
    auto_detected_assets: AssetReferences,
    current_assets: AssetReferences,
    prev_auto_detected_assets: AssetReferences,
    expected_input_filenames: list[str],
    expected_input_directories: list[str],
    expected_output_directories: list[str],
) -> None:
    """
    Test that parsing the scene for files correctly puts any non-detected files
    that may have been manually added at the front of the list and keeps them
    when called.
    """

    with (
        mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._get_scene_asset_references"
        ) as mock_get_scene_assets,
        mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._get_unevaluated_asset_references"
        ) as mock_get_unevaluated_asset_references,
        mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._update_paths_parm"
        ) as mock_update_paths_parm,
        mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._get_saved_auto_detected_asset_references"
        ) as mock_get_saved_auto_asset_references,
    ):
        mock_get_scene_assets.return_value = auto_detected_assets
        mock_get_unevaluated_asset_references.return_value = current_assets
        mock_get_saved_auto_asset_references.return_value = prev_auto_detected_assets

        node = hou.node
        _parse_files(node)

        mock_get_scene_assets.assert_called_once()
        mock_get_unevaluated_asset_references.assert_called_once()
        mock_get_saved_auto_asset_references.assert_called_once()
        mock_update_paths_parm.assert_has_calls(
            [
                mock.call(node, "input_filenames", expected_input_filenames),
                mock.call(node, "input_directories", expected_input_directories),
                mock.call(node, "output_directories", expected_output_directories),
                mock.call(node, "auto_input_filenames", list(auto_detected_assets.input_filenames)),
                mock.call(
                    node, "auto_input_directories", list(auto_detected_assets.input_directories)
                ),
                mock.call(
                    node, "auto_output_directories", list(auto_detected_assets.output_directories)
                ),
            ]
        )
        assert mock_update_paths_parm.call_count == 6


@pytest.mark.parametrize(
    ("input", "expected"),
    [
        param("a.$F.png", "a.*.png", id="$F"),
        param("b.${F}.png", "b.*.png", id="${F}"),
        param("c.$F4.png", "c.*.png", id="$F4"),
        param("d.${F4}.png", "d.*.png", id="${F4}"),
        param("e.$FF.png", "e.*.png", id="$FF"),
        param("f.${FF}.png", "f.*.png", id="${FF}"),
        param("g.$T.png", "g.*.png", id="$T"),
        param("h.${T}.png", "h.*.png", id="${T}"),
        param("i.$SF.png", "i.*.png", id="$SF"),
        param("j.${SF}.png", "j.*.png", id="${SF}"),
        param("k.$ST.png", "k.*.png", id="$ST"),
        param("l.${ST}.png", "l.*.png", id="{ST}"),
        param("$HIPNAME.$OS.png", "$HIPNAME.$OS.png", id="No matches"),
        param("$HIPNAME.$OS.$F4.png", "$HIPNAME.$OS.*.png", id="Only time variable matched"),
        param("$HIP/$HIPNAME.$OS.png", "$HIP/$HIPNAME.$OS.png", id="No matches with directories"),
        param(
            "$HIP/$HIPNAME.$OS.$F4.png",
            "$HIP/$HIPNAME.$OS.*.png",
            id="Only time variable matched with directories",
        ),
    ],
)
def test_time_variable_pattern_matching(input, expected):
    """Test to ensure we only replace the time-based variables and ignore others"""
    # GIVEN / WHEN
    actual = _houdini_time_vars_to_glob(input)

    # THEN
    assert actual == expected


# "pattern", "filenames"
filenames_with_frames = [
    param(
        "sequence.$F.png",
        [
            "sequence.0.png",
            "sequence.1.png",
        ],
        id="$F",
    ),
    param(
        "sequence.${F}.png",
        [
            "sequence.0.png",
            "sequence.1.png",
        ],
        id="Protected $F",
    ),
    param(
        "sequence.$F4.png",
        ["sequence.0000.png", "sequence.0001.png", "sequence.0002.png"],
        id="$F with padding",
    ),
    param(
        "sequence.${F4}.png",
        ["sequence.0000.png", "sequence.0001.png", "sequence.0002.png"],
        id="Protected $F with padding",
    ),
    param(
        "sequence.$F4.$F5.png",
        [
            "sequence.000.0000.png",
            "sequence.0001.00001.png",
        ],
        id="Multiple $F with padding",
    ),
    param(
        "sequence.${F}F.png",
        [
            "sequence.0F.png",
            "sequence.1F.png",
        ],
        id="Protected $F with trailing F",
    ),
    param(
        "sequence.${F4}F.png",
        [
            "sequence.0000F.png",
            "sequence.0001F.png",
        ],
        id="Protected $F with padding and trailing F",
    ),
    param(
        "sequence.$FF.png",
        [
            "sequence.0.png",
            "sequence.01.png",
        ],
        id="$FF",
    ),
    param(
        "sequence.${FF}.png",
        [
            "sequence.0.png",
            "sequence.01.png",
        ],
        id="Protected $FF",
    ),
    param(
        "sequence.$FFF.png",
        [
            "sequence.0F.png",
            "sequence.1F.png",
        ],
        id="$FF with trailing F",
    ),
    param(
        "sequence.$SF.png",
        [
            "sequence.0.png",
            "sequence.1.png",
        ],
        id="$SF",
    ),
    param(
        "sequence.${SF}.png",
        [
            "sequence.0.png",
            "sequence.1.png",
        ],
        id="Protected $SF",
    ),
    param(
        "sequence.$ST.png",
        [
            "sequence.0.png",
            "sequence.1.png",
        ],
        id="$ST",
    ),
    param(
        "sequence.${ST}.png",
        [
            "sequence.0.png",
            "sequence.1.png",
        ],
        id="Protected $ST",
    ),
    param(
        "sequence.`python stuff`.png",
        [
            "sequence.0.png",
            "sequence.1.png",
        ],
        id="Python expression",
    ),
    param(
        "sequence.`$F5`.png",
        [
            "sequence.0.png",
            "sequence.1.png",
        ],
        id="Python expression with a frame variable within",
    ),
]


@pytest.mark.parametrize(("pattern", "filenames"), filenames_with_frames)
def test_filenames_with_frames(tmpdir, pattern: str, filenames: list[str]):
    """Tests that we actually capture filenames that contain a time-based variable."""
    # GIVEN
    tmpdir_path = Path(tmpdir)
    filesystem = {str(tmpdir_path / file) for file in filenames}
    extra_files = {
        "seq.0.png",
        "sequence.1.jpg",
        "sequence.png",
    }

    # create files on disc
    for file in filesystem | extra_files:
        (tmpdir_path / file).touch()

    # populate houdini node with paths
    mock_ = mock.Mock()
    mock_.eval.return_value = str(tmpdir_path / _houdini_time_vars_to_glob(pattern))
    mock_.unexpandedString.return_value = str(tmpdir_path / pattern)
    mocked_input_parms: list = [mock_]

    def input_filenames_override(str_):
        mock_ = mock.Mock()
        if str_ != "input_filenames":
            mock_.multiParmInstances.return_value = []
            return mock_

        mock_.multiParmInstances.return_value = mocked_input_parms
        return mock_

    node = hou.node
    node.parm.side_effect = input_filenames_override

    # WHEN
    result_refs = _get_evaluated_asset_references(node)

    # THEN
    assert result_refs.input_filenames == filesystem
    assert len(result_refs.input_filenames) == len(filesystem)


def test_dirs_with_time_variable(tmpdir):
    """Tests that we actually capture file paths whose directory
    contain a time-based variable."""
    # GIVEN
    pattern = Path("show", "$F", "shot.png")
    matched_paths = [
        Path("show", "0", "shot.png"),
        Path("show", "1", "shot.png"),
        Path("show", "2", "shot.png"),
    ]
    not_matched_paths = [
        Path("show", "0", "noshot.png"),
    ]
    tmpdir_path = Path(tmpdir)
    matched_filesystem = {str(tmpdir_path / path) for path in matched_paths}
    filesystem = {str(tmpdir_path / path) for path in matched_paths + not_matched_paths}
    # create files on disc
    for file in filesystem:
        (tmpdir_path / file).parent.mkdir(parents=True, exist_ok=True)
        (tmpdir_path / file).touch()

    # populate houdini node with paths
    mock_ = mock.Mock()
    mock_.eval.return_value = str(tmpdir_path / _houdini_time_vars_to_glob(str(pattern)))
    mock_.unexpandedString.return_value = str(tmpdir_path / pattern)
    mocked_input_parms: list = [mock_]

    def input_filenames_override(str_):
        mock_ = mock.Mock()
        if str_ != "input_filenames":
            mock_.multiParmInstances.return_value = []
            return mock_

        mock_.multiParmInstances.return_value = mocked_input_parms
        return mock_

    node = hou.node
    node.parm.side_effect = input_filenames_override

    # WHEN
    result_refs = _get_evaluated_asset_references(node)

    # THEN
    assert filesystem != matched_filesystem
    assert result_refs.input_filenames == matched_filesystem
    assert len(result_refs.input_filenames) == len(matched_filesystem)


# --- USD dependency detection tests ---


class TestGetUsdAssetReferences:
    """Tests for _get_usd_asset_references()"""

    @mock.patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._get_usd_output_directories",
        return_value=set(),
    )
    def test_finds_layers_and_assets(self, mock_output_dirs):
        """Verify layers and assets from ComputeAllDependencies are collected."""
        mock_layer_a = mock.Mock()
        mock_layer_a.realPath = "/scene/scene.usda"
        mock_layer_b = mock.Mock()
        mock_layer_b.realPath = "/scene/model.usda"
        mock_layer_empty = mock.Mock()
        mock_layer_empty.realPath = ""

        with mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.UsdUtils"
        ) as mock_usd_utils:
            mock_usd_utils.ComputeAllDependencies.return_value = (
                [mock_layer_a, mock_layer_b, mock_layer_empty],
                ["/scene/textures/wood.exr", "/scene/textures/metal.exr"],
                [],
            )

            input_files, output_dirs, unresolved = _get_usd_asset_references({"/scene/scene.usda"})

        assert input_files == {
            "/scene/scene.usda",
            "/scene/model.usda",
            os.path.normpath("/scene/textures/wood.exr"),
            os.path.normpath("/scene/textures/metal.exr"),
        }
        assert unresolved == []

    @mock.patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._get_usd_output_directories",
        return_value=set(),
    )
    def test_handles_unresolved(self, mock_output_dirs):
        """Verify resolved assets and unresolved paths are separated correctly."""
        mock_layer = mock.Mock()
        mock_layer.realPath = "/scene/scene.usda"

        with mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.UsdUtils"
        ) as mock_usd_utils:
            mock_usd_utils.ComputeAllDependencies.return_value = (
                [mock_layer],
                ["/scene/textures/valid.exr"],
                ["/missing/texture.exr", "/missing/model.usd"],
            )

            input_files, _, unresolved = _get_usd_asset_references({"/scene/scene.usda"})

        assert os.path.normpath("/scene/textures/valid.exr") in input_files
        assert "/missing/texture.exr" not in input_files
        assert "/missing/model.usd" not in input_files
        assert unresolved == ["/missing/texture.exr", "/missing/model.usd"]

    @mock.patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._get_usd_output_directories",
        return_value=set(),
    )
    def test_deduplicates_across_files(self, mock_output_dirs):
        """Verify shared dependencies across two USD files appear only once."""
        mock_layer_a = mock.Mock()
        mock_layer_a.realPath = "/scene/a.usda"
        mock_layer_b = mock.Mock()
        mock_layer_b.realPath = "/scene/b.usda"
        mock_shared = mock.Mock()
        mock_shared.realPath = "/scene/shared.usda"

        with mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.UsdUtils"
        ) as mock_usd_utils:
            mock_usd_utils.ComputeAllDependencies.side_effect = [
                ([mock_layer_a, mock_shared], ["/scene/tex.exr"], []),
                ([mock_layer_b, mock_shared], ["/scene/tex.exr"], []),
            ]

            input_files, _, _ = _get_usd_asset_references({"/scene/a.usda", "/scene/b.usda"})

        assert input_files == {
            "/scene/a.usda",
            "/scene/b.usda",
            "/scene/shared.usda",
            os.path.normpath("/scene/tex.exr"),
        }


class TestGetUsdOutputDirectories:
    """Tests for _get_usd_output_directories()"""

    @pytest.mark.parametrize(
        "products_setup, expected",
        [
            pytest.param(
                [
                    ("/renders/beauty/output.exr", "RenderProduct"),
                    ("/renders/aov/depth.exr", "RenderProduct"),
                ],
                {os.path.normpath("/renders/beauty"), os.path.normpath("/renders/aov")},
                id="multiple_products",
            ),
            pytest.param(
                [("/renders/output.exr", "RenderProduct")],
                {os.path.normpath("/renders")},
                id="single_product",
            ),
            pytest.param(
                [],
                set(),
                id="no_products",
            ),
        ],
    )
    def test_output_directories(self, products_setup, expected):
        """Verify output directories extracted from RenderProduct prims."""
        with mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.Usd"
        ) as mock_usd:
            mock_stage = mock.Mock()
            mock_usd.Stage.Open.return_value = mock_stage

            if not products_setup:
                mock_products = mock.Mock()
                mock_products.IsValid.return_value = False
                mock_stage.GetPrimAtPath.return_value = mock_products
            else:
                mock_products = mock.Mock()
                mock_products.IsValid.return_value = True
                children = []
                for product_name, type_name in products_setup:
                    child = mock.Mock()
                    child.GetTypeName.return_value = type_name
                    attr = mock.Mock()
                    attr.Get.return_value = product_name
                    child.GetAttribute.return_value = attr
                    children.append(child)
                mock_products.GetChildren.return_value = children
                mock_stage.GetPrimAtPath.return_value = mock_products

            result = _get_usd_output_directories("/scene/scene.usda")

        assert result == expected

    def test_invalid_stage_returns_empty(self):
        """Verify no crash when stage fails to open."""
        with mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.Usd"
        ) as mock_usd:
            mock_usd.Stage.Open.return_value = None

            result = _get_usd_output_directories("/nonexistent.usda")

        assert result == set()


class TestSceneAssetReferencesUsdTraversal:
    """Tests for USD traversal integration in _get_scene_asset_references()"""

    def test_usd_file_triggers_traversal(self):
        """Verify that a .usda file in hou.fileReferences() triggers USD traversal."""
        node = hou.node
        hou.hipFile.path.return_value = "/scene/test.hip"

        usd_parm = mock.Mock()
        usd_parm.node.return_value = None
        usd_parm.name.return_value = "file1"
        usd_parm.evalAsString.return_value = "/scene/scene.usda"

        hou.fileReferences.return_value = [(usd_parm, "/scene/scene.usda")]

        mock_os = mock.Mock()
        mock_os.path.isdir = lambda p: False
        mock_os.path.isfile = lambda p: True
        mock_os.path.splitext = os.path.splitext
        mock_os.path.realpath = os.path.realpath
        mock_os.path.dirname = os.path.dirname

        with (
            mock.patch(
                "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.os",
                mock_os,
            ),
            mock.patch(
                "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._get_usd_asset_references"
            ) as mock_usd_refs,
        ):
            mock_usd_refs.return_value = (
                {"/scene/model.usda", "/scene/tex.exr"},
                {"/renders"},
                [],
            )
            asset_refs = _get_scene_asset_references(node)

        mock_usd_refs.assert_called_once()
        assert "/scene/model.usda" in asset_refs.input_filenames
        assert "/scene/tex.exr" in asset_refs.input_filenames
        assert "/renders" in asset_refs.output_directories

    def test_no_usd_files_skips_traversal(self):
        """Verify that non-USD scenes skip USD traversal entirely."""
        node = hou.node
        hou.hipFile.path.return_value = "/scene/test.hip"

        png_parm = mock.Mock()
        png_parm.node.return_value = None
        png_parm.name.return_value = "file1"
        png_parm.evalAsString.return_value = "/scene/texture.png"

        hou.fileReferences.return_value = [(png_parm, "/scene/texture.png")]

        mock_os = mock.Mock()
        mock_os.path.isdir = lambda p: False
        mock_os.path.isfile = lambda p: True
        mock_os.path.splitext = os.path.splitext
        mock_os.path.realpath = os.path.realpath

        with (
            mock.patch(
                "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.os",
                mock_os,
            ),
            mock.patch(
                "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets._get_usd_asset_references"
            ) as mock_usd_refs,
        ):
            _get_scene_asset_references(node)

        mock_usd_refs.assert_not_called()
