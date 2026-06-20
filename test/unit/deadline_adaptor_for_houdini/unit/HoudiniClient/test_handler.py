# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest
from .mock_hou import hou_module as hou

from deadline.houdini_adaptor.HoudiniClient.houdini_handler import HoudiniHandler


@pytest.fixture(autouse=True)
def reset_hou_mocks():
    for each in [d for d in dir(hou) if not d.startswith("__")]:
        attr = getattr(hou, each)
        if hasattr(attr, "reset_mock"):
            attr.reset_mock()
        else:
            del attr


class TestHoudiniHandler:
    def test_init(self) -> None:
        """ """
        # WHEN
        handler = HoudiniHandler()

        assert list(handler.action_dict.keys()) == [
            "scene_file",
            "render_node",
            "ignore_input_nodes",
            "wedge_node",
            "wedgenum",
            "start_render",
        ]
        assert handler.render_kwargs["ignore_input_nodes"]
        assert not handler.node

    def test_start_render_fail_no_node_set(self) -> None:
        handler = HoudiniHandler()
        handler.render_kwargs["frame"] = 1

        hou.node.type().nameWithCategory.return_value = "Driver/other"
        hou.renderMethod.RopByRop = None

        with pytest.raises(TypeError):
            handler.start_render({})

    def test_start_render(self) -> None:
        handler = HoudiniHandler()
        handler.render_kwargs["frame"] = 1
        handler.node = hou.node
        hou.node.type().nameWithCategory.return_value = "Driver/other"
        hou.renderMethod.RopByRop = None
        handler.start_render({"frame_range": {"start": 1, "end": 5, "step": 2}})
        hou.node.render.assert_called_once_with(
            verbose=True, frame_range=(1, 5, 2), ignore_inputs=True, method=None
        )

    def test_set_ignore_input_nodes(self) -> None:
        handler = HoudiniHandler()
        assert handler.render_kwargs["ignore_input_nodes"]
        data = {"ignore_input_nodes": False}
        handler.set_ignore_input_nodes(data)
        assert not handler.render_kwargs["ignore_input_nodes"]

    def test_set_render_node(self) -> None:
        handler = HoudiniHandler()
        hou.node.type().nameWithCategory.return_value = "test"
        data = {"render_node": "test"}
        assert not handler.node
        hou.node.return_value = "test"
        handler.set_render_node(data)
        assert handler.node
        assert handler.node == "test"

    def test_set_scene_file_not_found(self, capfd) -> None:
        handler = HoudiniHandler()
        data = {"scene_file": "/not/a/real/scene.hip"}
        with pytest.raises(FileNotFoundError):
            # test file not found
            handler.set_scene_file(data)

    @patch("deadline.houdini_adaptor.HoudiniClient.houdini_handler.HoudiniHandler._path_map_envs")
    def test_set_scene_file_loads(self, patch_pathmap, capfd) -> None:
        handler = HoudiniHandler()
        data = {"scene_file": "/not/a/real/scene.hip"}
        with patch(
            "deadline.houdini_adaptor.HoudiniClient.houdini_handler.os.path.isfile",
        ) as mock_isfile:
            mock_isfile.return_value = True
            # test file loads
            handler.set_scene_file(data)
            hou.hipFile.load.assert_called_once()

    @patch("deadline.houdini_adaptor.HoudiniClient.houdini_handler.HoudiniHandler._path_map_envs")
    def test_set_scene_file_raise_exception(self, patch_pathmap, capfd) -> None:
        handler = HoudiniHandler()
        data = {"scene_file": "/not/a/real/scene.hip"}
        with patch(
            "deadline.houdini_adaptor.HoudiniClient.houdini_handler.os.path.isfile"
        ) as mock_isfile:
            mock_isfile.return_value = True
            hou.LoadWarning = Exception  # type: ignore
            hou.hipFile.load.side_effect = Exception("test")
            # test exception fired
            handler.set_scene_file(data)
            out, _ = capfd.readouterr()
            assert out[-5:] == "test\n"

    # test case for code coverage this tests
    # code that will be removed to the common node library
    @pytest.mark.parametrize("driver_node", ["Driver/other", "NotDriver", "Driver"])
    def test_set_node_settings_driver_unrecognized_no_change(self, capfd, driver_node):
        handler = HoudiniHandler()
        hou.node.type().nameWithCategory.return_value = driver_node
        parm_mock = Mock(name="testparm")
        hou.node.parm = parm_mock

        handler.set_node_settings(hou.node)

        assert parm_mock().eval.call_count == 0
        assert parm_mock().set.call_count == 0

    @pytest.mark.parametrize("low_verbosity", [0, 1])
    def test_set_node_settings_driver_ifd_low_setting_increased_to_2(self, capfd, low_verbosity):
        handler = HoudiniHandler()

        hou.node.type().nameWithCategory.return_value = "Driver/ifd"
        alfred_parm_mock = Mock(name="alfredparm")
        verbosity_parm_mock = Mock(name="verbosityparm")
        verbosity_parm_mock.eval.side_effect = [low_verbosity, low_verbosity, 2, 2, 2]
        parm_func_mock = Mock(side_effect=[alfred_parm_mock, verbosity_parm_mock])
        hou.node.parm = parm_func_mock

        handler.set_node_settings(hou.node)

        assert parm_func_mock.call_count == 2
        alfred_parm_mock.set.assert_called_once_with(1)
        verbosity_parm_mock.set.assert_called_once_with(2)
        out, _ = capfd.readouterr()
        assert (
            "Enabled Alfred style progress\nIncreased verbosity to 2 to include basic logging\nLogging verbosity is set to 2\n"
            in out
        )

    @pytest.mark.parametrize("high_verbosity", [2, 3, 4, 5])
    def test_set_node_settings_driver_ifd_high_verbosity_no_change(self, capfd, high_verbosity):
        handler = HoudiniHandler()

        hou.node.type().nameWithCategory.return_value = "Driver/ifd"
        alfred_parm_mock = Mock(name="alfredparm")
        verbosity_parm_mock = Mock(name="verbosityparm")
        verbosity_parm_mock.eval.return_value = high_verbosity
        parm_func_mock = Mock(side_effect=[alfred_parm_mock, verbosity_parm_mock])
        hou.node.parm = parm_func_mock

        handler.set_node_settings(hou.node)

        assert parm_func_mock.call_count == 2
        alfred_parm_mock.set.assert_called_once_with(1)
        verbosity_parm_mock.set.assert_not_called()
        out, _ = capfd.readouterr()
        assert (
            f"Enabled Alfred style progress\nLogging verbosity is set to {high_verbosity}\n" in out
        )

    def test_set_node_settings_driver_ifd_keyed_parameter_correct_log_messages(self, capfd):
        handler = HoudiniHandler()

        hou.node.type().nameWithCategory.return_value = "Driver/ifd"
        alfred_parm_mock = Mock(name="alfredparm")
        verbosity_parm_mock = Mock(name="verbosityparm")
        verbosity_parm_mock.eval.return_value = 0
        parm_func_mock = Mock(side_effect=[alfred_parm_mock, verbosity_parm_mock])
        hou.node.parm = parm_func_mock

        handler.set_node_settings(hou.node)

        assert parm_func_mock.call_count == 2
        alfred_parm_mock.set.assert_called_once_with(1)
        verbosity_parm_mock.set.assert_called_once_with(2)
        out, _ = capfd.readouterr()
        assert "include basic logging" not in out
        assert "Enabled Alfred style progress\nLogging verbosity is set to 0\n" in out

    def test_set_node_settings_driver_usdrender_none_increased_to_3(self, capfd):
        handler = HoudiniHandler()

        hou.node.type().nameWithCategory.return_value = "Driver/usdrender"
        alfred_parm_mock = Mock(name="alfredparm")
        verbosity_parm_mock = Mock(name="verbosityparm")
        verbosity_parm_mock.eval.side_effect = ["", "", "3", "3", "3"]
        parm_func_mock = Mock(side_effect=[alfred_parm_mock, verbosity_parm_mock])
        hou.node.parm = parm_func_mock

        handler.set_node_settings(hou.node)

        assert parm_func_mock.call_count == 2
        alfred_parm_mock.set.assert_called_once_with(1)
        verbosity_parm_mock.set.assert_called_once_with("3")
        out, _ = capfd.readouterr()
        assert (
            "Enabled Alfred style progress\nIncreased verbosity to '3' to include basic logging\nLogging verbosity is set to '3'\n"
            in out
        )

    @pytest.mark.parametrize("verbosity", ["3", "9", "9p", "9P"])
    def test_set_node_settings_driver_usdrender_high_verbosity_not_changed(self, capfd, verbosity):
        handler = HoudiniHandler()

        hou.node.type().nameWithCategory.return_value = "Driver/usdrender"
        alfred_parm_mock = Mock(name="alfredparm")
        verbosity_parm_mock = Mock(name="verbosityparm")
        verbosity_parm_mock.eval.return_value = verbosity
        parm_func_mock = Mock(side_effect=[alfred_parm_mock, verbosity_parm_mock])
        hou.node.parm = parm_func_mock

        handler.set_node_settings(hou.node)

        assert parm_func_mock.call_count == 2
        alfred_parm_mock.set.assert_called_once_with(1)
        verbosity_parm_mock.set.assert_not_called()
        out, _ = capfd.readouterr()
        assert f"Enabled Alfred style progress\nLogging verbosity is set to '{verbosity}'\n" in out

    def test_set_node_settings_driver_usdrender_keyed_parameter_correct_log_messages(self, capfd):
        handler = HoudiniHandler()

        hou.node.type().nameWithCategory.return_value = "Driver/usdrender"
        alfred_parm_mock = Mock(name="alfredparm")
        verbosity_parm_mock = Mock(name="verbosityparm")
        verbosity_parm_mock.eval.return_value = ""
        parm_func_mock = Mock(side_effect=[alfred_parm_mock, verbosity_parm_mock])
        hou.node.parm = parm_func_mock

        handler.set_node_settings(hou.node)

        assert parm_func_mock.call_count == 2
        alfred_parm_mock.set.assert_called_with(1)
        verbosity_parm_mock.set.assert_called_with("3")
        out, _ = capfd.readouterr()
        assert "include basic logging" not in out
        assert "Enabled Alfred style progress\nLogging verbosity is set to ''\n" in out

    @pytest.mark.parametrize("driver_node", ["Driver/ifd", "Driver/usdrender"])
    def test_do_not_set_node_settings_for_none_parm(self, capfd, driver_node):
        handler = HoudiniHandler()

        hou.node.type().nameWithCategory.return_value = driver_node
        parm_mock = Mock(name="both_parms", return_value=None)
        hou.node.parm = parm_mock

        handler.set_node_settings(hou.node)

        out, _ = capfd.readouterr()
        assert "Enabled Alfred style progress\n" not in out


class TestRemapLopFilePaths:
    """Tests for HoudiniHandler._remap_lop_file_paths (fix for issue #314)"""

    def _setup_lop_nodes(self, parms):
        """Helper to mock hou.node('/').recursiveGlob returning nodes with given parms."""
        mock_node = Mock()
        mock_node.parms.return_value = parms if isinstance(parms, list) else [parms]
        mock_root = MagicMock()
        mock_root.recursiveGlob.return_value = [mock_node]
        hou.node.return_value = mock_root
        return mock_node

    def _make_parm(self, name, string_type, unexpanded_string):
        """Helper to create a mock parm with template metadata."""
        parm = Mock(name=f"parm_{name}")
        parm.name.return_value = name
        parm.path.return_value = f"/stage/node/{name}"
        parm.unexpandedString.return_value = unexpanded_string
        template = Mock()
        template.type.return_value = hou.parmTemplateType.String
        template.stringType.return_value = string_type
        parm.parmTemplate.return_value = template
        return parm

    @patch.dict("os.environ", {}, clear=True)
    def test_early_return_no_pathmap(self):
        """Does nothing when HOUDINI_PATHMAP is not set."""
        handler = HoudiniHandler()
        handler._remap_lop_file_paths()
        hou.node.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_remaps_file_reference_parm(self, capfd):
        """Remaps parms with stringType == FileReference (sublayer case)."""
        handler = HoudiniHandler()
        parm = self._make_parm("filepath1", hou.stringParmType.FileReference, "/src/asset.usd")
        self._setup_lop_nodes(parm)
        hou.hscript.return_value = ("/dest/asset.usd\n", "")

        handler._remap_lop_file_paths()

        parm.set.assert_called_once_with("/dest/asset.usd")
        out, _ = capfd.readouterr()
        assert "/src/asset.usd -> /dest/asset.usd" in out

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_remaps_filepath_named_parm_not_tagged_as_file_reference(self, capfd):
        """Remaps parms named 'filepath*' even if stringType is Regular (reference node case)."""
        handler = HoudiniHandler()
        parm = self._make_parm("filepath1", hou.stringParmType.Regular, "/src/model.usd")
        self._setup_lop_nodes(parm)
        hou.hscript.return_value = ("/dest/model.usd\n", "")

        handler._remap_lop_file_paths()

        parm.set.assert_called_once_with("/dest/model.usd")

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_skips_empty_parm_values(self):
        """Does not call hscript for empty parm values."""
        handler = HoudiniHandler()
        parm = self._make_parm("filepath1", hou.stringParmType.FileReference, "")
        self._setup_lop_nodes(parm)

        handler._remap_lop_file_paths()

        hou.hscript.assert_not_called()
        parm.set.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    @pytest.mark.parametrize("expression", ["$HIP/assets/mesh.usd", "`chs('path')`"])
    def test_skips_expression_parms(self, expression):
        """Does not remap parms starting with $ or backtick (expression-based paths)."""
        handler = HoudiniHandler()
        parm = self._make_parm("filepath1", hou.stringParmType.FileReference, expression)
        self._setup_lop_nodes(parm)

        handler._remap_lop_file_paths()

        hou.hscript.assert_not_called()
        parm.set.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_skips_non_string_parms(self):
        """Ignores non-String parm types (int, float, etc.)."""
        handler = HoudiniHandler()
        parm = Mock(name="int_parm")
        template = Mock()
        template.type.return_value = hou.parmTemplateType.Int
        parm.parmTemplate.return_value = template
        self._setup_lop_nodes(parm)

        handler._remap_lop_file_paths()

        hou.hscript.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_no_op_when_path_unchanged(self):
        """Does not call parm.set when pathmap returns the same path."""
        handler = HoudiniHandler()
        parm = self._make_parm("filepath1", hou.stringParmType.FileReference, "/other/path.usd")
        self._setup_lop_nodes(parm)
        hou.hscript.return_value = ("/other/path.usd\n", "")

        handler._remap_lop_file_paths()

        parm.set.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_handles_hscript_error(self, capfd):
        """Prints error and continues when hscript pathmap fails."""
        handler = HoudiniHandler()
        parm = self._make_parm("filepath1", hou.stringParmType.FileReference, "/src/bad.usd")
        self._setup_lop_nodes(parm)
        hou.hscript.return_value = ("", "pathmap: error\n")

        handler._remap_lop_file_paths()

        parm.set.assert_not_called()
        out, _ = capfd.readouterr()
        assert "Error remapping" in out
