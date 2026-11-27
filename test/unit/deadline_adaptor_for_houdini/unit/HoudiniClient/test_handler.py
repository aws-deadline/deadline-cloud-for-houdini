# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

from typing import Any, Callable
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


def _fake_pathmap(source: str, destination: str) -> Callable[[str], tuple[str, str]]:
    """Returns a hou.hscript stand-in for "pathmap -t '<path>' -c" with one rule. Like
    Houdini, it maps only a path that starts with the rule's forward-slash source, and
    echoes any other path unchanged."""

    def hscript(command: str) -> tuple[str, str]:
        # Pull the path out of the quoted pathmap argument
        path: str = command.split("'")[1]
        # Swap the rule's source for its destination, or echo an uncovered path
        if path.startswith(source):
            return (destination + path[len(source) :] + "\n", "")
        return (path + "\n", "")

    return hscript


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

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_skips_keyframed_parm(self, capfd):
        """Skips parms whose unexpandedString raises hou.OperationFailed (keyframed)."""
        handler = HoudiniHandler()
        parm = self._make_parm("filepath1", hou.stringParmType.FileReference, "/src/asset.usd")
        parm.unexpandedString.side_effect = hou.OperationFailed("Cannot get unexpanded string")
        parm.evalAsString.return_value = "/src/asset.usd"
        self._setup_lop_nodes(parm)

        handler._remap_lop_file_paths()

        parm.set.assert_not_called()
        out, _ = capfd.readouterr()
        assert "Skipping LOP parm" in out
        assert "(keyframed)" in out
        assert "will not be path-mapped" in out

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_continues_after_keyframed_parm(self, capfd):
        """Loop continues to remap subsequent parms after one raises OperationFailed."""
        handler = HoudiniHandler()
        keyframed_parm = self._make_parm(
            "filepath1", hou.stringParmType.FileReference, "/src/anim.usd"
        )
        keyframed_parm.unexpandedString.side_effect = hou.OperationFailed(
            "Cannot get unexpanded string"
        )
        keyframed_parm.evalAsString.return_value = "/src/anim.usd"
        normal_parm = self._make_parm(
            "filepath2", hou.stringParmType.FileReference, "/src/asset.usd"
        )
        self._setup_lop_nodes([keyframed_parm, normal_parm])
        hou.hscript.return_value = ("/dest/asset.usd\n", "")

        handler._remap_lop_file_paths()

        keyframed_parm.set.assert_not_called()
        normal_parm.set.assert_called_once_with("/dest/asset.usd")
        out, _ = capfd.readouterr()
        assert "Skipping LOP parm" in out
        assert "(keyframed)" in out
        assert "/src/asset.usd -> /dest/asset.usd" in out

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_skips_locked_parm(self, capfd):
        """Skips parms whose set() raises hou.PermissionError (locked HDA)."""
        handler = HoudiniHandler()
        parm = self._make_parm("filepath1", hou.stringParmType.FileReference, "/src/asset.usd")
        parm.set.side_effect = hou.PermissionError("Permission denied: locked HDA")
        self._setup_lop_nodes(parm)
        hou.hscript.return_value = ("/dest/asset.usd\n", "")

        handler._remap_lop_file_paths()

        out, _ = capfd.readouterr()
        assert "Skipping LOP parm /stage/node/filepath1" in out

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": '{"/src": "/dest"}'})
    def test_continues_after_locked_parm(self, capfd):
        """Loop continues to remap subsequent parms after one raises PermissionError."""
        handler = HoudiniHandler()
        locked_parm = self._make_parm(
            "filepath1", hou.stringParmType.FileReference, "/src/locked.usd"
        )
        locked_parm.set.side_effect = hou.PermissionError("Permission denied: locked HDA")
        normal_parm = self._make_parm(
            "filepath2", hou.stringParmType.FileReference, "/src/asset.usd"
        )
        self._setup_lop_nodes([locked_parm, normal_parm])
        hou.hscript.return_value = ("/dest/asset.usd\n", "")

        handler._remap_lop_file_paths()

        normal_parm.set.assert_called_once_with("/dest/asset.usd")
        out, _ = capfd.readouterr()
        assert "Skipping LOP parm /stage/node/filepath1" in out
        assert "/src/asset.usd -> /dest/asset.usd" in out

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("P:/assets", "/worker/assets"))
    )
    def test_remaps_backslash_windows_path(self, capfd: pytest.CaptureFixture[str]) -> None:
        """A LOP file parm holding a Windows path typed with backslashes maps through the
        forward-slash rule, like the same path typed with forward slashes. Catches USD
        receiving an unmapped Windows path from a Sublayer or Reference LOP."""
        # GIVEN a Sublayer LOP whose file path uses backslashes, under a rule's source
        handler: HoudiniHandler = HoudiniHandler()
        parm: Mock = self._make_parm(
            "filepath1", hou.stringParmType.FileReference, "P:\\assets\\layout.usda"
        )
        self._setup_lop_nodes(parm)

        # WHEN the LOP parms are remapped
        handler._remap_lop_file_paths()

        # THEN the parm is set to the mapped path, and the log shows the value as typed
        parm.set.assert_called_once_with("/worker/assets/layout.usda")
        out: str = capfd.readouterr().out
        assert "P:\\assets\\layout.usda -> /worker/assets/layout.usda" in out


class TestRemapSopFilePaths:
    """Tests that loading a scene path-maps the file parms of SOP nodes, such as the USD
    Import SOP, whose paths USD opens without HOUDINI_PATHMAP."""

    def _make_sop_node(
        self,
        parms: list[Mock],
        inside_locked_hda: bool = False,
        editable_inside_locked_hda: bool = False,
        parent: Mock | None = None,
    ) -> Mock:
        """Helper to create a mock SOP node with the given parms and HDA lock state. A node
        inside a locked HDA with no parent given gets a SOP-level HDA with no parms."""
        # A node with these parms, inside or outside a locked HDA as asked
        node: Mock = Mock(name="sop_node")
        node.parms.return_value = parms
        node.isInsideLockedHDA.return_value = inside_locked_hda
        node.isEditableInsideLockedHDA.return_value = editable_inside_locked_hda
        # The node it sits in, which is a locked HDA when the node is inside one
        default_parent: Mock | None = (
            self._make_hda_node("Sop", "/obj/geo1/hda1", []) if inside_locked_hda else None
        )
        node.parent.return_value = parent or default_parent
        return node

    def _make_hda_node(
        self,
        category: str,
        path: str,
        parms: list[Mock],
        parent: Mock | None = None,
    ) -> Mock:
        """Helper to create a mock locked HDA instance in the given node context. With a
        parent, it is itself inside that locked HDA."""
        # An HDA node with these parms, whose type is in the given context
        node: Mock = Mock(name=f"hda_{path}")
        node.path.return_value = path
        node.parms.return_value = parms
        node.type.return_value.category.return_value.name.return_value = category
        # Inside a locked HDA only when it has a parent
        node.isInsideLockedHDA.return_value = parent is not None
        node.parent.return_value = parent
        return node

    def _make_parm(self, name: str, unexpanded_string: str) -> Mock:
        """Helper to create a mock file reference string parm."""
        # A parm holding the given raw value, under a USD Import SOP's path
        parm: Mock = Mock(name=f"parm_{name}")
        parm.name.return_value = name
        parm.path.return_value = f"/obj/geo1/usdimport1/{name}"
        parm.unexpandedString.return_value = unexpanded_string

        # Its template marks it as a file reference string, as a File parm is
        template: Mock = Mock()
        template.type.return_value = hou.parmTemplateType.String
        template.stringType.return_value = hou.stringParmType.FileReference
        parm.parmTemplate.return_value = template
        return parm

    def _scene_with_sop_nodes(
        self, sop_nodes: list[Mock], lop_nodes: list[Mock] | None = None
    ) -> None:
        """Helper to make hou.node('/').recursiveGlob return the given nodes for the SOP
        filter, and the given LOP nodes, or none, for the LOP filter."""

        # Answer the SOP and LOP filters with their nodes, and any other filter with none
        def glob(pattern: str, filter: Any) -> list[Mock]:
            if filter is hou.nodeTypeFilter.Sop:
                return sop_nodes
            if filter is hou.nodeTypeFilter.Lop:
                return lop_nodes or []
            return []

        # Make hou.node("/") the root that answers recursiveGlob that way
        root: MagicMock = MagicMock()
        root.recursiveGlob.side_effect = glob
        hou.node.return_value = root

    def _load_scene(self) -> None:
        """Helper to run the scene_file action on a scene file that exists."""
        # Load with the file present and the $JOB and $POSE mapping stubbed out, so the
        # parm remapping is the only path mapping that runs
        handler: HoudiniHandler = HoudiniHandler()
        with (
            patch(
                "deadline.houdini_adaptor.HoudiniClient.houdini_handler.os.path.isfile",
                return_value=True,
            ),
            patch(
                "deadline.houdini_adaptor.HoudiniClient.houdini_handler.HoudiniHandler._path_map_envs"
            ),
        ):
            handler.set_scene_file({"scene_file": "/scene.hip"})

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    def test_scene_load_remaps_usd_import_sop_file_path(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A USD Import SOP's absolute filepath1 under a rule is set to the mapped path when
        the scene loads, and logged. Catches the SOP path reaching USD unmapped, which
        fails the cook with 'Invalid source .../usdimport1/lopimport1'."""
        # GIVEN a USD Import SOP whose File parm is under the rule's source
        parm: Mock = self._make_parm("filepath1", "P:/assets/cables.usda")
        self._scene_with_sop_nodes([self._make_sop_node([parm])])
        hou.hscript.return_value = ("/worker/assets/cables.usda\n", "")

        # WHEN the scene loads
        self._load_scene()

        # THEN the raw value goes through pathmap, and the mapped path is set and logged
        hou.hscript.assert_called_once_with("pathmap -t 'P:/assets/cables.usda' -c")
        parm.set.assert_called_once_with("/worker/assets/cables.usda")
        out: str = capfd.readouterr().out
        assert (
            "Remapped SOP parm /obj/geo1/usdimport1/filepath1: "
            "P:/assets/cables.usda -> /worker/assets/cables.usda" in out
        )

    @patch.dict("os.environ", {}, clear=True)
    def test_scene_load_without_rules_changes_no_sop_parm(self) -> None:
        """With HOUDINI_PATHMAP unset, no SOP parm is read or changed. Catches remapping
        that runs when the job has no path mapping rules."""
        # GIVEN a USD Import SOP with an absolute path, and no rules
        parm: Mock = self._make_parm("filepath1", "P:/assets/cables.usda")
        self._scene_with_sop_nodes([self._make_sop_node([parm])])

        # WHEN the scene loads
        self._load_scene()

        # THEN no node is looked up and the parm keeps its value
        hou.node.assert_not_called()
        parm.set.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    def test_scene_load_skips_sop_nodes_inside_locked_hda(self) -> None:
        """A SOP inside a locked HDA is not path-mapped, so its parms keep the values the
        HDA gives them. Catches setting parms the HDA derives from its own parms."""
        # GIVEN a SOP inside a locked HDA, not marked editable, with a mappable path
        parm: Mock = self._make_parm("file", "P:/assets/cables.bgeo.sc")
        self._scene_with_sop_nodes([self._make_sop_node([parm], inside_locked_hda=True)])
        hou.hscript.return_value = ("/worker/assets/cables.bgeo.sc\n", "")

        # WHEN the scene loads
        self._load_scene()

        # THEN its parm is neither mapped nor set
        hou.hscript.assert_not_called()
        parm.set.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    def test_scene_load_remaps_editable_sop_node_inside_locked_hda(self) -> None:
        """A SOP marked editable inside a locked HDA holds its own value, so it is still
        path-mapped. Catches skipping every node inside a locked HDA."""
        # GIVEN an editable SOP inside a locked HDA, with a mappable path
        parm: Mock = self._make_parm("file", "P:/assets/cables.bgeo.sc")
        node: Mock = self._make_sop_node(
            [parm], inside_locked_hda=True, editable_inside_locked_hda=True
        )
        self._scene_with_sop_nodes([node])
        hou.hscript.return_value = ("/worker/assets/cables.bgeo.sc\n", "")

        # WHEN the scene loads
        self._load_scene()

        # THEN its parm is set to the mapped path
        parm.set.assert_called_once_with("/worker/assets/cables.bgeo.sc")

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    def test_scene_load_skips_keyframed_sop_parm_and_maps_the_rest(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A keyframed SOP file parm is skipped and logged, and the next parm is still
        remapped. Catches one uncooperative SOP parm aborting scene load or the rest of
        the path mapping."""
        # GIVEN a keyframed parm, which has no single raw value, before a plain one
        keyframed: Mock = self._make_parm("filepath1", "P:/assets/anim.usda")
        keyframed.unexpandedString.side_effect = hou.OperationFailed("keyframed")
        keyframed.evalAsString.return_value = "P:/assets/anim.usda"
        normal: Mock = self._make_parm("filepath2", "P:/assets/cables.usda")
        self._scene_with_sop_nodes([self._make_sop_node([keyframed, normal])])
        hou.hscript.return_value = ("/worker/assets/cables.usda\n", "")

        # WHEN the scene loads
        self._load_scene()

        # THEN the keyframed parm is left and logged, and the plain one is mapped
        keyframed.set.assert_not_called()
        normal.set.assert_called_once_with("/worker/assets/cables.usda")
        out: str = capfd.readouterr().out
        assert (
            "Skipping SOP parm /obj/geo1/usdimport1/filepath1 (keyframed): "
            "P:/assets/anim.usda will not be path-mapped" in out
        )

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    def test_scene_load_skips_sop_parm_that_cannot_be_set(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A SOP parm whose set() raises is skipped and logged without aborting scene
        load. Catches a locked parm failing the whole task."""
        # GIVEN a mappable parm that refuses to be set
        parm: Mock = self._make_parm("filepath1", "P:/assets/cables.usda")
        parm.set.side_effect = hou.PermissionError("locked")
        self._scene_with_sop_nodes([self._make_sop_node([parm])])
        hou.hscript.return_value = ("/worker/assets/cables.usda\n", "")

        # WHEN the scene loads
        self._load_scene()

        # THEN the load completes and the skip is logged with the reason
        out: str = capfd.readouterr().out
        assert "Skipping SOP parm /obj/geo1/usdimport1/filepath1: locked" in out

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("P:/assets", "/worker/assets"))
    )
    def test_scene_load_remaps_backslash_windows_path_in_sop_parm(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A USD Import SOP's filepath1 holding a Windows path typed with backslashes maps
        through the forward-slash rule. Catches the pasted Windows path reaching USD
        unmapped, which fails the cook with 'Invalid source .../usdimport1/lopimport1'."""
        # GIVEN a USD Import SOP whose File parm uses backslashes, under a rule's source
        parm: Mock = self._make_parm("filepath1", "P:\\assets\\cables.usda")
        self._scene_with_sop_nodes([self._make_sop_node([parm])])

        # WHEN the scene loads
        self._load_scene()

        # THEN the mapped path is set, and the log shows the value as typed
        parm.set.assert_called_once_with("/worker/assets/cables.usda")
        out: str = capfd.readouterr().out
        assert (
            "Remapped SOP parm /obj/geo1/usdimport1/filepath1: "
            "P:\\assets\\cables.usda -> /worker/assets/cables.usda" in out
        )

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("P:/assets", "/worker/assets"))
    )
    def test_scene_load_remaps_windows_path_with_mixed_slashes(self) -> None:
        """A Windows path with a forward slash after the drive and backslashes after that
        maps like the all-forward-slash path. Catches recognizing only "X:\\" as the
        start of a Windows path."""
        # GIVEN a File parm with both kinds of slash, under a rule's source
        parm: Mock = self._make_parm("filepath1", "P:/assets\\cables.usda")
        self._scene_with_sop_nodes([self._make_sop_node([parm])])

        # WHEN the scene loads
        self._load_scene()

        # THEN the mapped path is set
        parm.set.assert_called_once_with("/worker/assets/cables.usda")

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'//server/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("//server/assets", "/worker/assets"))
    )
    def test_scene_load_remaps_backslash_unc_path(self) -> None:
        """A UNC path typed with backslashes maps through the forward-slash rule the
        adaptor writes for it. Catches recognizing only drive-letter Windows paths."""
        # GIVEN a File parm with a UNC path, under a rule's source
        parm: Mock = self._make_parm("filepath1", "\\\\server\\assets\\cables.usda")
        self._scene_with_sop_nodes([self._make_sop_node([parm])])

        # WHEN the scene loads
        self._load_scene()

        # THEN the mapped path is set
        parm.set.assert_called_once_with("/worker/assets/cables.usda")

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("P:/assets", "/worker/assets"))
    )
    def test_scene_load_leaves_uncovered_backslash_path_unchanged(self) -> None:
        """A backslash Windows path that no rule covers keeps its value. Catches writing
        the forward-slash form back into a parm that no rule mapped."""
        # GIVEN a File parm with a backslash path outside every rule's source
        parm: Mock = self._make_parm("filepath1", "Q:\\other\\cables.usda")
        self._scene_with_sop_nodes([self._make_sop_node([parm])])

        # WHEN the scene loads
        self._load_scene()

        # THEN the parm is not set
        parm.set.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'/assets': '/worker/assets'}"})
    @patch.object(hou, "hscript", new=Mock(side_effect=_fake_pathmap("/assets", "/worker/assets")))
    def test_scene_load_keeps_backslash_in_posix_path(self) -> None:
        """A POSIX path keeps its backslashes when it is looked up, since there a
        backslash is part of a name. Catches rewriting backslashes in every path."""
        # GIVEN a File parm with a POSIX path whose file name holds a backslash
        parm: Mock = self._make_parm("filepath1", "/assets/a\\b.usda")
        self._scene_with_sop_nodes([self._make_sop_node([parm])])

        # WHEN the scene loads
        self._load_scene()

        # THEN pathmap gets the path as typed, and the mapped path keeps the backslash
        hou.hscript.assert_called_once_with("pathmap -t '/assets/a\\b.usda' -c")
        parm.set.assert_called_once_with("/worker/assets/a\\b.usda")

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("P:/assets", "/worker/assets"))
    )
    def test_scene_load_remaps_obj_hda_that_holds_locked_usd_import_sop(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """An OBJ-level locked HDA that wraps a USD Import SOP and promotes its File parm
        has the promoted parm path-mapped, and logged. Catches skipping the SOP inside the
        locked HDA without remapping the HDA, which no LOP or SOP pass visits, so USD
        opens the unmapped path and fails with 'Invalid source .../lopimport1'."""
        # GIVEN an OBJ HDA whose promoted File parm holds a covered Windows path
        hda_parm: Mock = self._make_parm("filepath1", "P:\\assets\\cables.usda")
        hda_parm.path.return_value = "/obj/asset1/filepath1"
        hda: Mock = self._make_hda_node("Object", "/obj/asset1", [hda_parm])
        # AND the USD Import SOP inside it, whose own parm can't be set
        inner_parm: Mock = self._make_parm("filepath1", "P:/assets/inner.usda")
        self._scene_with_sop_nodes(
            [self._make_sop_node([inner_parm], inside_locked_hda=True, parent=hda)]
        )

        # WHEN the scene loads
        self._load_scene()

        # THEN the HDA's parm is set to the mapped path and logged, and the inner one is not
        hda_parm.set.assert_called_once_with("/worker/assets/cables.usda")
        inner_parm.set.assert_not_called()
        out: str = capfd.readouterr().out
        assert (
            "Remapped HDA parm /obj/asset1/filepath1: "
            "P:\\assets\\cables.usda -> /worker/assets/cables.usda" in out
        )

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("P:/assets", "/worker/assets"))
    )
    def test_scene_load_remaps_only_outermost_of_nested_locked_hdas(self) -> None:
        """With a locked HDA nested in another, only the outermost HDA's file parms are
        path-mapped. Catches remapping the inner HDA, whose parms can't be set, or
        stopping at the inner HDA and leaving the outer HDA's promoted path unmapped."""
        # GIVEN an OBJ HDA holding a nested locked HDA, both with a covered File parm
        outer_parm: Mock = self._make_parm("filepath1", "P:/assets/cables.usda")
        outer: Mock = self._make_hda_node("Object", "/obj/asset1", [outer_parm])
        inner_hda_parm: Mock = self._make_parm("filepath1", "P:/assets/inner.usda")
        inner_hda: Mock = self._make_hda_node(
            "Object", "/obj/asset1/inner1", [inner_hda_parm], parent=outer
        )
        # AND a USD Import SOP inside the nested HDA
        sop_parm: Mock = self._make_parm("filepath1", "P:/assets/sop.usda")
        self._scene_with_sop_nodes(
            [self._make_sop_node([sop_parm], inside_locked_hda=True, parent=inner_hda)]
        )

        # WHEN the scene loads
        self._load_scene()

        # THEN only the outermost HDA's parm is set
        outer_parm.set.assert_called_once_with("/worker/assets/cables.usda")
        inner_hda_parm.set.assert_not_called()
        sop_parm.set.assert_not_called()

    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("P:/assets", "/worker/assets"))
    )
    def test_scene_load_remaps_hda_once_for_several_locked_sops(self) -> None:
        """An HDA holding several locked SOPs is path-mapped once. Catches the HDA's
        parms being looked up and set again for each SOP inside it."""
        # GIVEN an OBJ HDA with a covered File parm, holding two locked SOPs
        hda_parm: Mock = self._make_parm("filepath1", "P:/assets/cables.usda")
        hda: Mock = self._make_hda_node("Object", "/obj/asset1", [hda_parm])
        self._scene_with_sop_nodes(
            [
                self._make_sop_node([], inside_locked_hda=True, parent=hda),
                self._make_sop_node([], inside_locked_hda=True, parent=hda),
            ]
        )

        # WHEN the scene loads
        self._load_scene()

        # THEN the HDA's parm is looked up and set once
        hou.hscript.assert_called_once_with("pathmap -t 'P:/assets/cables.usda' -c")
        hda_parm.set.assert_called_once_with("/worker/assets/cables.usda")

    @pytest.mark.parametrize("category", ["Sop", "Lop"])
    @patch.dict("os.environ", {"HOUDINI_PATHMAP": "{'P:/assets': '/worker/assets'}"})
    @patch.object(
        hou, "hscript", new=Mock(side_effect=_fake_pathmap("P:/assets", "/worker/assets"))
    )
    def test_scene_load_remaps_sop_or_lop_hda_only_in_its_own_pass(
        self, category: str, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A SOP or LOP locked HDA that holds a locked SOP is path-mapped once, by the pass
        for its own context. Catches remapping it a second time as an HDA."""
        # GIVEN a SOP or LOP HDA with a covered File parm, holding a locked SOP
        hda_parm: Mock = self._make_parm("filepath1", "P:/assets/cables.usda")
        hda: Mock = self._make_hda_node(category, "/obj/asset1", [hda_parm])
        inner: Mock = self._make_sop_node([], inside_locked_hda=True, parent=hda)
        # AND the HDA itself in its own context's scene nodes
        if category == "Sop":
            self._scene_with_sop_nodes([hda, inner])
        else:
            self._scene_with_sop_nodes([inner], lop_nodes=[hda])

        # WHEN the scene loads
        self._load_scene()

        # THEN the HDA's parm is set once, and not by an HDA pass
        hda_parm.set.assert_called_once_with("/worker/assets/cables.usda")
        assert "Remapped HDA parm" not in capfd.readouterr().out
