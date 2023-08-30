# Copyright 2022 Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

from unittest import TestCase
from unittest.mock import Mock, patch  # noqa:F401

import pytest
from .mock_hou import hou_module as hou

from deadline_adaptor_for_houdini.HoudiniClient.houdini_handler import HoudiniHandler


@pytest.fixture(autouse=True)
def reset_hou_mocks():
    for each in [d for d in dir(hou) if not d.startswith("__")]:
        getattr(hou, each).reset_mock()


class TestHoudiniHandler:
    def test_init(self) -> None:
        """ """
        # WHEN
        handler = HoudiniHandler()

        assert list(handler.action_dict.keys()) == [
            "scene_file",
            "render_node",
            "frame",
            "ignore_input_nodes",
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
        handler.start_render({})
        hou.node.render.assert_called_once_with(
            verbose=True, frame_range=(1, 1, 1), ignore_inputs=True, method=None
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

    def test_set_frame(self) -> None:
        handler = HoudiniHandler()
        assert "frame" not in handler.render_kwargs.keys()
        data = {"frame": 1}
        handler.set_frame(data)
        assert handler.render_kwargs["frame"] == 1

    def test_set_scene_file_not_found(self, capfd) -> None:
        handler = HoudiniHandler()
        data = {"scene_file": "/not/a/real/scene.hip"}
        with pytest.raises(FileNotFoundError):
            # test file not found
            handler.set_scene_file(data)

    def test_set_scene_file_loads(self, capfd) -> None:
        handler = HoudiniHandler()
        data = {"scene_file": "/not/a/real/scene.hip"}
        with patch(
            "deadline_adaptor_for_houdini.HoudiniClient.houdini_handler.os.path.isfile"
        ) as mock_isfile:
            mock_isfile.return_value = True
            # test file loads
            handler.set_scene_file(data)
            hou.hipFile.load.assert_called_once()

    def test_set_scene_file_raise_exception(self, capfd) -> None:
        handler = HoudiniHandler()
        data = {"scene_file": "/not/a/real/scene.hip"}
        with patch(
            "deadline_adaptor_for_houdini.HoudiniClient.houdini_handler.os.path.isfile"
        ) as mock_isfile:
            mock_isfile.return_value = True
            hou.LoadWarning = Exception  # type: ignore
            hou.hipFile.load.side_effect = Exception("test")
            # test exception fired
            handler.set_scene_file(data)
            out, _ = capfd.readouterr()
            assert out[-5:] == "test\n"

    """
    # test case for code coverage this tests
    # code that will be removed to the common node library
    def test_set_node_settings(self, capfd):
        handler = HoudiniHandler()

        side_effect = [
            "Driver/other",
            "Driver/ifd",
            "Driver/karma",
            "NotDriver",
        ]
        hou.node.type().nameWithCategory.side_effect = side_effect
        hou.node.parm.return_value = Mock(name="parm")

        handler.set_node_settings(hou.node)
        assert (
            not hou.logging.setRenderLogVerbosity.called
        ), "hou.logging.setRenderLogVerbosity was called and should not have been"
        assert not hou.node.parm.called, "hou.node.parm was called and should not have been"

        handler.set_node_settings(hou.node)
        TestCase().assertEqual(hou.node.parm.call_count, 2)
        hou.node.parm.reset_mock()
        assert (
            not hou.logging.setRenderLogVerbosity.called
        ), "hou.logging.setRenderLogVerbosity was called and should not have been"
        out, _ = capfd.readouterr()
        assert out[-49:] == "Enabled Alfred style progress\nSet verbosity to 3\n"

        handler.set_node_settings(hou.node)
        TestCase().assertEqual(hou.node.parm.call_count, 2)
        hou.node.parm.reset_mock()
        out, _ = capfd.readouterr()
        assert out[-49:] == "Enabled Alfred style progress\nSet verbosity to 3\n"
        hou.logging.setRenderLogVerbosity.assert_called_once_with(3)
        hou.logging.setRenderLogVerbosity.reset_mock()

        handler.set_node_settings(hou.node)
        assert (
            not hou.logging.setRenderLogVerbosity.called
        ), "hou.logging.setRenderLogVerbosity was called and should not have been"
        assert not hou.node.parm.called, "hou.node.parm was called and should not have been"
    """

    def test_do_not_set_node_settings(self, capfd):
        handler = HoudiniHandler()

        side_effect = ["Driver/ifd", "Driver/karma"]
        hou.node.type().nameWithCategory.side_effect = side_effect
        hou.node.parm.return_value = None

        handler.set_node_settings(hou.node)
        assert (
            not hou.logging.setRenderLogVerbosity.called
        ), "hou.logging.setRenderLogVerbosity was called and should not have been"
        out, _ = capfd.readouterr()
        assert out == ""
        hou.node.parm.reset_mock()
        handler.set_node_settings(hou.node)
        TestCase().assertEqual(hou.node.parm.call_count, 2)
        hou.node.parm.reset_mock()
        out, _ = capfd.readouterr()
        assert out == ""
        assert not hou.logging.setRenderLogVerbosity.called
