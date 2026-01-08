# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from unittest import mock

import pytest

from .shared_callback_tests import GET_SETTING_FUNCTION_FULL_PATH, TEST_DEADLINE_NODE_NAME
from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    submit_callback,
    _follow_fetch_nodes,
)


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
