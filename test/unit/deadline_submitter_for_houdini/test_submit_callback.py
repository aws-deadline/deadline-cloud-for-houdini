# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from unittest import mock

import pytest

from .shared_callback_tests import GET_SETTING_FUNCTION_FULL_PATH, TEST_DEADLINE_NODE_NAME
from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    submit_callback,
    _follow_fetch_nodes,
)


@pytest.fixture
def rop_lifetime_node(monkeypatch):
    monkeypatch.setattr(hou, "nodeEventType", mock.Mock(), raising=False)
    node = mock.Mock()
    node.sessionId.return_value = 42
    return node


def test_rop_tool_is_modeless_and_retained_until_destroyed(rop_lifetime_node):
    """A floating ROP tool must survive callback return and release on close."""
    from importlib import import_module

    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter

    qt = getattr(import_module("qtpy.QtCore"), "Qt")

    dialog = mock.Mock()
    submitter._show_rop_submitter(dialog, rop_lifetime_node)
    try:
        dialog.show.assert_called_once_with()
        dialog.exec_.assert_not_called()
        dialog.setAttribute.assert_called_once_with(qt.WA_DeleteOnClose, True)
        assert submitter._ROP_SUBMITTER_DIALOGS[42] is dialog
    finally:
        dialog.destroyed.connect.call_args.args[0]()
    assert 42 not in submitter._ROP_SUBMITTER_DIALOGS
    rop_lifetime_node.removeEventCallback.assert_called_once_with(
        (hou.nodeEventType.BeingDeleted,),
        rop_lifetime_node.addEventCallback.call_args.args[1],
    )


def test_reopening_rop_reuses_dialog_even_after_node_rename(rop_lifetime_node):
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter

    dialog = mock.Mock()
    submitter._show_rop_submitter(dialog, rop_lifetime_node)
    try:
        rop_lifetime_node.path.return_value = "/out/renamed_deadline"
        submit_callback({"node": rop_lifetime_node})
        assert submitter._ROP_SUBMITTER_DIALOGS[42] is dialog
        dialog.raise_.assert_called_once_with()
        dialog.activateWindow.assert_called_once_with()
        rop_lifetime_node.inputAncestors.assert_not_called()
    finally:
        dialog.destroyed.connect.call_args.args[0]()


def test_deleted_rop_closes_dialog_without_accessing_deleted_node(rop_lifetime_node):
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter

    dialog = mock.Mock()
    submitter._show_rop_submitter(dialog, rop_lifetime_node)
    deleted = rop_lifetime_node.addEventCallback.call_args.args[1]
    deleted(node=rop_lifetime_node, event_type=hou.nodeEventType.BeingDeleted)
    assert 42 not in submitter._ROP_SUBMITTER_DIALOGS
    dialog.close.assert_called_once_with()
    dialog.destroyed.connect.call_args.args[0]()
    rop_lifetime_node.removeEventCallback.assert_not_called()


@pytest.fixture(scope="function")
def default_adc_node():
    """
    Creates a default AWS Deadline Cloud render node with no input or output files
    and one input render node
    """
    adc_node = hou.node(TEST_DEADLINE_NODE_NAME)
    adc_node.parm("auto_parse_hip").eval.return_value = True
    for parm in ["input_filenames", "input_directories", "output_directories"]:
        adc_node.parm(parm).multiParmInstances.return_value = []
        adc_node.parm(parm).multiParmInstancesCount.return_value = 0
    mock_render_node = hou.node("/Driver/render_node")
    adc_node.inputAncestors.return_value = [mock_render_node]
    hou.fileReferences.return_value = []
    return adc_node


def test_error_message_for_missing_inputs():
    """Tests that if there is not ancestors to the Deadline Cloud node,
    a message is displayed and the function returns immediately"""
    adc_node = hou.node(TEST_DEADLINE_NODE_NAME)
    adc_node.inputAncestors.return_value = []

    submit_callback({"node": adc_node})

    hou.ui.displayMessage.assert_called_once_with(
        "The AWS Deadline Cloud render node (ROP) must have an input ROP specified to submit a job",
        title="Missing Input Render Node",
        severity=hou.severityType.Warning,
    )

    adc_node.parm.assert_not_called()


@pytest.mark.parametrize("empty_farm_id", [None, ""])
def test_error_message_for_missing_farm_id(empty_farm_id, default_adc_node, mock_api):
    with mock.patch(
        GET_SETTING_FUNCTION_FULL_PATH,
        side_effect=lambda setting_name: (
            empty_farm_id if setting_name == "defaults.farm_id" else "test-setting"
        ),
    ):
        submit_callback({"node": default_adc_node})

    hou.ui.displayMessage.assert_called_once_with(
        "Please configure the farm ID in the AWS Deadline Cloud render node (ROP) settings",
        title="Farm ID Required",
        severity=hou.severityType.Warning,
    )
    mock_api.get_boto3_client.assert_not_called()


@pytest.mark.parametrize("empty_queue_id", [None, ""])
def test_error_message_for_missing_queue_id(empty_queue_id, default_adc_node, mock_api):
    with mock.patch(
        GET_SETTING_FUNCTION_FULL_PATH,
        side_effect=lambda setting_name: (
            empty_queue_id if setting_name == "defaults.queue_id" else "test-setting"
        ),
    ):
        submit_callback({"node": default_adc_node})

    hou.ui.displayMessage.assert_called_once_with(
        "Please configure the queue ID in the AWS Deadline Cloud render node (ROP) settings",
        title="Queue ID Required",
        severity=hou.severityType.Warning,
    )
    mock_api.get_boto3_client.assert_not_called()


class TestUnlockingROPs:
    """Tests for ROP unlocking functionality including fetch node following"""

    def test_chained_fetch_nodes_to_target(self):
        """Test: fetch1 -> fetch2 -> fetch3 -> target"""
        # Create fetch chain: fetch1 -> fetch2 -> fetch3 -> target
        fetch1 = mock.MagicMock()
        fetch1.type.return_value.nameWithCategory.return_value = "Driver/fetch"
        fetch1.path.return_value = "/out/fetch1"
        fetch1.parm.return_value.eval.return_value = "fetch2"

        fetch2 = mock.MagicMock()
        fetch2.type.return_value.nameWithCategory.return_value = "Driver/fetch"
        fetch2.path.return_value = "/out/fetch2"
        fetch2.parm.return_value.eval.return_value = "fetch3"

        fetch3 = mock.MagicMock()
        fetch3.type.return_value.nameWithCategory.return_value = "Driver/fetch"
        fetch3.path.return_value = "/out/fetch3"
        fetch3.parm.return_value.eval.return_value = "geometry1"

        target_node = mock.MagicMock()
        target_node.type.return_value.nameWithCategory.return_value = "Driver/geometry"
        target_node.path.return_value = "/out/geometry1"

        # Set up chain
        fetch1.node.return_value = fetch2
        fetch2.node.return_value = fetch3
        fetch3.node.return_value = target_node

        # Test fetch following
        result = _follow_fetch_nodes(fetch1)
        assert result is target_node

    def test_infinite_recursion_prevention(self):
        """Test: fetch1 -> fetch2 -> fetch1 (circular reference)"""
        fetch1 = mock.MagicMock()
        fetch1.type.return_value.nameWithCategory.return_value = "Driver/fetch"
        fetch1.path.return_value = "/out/fetch1"
        fetch1.parm.return_value.eval.return_value = "fetch2"

        fetch2 = mock.MagicMock()
        fetch2.type.return_value.nameWithCategory.return_value = "Driver/fetch"
        fetch2.path.return_value = "/out/fetch2"
        fetch2.parm.return_value.eval.return_value = "fetch1"

        # Set up circular reference
        fetch1.node.return_value = fetch2
        fetch2.node.return_value = fetch1

        result = _follow_fetch_nodes(fetch1)
        assert result is fetch1

    def test_non_fetch_node_passthrough(self):
        """Test: geometry node (not a fetch) should return itself"""
        geometry_node = mock.MagicMock()
        geometry_node.type.return_value.nameWithCategory.return_value = "Driver/geometry"
        geometry_node.path.return_value = "/out/geometry1"

        result = _follow_fetch_nodes(geometry_node)
        assert result is geometry_node

    def test_fetch_node_with_invalid_source(self):
        """Test: fetch node with empty/invalid source should return itself"""
        fetch_node = mock.MagicMock()
        fetch_node.type.return_value.nameWithCategory.return_value = "Driver/fetch"
        fetch_node.path.return_value = "/out/fetch1"
        fetch_node.parm.return_value.eval.return_value = "nonexistent"
        fetch_node.node.return_value = None  # Source doesn't exist

        result = _follow_fetch_nodes(fetch_node)
        assert result is fetch_node
