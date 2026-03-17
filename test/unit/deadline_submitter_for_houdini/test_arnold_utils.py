# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Unit tests for arnold_utils module."""

from unittest.mock import MagicMock, Mock
import pytest

from .mock_hou import hou_module as hou
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.arnold_utils import (
    ArnoldExportSettings,
    is_arnold_rop,
    find_arnold_rops_in_network,
    configure_arnold_rop_for_export,
    get_arnold_ass_output_directories,
    _set_parm_safe,
)


# --- Helpers ---


def _make_mock_node(type_name="arnold", parms=None):
    """Create a mock hou.Node with the given type name and parameters."""
    node = MagicMock()
    node.type().name.return_value = type_name
    node.type().nameWithCategory.return_value = f"Driver/{type_name}"
    node.path.return_value = f"/out/{type_name}1"

    parm_dict = parms or {}

    def parm_side_effect(name):
        return parm_dict.get(name)

    node.parm = MagicMock(side_effect=parm_side_effect)
    return node


def _make_mock_parm(value="", raises_type_error=False):
    """Create a mock hou.Parm."""
    parm = MagicMock()
    parm.eval.return_value = value

    if raises_type_error:
        parm.set = MagicMock(side_effect=[TypeError("wrong type"), None])
    else:
        parm.set = MagicMock()

    return parm


# --- is_arnold_rop ---


class TestIsArnoldRop:
    def test_arnold_node(self):
        node = _make_mock_node("arnold")
        assert is_arnold_rop(node) is True

    def test_non_arnold_node(self):
        node = _make_mock_node("ifd")
        assert is_arnold_rop(node) is False

    def test_karma_node(self):
        node = _make_mock_node("karma")
        assert is_arnold_rop(node) is False

    def test_exception_returns_false(self):
        node = MagicMock()
        node.type.side_effect = Exception("broken")
        assert is_arnold_rop(node) is False


# --- find_arnold_rops_in_network ---


class TestFindArnoldRopsInNetwork:
    def test_finds_arnold_rops(self):
        arnold1 = _make_mock_node("arnold")
        arnold2 = _make_mock_node("arnold")
        mantra = _make_mock_node("ifd")

        root = MagicMock()
        root.inputAncestors.return_value = [arnold1, mantra, arnold2]

        result = find_arnold_rops_in_network(root)
        assert len(result) == 2
        assert arnold1 in result
        assert arnold2 in result

    def test_no_arnold_rops(self):
        root = MagicMock()
        root.inputAncestors.return_value = [_make_mock_node("ifd"), _make_mock_node("karma")]

        assert find_arnold_rops_in_network(root) == []

    def test_empty_network(self):
        root = MagicMock()
        root.inputAncestors.return_value = []

        assert find_arnold_rops_in_network(root) == []


# --- _set_parm_safe ---


class TestSetParmSafe:
    def test_set_int(self):
        parm = _make_mock_parm()
        _set_parm_safe(parm, 2)
        parm.set.assert_called_once_with(2)

    def test_set_string(self):
        parm = _make_mock_parm()
        _set_parm_safe(parm, "/renders/out.ass")
        parm.set.assert_called_once_with("/renders/out.ass")

    def test_set_bool_true(self):
        parm = _make_mock_parm()
        _set_parm_safe(parm, True)
        parm.set.assert_called_once_with(1)

    def test_set_bool_false(self):
        parm = _make_mock_parm()
        _set_parm_safe(parm, False)
        parm.set.assert_called_once_with(0)

    def test_type_error_fallback_int_to_string(self):
        parm = _make_mock_parm(raises_type_error=True)
        _set_parm_safe(parm, 2)
        assert parm.set.call_count == 2
        parm.set.assert_called_with("2")

    def test_type_error_non_int_reraises(self):
        parm = MagicMock()
        parm.set = MagicMock(side_effect=TypeError("wrong type"))
        with pytest.raises(TypeError):
            _set_parm_safe(parm, [1, 2, 3])


# --- configure_arnold_rop_for_export ---


class TestConfigureArnoldRopForExport:
    def test_default_settings_sets_all_params(self):
        parms = {
            "ar_ass_export_enable": _make_mock_parm(),
            "ar_picture": _make_mock_parm("ip"),
            "ar_log_verbosity": _make_mock_parm(),
            "ar_log_console_enable": _make_mock_parm(),
            "ar_abort_on_license_fail": _make_mock_parm(),
        }
        node = _make_mock_node("arnold", parms)

        configure_arnold_rop_for_export(node, ArnoldExportSettings())

        for p in parms.values():
            p.set.assert_called()

    def test_disable_ass_export_skips_param(self):
        parms = {
            "ar_picture": _make_mock_parm("ip"),
            "ar_log_verbosity": _make_mock_parm(),
            "ar_log_console_enable": _make_mock_parm(),
            "ar_abort_on_license_fail": _make_mock_parm(),
        }
        node = _make_mock_node("arnold", parms)

        configure_arnold_rop_for_export(node, ArnoldExportSettings(enable_ass_export=False))
        # ar_ass_export_enable not in parms, so it was never requested
        assert node.parm("ar_ass_export_enable") is None

    def test_keep_image_render(self):
        parms = {
            "ar_ass_export_enable": _make_mock_parm(),
            "ar_log_verbosity": _make_mock_parm(),
            "ar_log_console_enable": _make_mock_parm(),
            "ar_abort_on_license_fail": _make_mock_parm(),
        }
        node = _make_mock_node("arnold", parms)

        configure_arnold_rop_for_export(node, ArnoldExportSettings(disable_image_render=False))
        assert node.parm("ar_picture") is None

    def test_custom_output_path(self):
        parms = {
            "ar_ass_export_enable": _make_mock_parm(),
            "ar_picture": _make_mock_parm(),
            "ar_log_verbosity": _make_mock_parm(),
            "ar_log_console_enable": _make_mock_parm(),
            "ar_abort_on_license_fail": _make_mock_parm(),
            "ar_ass_file": _make_mock_parm(),
        }
        node = _make_mock_node("arnold", parms)

        configure_arnold_rop_for_export(
            node, ArnoldExportSettings(ass_output_path="/renders/scene.$F4.ass")
        )
        parms["ar_ass_file"].set.assert_called_with("/renders/scene.$F4.ass")

    def test_missing_required_param_raises(self):
        parms = {
            "ar_ass_export_enable": _make_mock_parm(),
            "ar_picture": _make_mock_parm(),
            "ar_log_verbosity": _make_mock_parm(),
            "ar_log_console_enable": _make_mock_parm(),
            "ar_abort_on_license_fail": _make_mock_parm(),
        }
        node = _make_mock_node("arnold", parms)

        with pytest.raises(RuntimeError, match="Required parameter"):
            configure_arnold_rop_for_export(
                node, ArnoldExportSettings(ass_output_path="/renders/out.ass")
            )

    def test_missing_optional_params_skipped(self):
        parms = {"ar_picture": _make_mock_parm("ip")}
        node = _make_mock_node("arnold", parms)

        # Should not raise
        configure_arnold_rop_for_export(node, ArnoldExportSettings())
        parms["ar_picture"].set.assert_called()


# --- get_arnold_ass_output_directories ---


class TestGetArnoldAssOutputDirectories:
    def test_ass_file_path(self):
        parms = {
            "ar_ass_file": _make_mock_parm("/renders/scene.0001.ass"),
            "ar_picture": _make_mock_parm("/renders/image.0001.exr"),
        }
        node = _make_mock_node("arnold", parms)
        assert get_arnold_ass_output_directories(node) == {"/renders"}

    def test_falls_back_to_ar_picture(self):
        parms = {
            "ar_ass_file": _make_mock_parm(""),
            "ar_picture": _make_mock_parm("/output/beauty.0001.exr"),
        }
        node = _make_mock_node("arnold", parms)
        assert get_arnold_ass_output_directories(node) == {"/output"}

    def test_skips_ip_picture(self):
        parms = {
            "ar_ass_file": _make_mock_parm(""),
            "ar_picture": _make_mock_parm("ip"),
        }
        node = _make_mock_node("arnold", parms)
        assert get_arnold_ass_output_directories(node) == set()

    def test_handles_frame_tokens(self):
        parms = {"ar_ass_file": _make_mock_parm("/renders/scene.$F4.ass")}
        node = _make_mock_node("arnold", parms)
        assert get_arnold_ass_output_directories(node) == {"/renders"}

    def test_no_parms(self):
        node = _make_mock_node("arnold", {})
        assert get_arnold_ass_output_directories(node) == set()

    def test_ass_file_preferred_over_picture(self):
        parms = {
            "ar_ass_file": _make_mock_parm("/ass_output/scene.0001.ass"),
            "ar_picture": _make_mock_parm("/image_output/beauty.0001.exr"),
        }
        node = _make_mock_node("arnold", parms)
        assert get_arnold_ass_output_directories(node) == {"/ass_output"}
