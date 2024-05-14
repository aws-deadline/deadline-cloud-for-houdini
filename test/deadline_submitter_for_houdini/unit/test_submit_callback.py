# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from unittest import mock

import pytest

from .shared_callback_tests import GET_SETTING_FUNCTION_FULL_PATH, TEST_DEADLINE_NODE_NAME
from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    submit_callback,
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


@pytest.fixture(scope="function", autouse=True)
def patch_os_dirname():
    with mock.patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.os", mock.Mock()
    ):
        yield


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
