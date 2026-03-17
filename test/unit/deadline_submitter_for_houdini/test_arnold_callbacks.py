# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Unit tests for Arnold-related callbacks and helpers in submitter.py."""

from unittest import mock
from unittest.mock import MagicMock

import pytest

from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    export_ass_callback,
    _auto_configure_arnold_rops,
)

_ARNOLD_UTILS = "deadline.houdini_submitter.python.deadline_cloud_for_houdini.arnold_utils"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_arnold_rop(name="arnold1"):
    node = MagicMock()
    node.type().name.return_value = "arnold"
    node.path.return_value = f"/out/{name}"
    node.name.return_value = name
    return node


def _make_deadline_node(trange=0, f1=1, f2=10, f3=1, auto_configure=True):
    """Create a mock Deadline Cloud node."""
    node = MagicMock()

    parm_map = {
        "trange": MagicMock(**{"eval.return_value": trange}),
        "f1": MagicMock(**{"eval.return_value": f1}),
        "f2": MagicMock(**{"eval.return_value": f2}),
        "f3": MagicMock(**{"eval.return_value": f3}),
        "arnold_auto_configure": MagicMock(**{"eval.return_value": auto_configure}),
    }

    def parm_side_effect(name):
        return parm_map.get(name)

    node.parm = MagicMock(side_effect=parm_side_effect)
    return node


# ---------------------------------------------------------------------------
# _auto_configure_arnold_rops
# ---------------------------------------------------------------------------


class TestAutoConfigureArnoldRops:
    @mock.patch(f"{_ARNOLD_UTILS}.configure_arnold_rop_for_export")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_no_arnold_rops_returns_empty(self, mock_find, mock_configure):
        mock_find.return_value = []
        node = _make_deadline_node()

        result = _auto_configure_arnold_rops(node)

        assert result == []
        mock_configure.assert_not_called()
        hou.hipFile.save.assert_not_called()

    @mock.patch(f"{_ARNOLD_UTILS}.configure_arnold_rop_for_export")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_configures_when_auto_configure_enabled(self, mock_find, mock_configure):
        rop1 = _make_arnold_rop("a1")
        rop2 = _make_arnold_rop("a2")
        mock_find.return_value = [rop1, rop2]
        node = _make_deadline_node(auto_configure=True)

        result = _auto_configure_arnold_rops(node)

        assert len(result) == 2
        assert mock_configure.call_count == 2
        hou.hipFile.save.assert_called_once()

    @mock.patch(f"{_ARNOLD_UTILS}.configure_arnold_rop_for_export")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_skips_configure_when_disabled(self, mock_find, mock_configure):
        rop1 = _make_arnold_rop()
        mock_find.return_value = [rop1]
        node = _make_deadline_node(auto_configure=False)

        result = _auto_configure_arnold_rops(node)

        assert len(result) == 1
        mock_configure.assert_not_called()
        hou.hipFile.save.assert_not_called()

    @mock.patch(f"{_ARNOLD_UTILS}.configure_arnold_rop_for_export")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_defaults_to_configure_when_parm_missing(self, mock_find, mock_configure):
        rop1 = _make_arnold_rop()
        mock_find.return_value = [rop1]
        node = MagicMock()
        node.parm = MagicMock(return_value=None)  # parm not found

        result = _auto_configure_arnold_rops(node)

        assert len(result) == 1
        mock_configure.assert_called_once()
        hou.hipFile.save.assert_called_once()


# ---------------------------------------------------------------------------
# export_ass_callback
# ---------------------------------------------------------------------------


class TestExportAssCallback:
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_no_arnold_rops_shows_warning(self, mock_find):
        mock_find.return_value = []
        node = _make_deadline_node()

        export_ass_callback({"node": node})

        hou.ui.displayMessage.assert_called_once()
        call_kwargs = hou.ui.displayMessage.call_args
        assert "No Arnold ROPs found" in call_kwargs[0][0]
        assert call_kwargs[1]["severity"] == hou.severityType.Warning

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_single_rop_success(self, mock_find, mock_export):
        rop = _make_arnold_rop()
        mock_find.return_value = [rop]
        mock_export.return_value = ["/renders/scene.0001.ass", "/renders/scene.0002.ass"]
        node = _make_deadline_node(trange=0)

        export_ass_callback({"node": node})

        mock_export.assert_called_once()
        hou.hipFile.save.assert_called_once()
        call_args = hou.ui.displayMessage.call_args
        assert "2 .ass file(s) successfully" in call_args[0][0]

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_frame_range_passed_when_trange_nonzero(self, mock_find, mock_export):
        rop = _make_arnold_rop()
        mock_find.return_value = [rop]
        mock_export.return_value = ["/renders/scene.0001.ass"]
        node = _make_deadline_node(trange=1, f1=5, f2=20, f3=2)

        export_ass_callback({"node": node})

        call_args_positional = mock_export.call_args[0]
        assert call_args_positional[2] == (5, 20, 2)

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_current_frame_when_trange_zero(self, mock_find, mock_export):
        rop = _make_arnold_rop()
        mock_find.return_value = [rop]
        mock_export.return_value = ["/renders/scene.0001.ass"]
        node = _make_deadline_node(trange=0)

        export_ass_callback({"node": node})

        call_args_positional = mock_export.call_args[0]
        assert call_args_positional[2] is None

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_partial_failure_shows_warning(self, mock_find, mock_export):
        rop1 = _make_arnold_rop("a1")
        rop2 = _make_arnold_rop("a2")
        mock_find.return_value = [rop1, rop2]
        mock_export.side_effect = [
            ["/renders/scene.0001.ass"],
            RuntimeError("License not found"),
        ]
        node = _make_deadline_node(trange=0)

        export_ass_callback({"node": node})

        call_args = hou.ui.displayMessage.call_args
        assert "1 error(s)" in call_args[0][0]
        assert call_args[1]["severity"] == hou.severityType.Warning

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_no_files_on_disk_shows_warning(self, mock_find, mock_export):
        rop = _make_arnold_rop()
        mock_find.return_value = [rop]
        mock_export.return_value = []
        node = _make_deadline_node(trange=0)

        export_ass_callback({"node": node})

        call_args = hou.ui.displayMessage.call_args
        assert "no .ass files were found" in call_args[0][0]
        assert call_args[1]["severity"] == hou.severityType.Warning
