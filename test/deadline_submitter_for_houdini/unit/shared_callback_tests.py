# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from unittest import mock

from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _NONE_SELECTED_TEXT,
    _REFRESHING_TEXT,
)


GET_SETTING_FUNCTION_FULL_PATH = (
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.get_setting"
)
TEST_DEADLINE_NODE_NAME = "/Driver/deadline_cloud"


def no_selected_farm_id_test(empty_farm_id, mock_api, function_under_test):
    adc_node = hou.node()

    with mock.patch(
        GET_SETTING_FUNCTION_FULL_PATH,
        side_effect=lambda setting_name: (
            empty_farm_id if setting_name == "defaults.farm_id" else "test-setting"
        ),
    ):
        function_under_test({"node": adc_node})

    mock_api.get_boto3_client.assert_not_called()
    adc_node.parm.assert_has_calls(
        [
            mock.call("farm"),
            mock.call().set(_REFRESHING_TEXT),
            mock.call("queue"),
            mock.call().set(_REFRESHING_TEXT),
            mock.call("farm"),
            mock.call().set(_NONE_SELECTED_TEXT),
            mock.call("queue"),
            mock.call().set(_NONE_SELECTED_TEXT),
        ]
    )
    assert adc_node.parm.call_count == 4
    assert adc_node.parm().set.call_count == 4


def no_selected_queue_id_test(empty_queue_id, mock_api, function_under_test):
    adc_node = hou.node(TEST_DEADLINE_NODE_NAME)
    mock_api.get_boto3_client("deadline").get_farm.return_value = {"displayName": "test-farm"}

    with mock.patch(
        GET_SETTING_FUNCTION_FULL_PATH,
        side_effect=lambda setting_name: (
            empty_queue_id if setting_name == "defaults.queue_id" else "test-setting"
        ),
    ):
        function_under_test({"node": adc_node})

    deadline_api = mock_api.get_boto3_client("deadline")
    deadline_api.get_farm.assert_called_once_with(farmId="test-setting")
    adc_node.parm.assert_has_calls(
        [
            mock.call("farm"),
            mock.call().set(_REFRESHING_TEXT),
            mock.call("queue"),
            mock.call().set(_REFRESHING_TEXT),
            mock.call("farm"),
            mock.call().set("test-farm"),
            mock.call("queue"),
            mock.call().set(_NONE_SELECTED_TEXT),
        ]
    )
    assert adc_node.parm.call_count == 4
    assert adc_node.parm().set.call_count == 4


def farm_and_queue_id_selected_test(mock_api, function_under_test):
    adc_node = hou.node(TEST_DEADLINE_NODE_NAME)
    mock_api.get_boto3_client("deadline").get_farm.return_value = {"displayName": "test-farm"}
    mock_api.get_boto3_client("deadline").get_queue.return_value = {"displayName": "test-queue"}

    def sample_settings(setting_name):
        if setting_name == "defaults.farm_id":
            return "farm-12345"
        if setting_name == "defaults.queue_id":
            return "queue-12345"
        return None

    with (
        mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.update_queue_parameters"
        ) as mock_update_queue_parameters,
        mock.patch(
            GET_SETTING_FUNCTION_FULL_PATH,
            side_effect=sample_settings,
        ),
    ):
        function_under_test({"node": adc_node})

    deadline_api = mock_api.get_boto3_client("deadline")
    deadline_api.get_farm.assert_called_once_with(farmId="farm-12345")
    deadline_api.get_queue.assert_called_once_with(farmId="farm-12345", queueId="queue-12345")
    adc_node.parm.assert_has_calls(
        [
            mock.call("farm"),
            mock.call().set(_REFRESHING_TEXT),
            mock.call("queue"),
            mock.call().set(_REFRESHING_TEXT),
            mock.call("farm"),
            mock.call().set("test-farm"),
            mock.call("queue"),
            mock.call().set("test-queue"),
        ]
    )
    assert adc_node.parm.call_count == 4
    assert adc_node.parm().set.call_count == 4
    mock_update_queue_parameters.assert_called_once_with("farm-12345", "queue-12345", adc_node)
