# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Unit tests for arnold_utils module."""

from unittest.mock import MagicMock
import pytest
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.arnold_utils import (
    ArnoldExportSettings,
    is_arnold_rop,
    find_arnold_rops_in_network,
    configure_arnold_rop_for_export,
    get_arnold_ass_output_directories,
    _set_parm_safe,
    _snapshot_parms,
    _restore_parms,
)


def _make_node(type_name="arnold", parms=None):
    node = MagicMock()
    node.type().name.return_value = type_name
    node.path.return_value = f"/out/{type_name}1"
    parm_dict = parms or {}
    node.parm = MagicMock(side_effect=lambda n: parm_dict.get(n))
    return node


def _make_parm(value="", raises_type_error=False):
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
        assert is_arnold_rop(_make_node("arnold")) is True

    def test_non_arnold_node(self):
        assert is_arnold_rop(_make_node("ifd")) is False

    def test_exception_returns_false(self):
        node = MagicMock()
        node.type.side_effect = Exception("broken")
        assert is_arnold_rop(node) is False


# --- find_arnold_rops_in_network ---


class TestFindArnoldRopsInNetwork:
    def test_finds_arnold_rops(self):
        a1 = _make_node("arnold")
        a2 = _make_node("arnold")
        root = MagicMock()
        root.inputAncestors.return_value = [a1, _make_node("ifd"), a2]
        result = find_arnold_rops_in_network(root)
        assert len(result) == 2

    def test_empty_network(self):
        root = MagicMock()
        root.inputAncestors.return_value = []
        assert find_arnold_rops_in_network(root) == []


# --- _set_parm_safe ---


class TestSetParmSafe:
    def test_set_int(self):
        parm = _make_parm()
        _set_parm_safe(parm, 2)
        parm.set.assert_called_once_with(2)

    def test_set_string(self):
        parm = _make_parm()
        _set_parm_safe(parm, "/renders/out.ass")
        parm.set.assert_called_once_with("/renders/out.ass")

    def test_set_bool_true(self):
        parm = _make_parm()
        _set_parm_safe(parm, True)
        parm.set.assert_called_once_with(1)

    def test_set_bool_false(self):
        parm = _make_parm()
        _set_parm_safe(parm, False)
        parm.set.assert_called_once_with(0)

    def test_type_error_fallback_int_to_string(self):
        parm = _make_parm(raises_type_error=True)
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
    def test_default_settings(self):
        parms = {
            "ar_ass_export_enable": _make_parm(),
            "ar_picture": _make_parm("ip"),
            "ar_log_verbosity": _make_parm(),
            "ar_log_console_enable": _make_parm(),
            "ar_abort_on_license_fail": _make_parm(),
        }
        node = _make_node("arnold", parms)
        configure_arnold_rop_for_export(node, ArnoldExportSettings())
        for p in parms.values():
            p.set.assert_called()

    def test_custom_output_path(self):
        parms = {
            "ar_ass_export_enable": _make_parm(),
            "ar_picture": _make_parm(),
            "ar_log_verbosity": _make_parm(),
            "ar_log_console_enable": _make_parm(),
            "ar_abort_on_license_fail": _make_parm(),
            "ar_ass_file": _make_parm(),
        }
        node = _make_node("arnold", parms)
        configure_arnold_rop_for_export(
            node, ArnoldExportSettings(ass_output_path="/renders/scene.$F4.ass")
        )
        parms["ar_ass_file"].set.assert_called_with("/renders/scene.$F4.ass")

    def test_missing_required_param_raises(self):
        parms = {
            "ar_ass_export_enable": _make_parm(),
            "ar_picture": _make_parm(),
            "ar_log_verbosity": _make_parm(),
            "ar_log_console_enable": _make_parm(),
            "ar_abort_on_license_fail": _make_parm(),
        }
        node = _make_node("arnold", parms)
        with pytest.raises(RuntimeError, match="Required parameter"):
            configure_arnold_rop_for_export(
                node, ArnoldExportSettings(ass_output_path="/renders/out.ass")
            )

    def test_missing_optional_params_skipped(self):
        parms = {"ar_picture": _make_parm("ip")}
        node = _make_node("arnold", parms)
        configure_arnold_rop_for_export(node, ArnoldExportSettings())
        parms["ar_picture"].set.assert_called()

    def test_disable_ass_export(self):
        parms = {
            "ar_picture": _make_parm("ip"),
            "ar_log_verbosity": _make_parm(),
            "ar_log_console_enable": _make_parm(),
            "ar_abort_on_license_fail": _make_parm(),
        }
        node = _make_node("arnold", parms)
        configure_arnold_rop_for_export(node, ArnoldExportSettings(enable_ass_export=False))
        assert node.parm("ar_ass_export_enable") is None

    def test_keep_image_render(self):
        parms = {
            "ar_ass_export_enable": _make_parm(),
            "ar_log_verbosity": _make_parm(),
            "ar_log_console_enable": _make_parm(),
            "ar_abort_on_license_fail": _make_parm(),
        }
        node = _make_node("arnold", parms)
        configure_arnold_rop_for_export(node, ArnoldExportSettings(disable_image_render=False))
        assert node.parm("ar_picture") is None


# --- _snapshot_parms / _restore_parms ---


class TestSnapshotRestore:
    def test_snapshot_captures_values(self):
        parm = MagicMock()
        parm.unexpandedString.return_value = "$HIP/render.ass"
        node = MagicMock()
        node.parm = MagicMock(side_effect=lambda n: parm if n == "ar_ass_file" else None)
        result = _snapshot_parms(node, ["ar_ass_file", "missing_parm"])
        assert result == {"ar_ass_file": "$HIP/render.ass"}

    def test_snapshot_falls_back_to_eval_as_string(self):
        parm = MagicMock()
        parm.unexpandedString.side_effect = Exception("no unexpanded")
        parm.evalAsString.return_value = "/var/scenes/render.ass"
        node = MagicMock()
        node.parm = MagicMock(return_value=parm)
        result = _snapshot_parms(node, ["ar_ass_file"])
        assert result == {"ar_ass_file": "/var/scenes/render.ass"}

    def test_restore_sets_values(self):
        parm = MagicMock()
        node = MagicMock()
        node.parm = MagicMock(return_value=parm)
        _restore_parms(node, {"ar_ass_file": "$HIP/render.ass"})
        parm.set.assert_called_once_with("$HIP/render.ass")

    def test_restore_handles_missing_parm(self):
        node = MagicMock()
        node.parm = MagicMock(return_value=None)
        _restore_parms(node, {"missing": "value"})  # should not raise


# --- get_arnold_ass_output_directories ---


class TestGetArnoldAssOutputDirectories:
    def test_ass_file_path(self):
        parms = {
            "ar_ass_file": _make_parm("/renders/scene.0001.ass"),
            "ar_picture": _make_parm("/renders/image.0001.exr"),
        }
        node = _make_node("arnold", parms)
        assert get_arnold_ass_output_directories(node) == {"/renders"}

    def test_falls_back_to_ar_picture(self):
        parms = {
            "ar_ass_file": _make_parm(""),
            "ar_picture": _make_parm("/output/beauty.0001.exr"),
        }
        node = _make_node("arnold", parms)
        assert get_arnold_ass_output_directories(node) == {"/output"}

    def test_skips_ip_picture(self):
        parms = {
            "ar_ass_file": _make_parm(""),
            "ar_picture": _make_parm("ip"),
        }
        node = _make_node("arnold", parms)
        assert get_arnold_ass_output_directories(node) == set()

    def test_no_parms(self):
        node = _make_node("arnold", {})
        assert get_arnold_ass_output_directories(node) == set()
