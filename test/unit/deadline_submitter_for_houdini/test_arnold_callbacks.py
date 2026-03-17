# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Unit tests for Arnold-related callbacks and helpers in submitter.py."""

from unittest import mock
from unittest.mock import MagicMock

from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    export_ass_callback,
    _auto_configure_arnold_rops,
)

_ARNOLD_UTILS = "deadline.houdini_submitter.python.deadline_cloud_for_houdini.arnold_utils"


def _make_arnold_rop(name="arnold1"):
    node = MagicMock()
    node.type().name.return_value = "arnold"
    node.path.return_value = f"/out/{name}"
    node.name.return_value = name
    return node


def _make_deadline_node(trange=0, f1=1, f2=10, f3=1, auto_configure=True):
    node = MagicMock()
    parm_map = {
        "trange": MagicMock(**{"eval.return_value": trange}),
        "f1": MagicMock(**{"eval.return_value": f1}),
        "f2": MagicMock(**{"eval.return_value": f2}),
        "f3": MagicMock(**{"eval.return_value": f3}),
        "arnold_auto_configure": MagicMock(**{"eval.return_value": auto_configure}),
    }
    node.parm = MagicMock(side_effect=lambda n: parm_map.get(n))
    return node


class TestAutoConfigureArnoldRops:
    @mock.patch(f"{_ARNOLD_UTILS}.configure_arnold_rop_for_export")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_no_arnold_rops(self, mock_find, mock_configure):
        mock_find.return_value = []
        result = _auto_configure_arnold_rops(_make_deadline_node())
        assert result == []
        mock_configure.assert_not_called()

    @mock.patch(f"{_ARNOLD_UTILS}.configure_arnold_rop_for_export")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_configures_when_enabled(self, mock_find, mock_configure):
        mock_find.return_value = [_make_arnold_rop("a1"), _make_arnold_rop("a2")]
        result = _auto_configure_arnold_rops(_make_deadline_node(auto_configure=True))
        assert len(result) == 2
        assert mock_configure.call_count == 2
        hou.hipFile.save.assert_called_once()

    @mock.patch(f"{_ARNOLD_UTILS}.configure_arnold_rop_for_export")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_skips_when_disabled(self, mock_find, mock_configure):
        mock_find.return_value = [_make_arnold_rop()]
        result = _auto_configure_arnold_rops(_make_deadline_node(auto_configure=False))
        assert len(result) == 1
        mock_configure.assert_not_called()

    @mock.patch(f"{_ARNOLD_UTILS}.configure_arnold_rop_for_export")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_defaults_when_parm_missing(self, mock_find, mock_configure):
        mock_find.return_value = [_make_arnold_rop()]
        node = MagicMock()
        node.parm = MagicMock(return_value=None)
        result = _auto_configure_arnold_rops(node)
        assert len(result) == 1
        mock_configure.assert_called_once()


class TestExportAssCallback:
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_no_rops_shows_warning(self, mock_find):
        mock_find.return_value = []
        export_ass_callback({"node": _make_deadline_node()})
        hou.ui.displayMessage.assert_called_once()
        assert "No Arnold ROPs found" in hou.ui.displayMessage.call_args[0][0]

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_success(self, mock_find, mock_export):
        mock_find.return_value = [_make_arnold_rop()]
        mock_export.return_value = ["/renders/scene.0001.ass"]
        export_ass_callback({"node": _make_deadline_node(trange=0)})
        mock_export.assert_called_once()
        assert "1 .ass file(s) successfully" in hou.ui.displayMessage.call_args[0][0]

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_frame_range_when_trange_nonzero(self, mock_find, mock_export):
        mock_find.return_value = [_make_arnold_rop()]
        mock_export.return_value = ["/renders/scene.0001.ass"]
        export_ass_callback({"node": _make_deadline_node(trange=1, f1=5, f2=20, f3=2)})
        assert mock_export.call_args[0][2] == (5, 20, 2)

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_partial_failure(self, mock_find, mock_export):
        mock_find.return_value = [_make_arnold_rop("a1"), _make_arnold_rop("a2")]
        mock_export.side_effect = [["/renders/scene.0001.ass"], RuntimeError("License fail")]
        export_ass_callback({"node": _make_deadline_node(trange=0)})
        assert "1 error(s)" in hou.ui.displayMessage.call_args[0][0]

    @mock.patch(f"{_ARNOLD_UTILS}.export_arnold_ass_locally")
    @mock.patch(f"{_ARNOLD_UTILS}.find_arnold_rops_in_network")
    def test_no_files_warning(self, mock_find, mock_export):
        mock_find.return_value = [_make_arnold_rop()]
        mock_export.return_value = []
        export_ass_callback({"node": _make_deadline_node(trange=0)})
        assert "no .ass files were found" in hou.ui.displayMessage.call_args[0][0]
