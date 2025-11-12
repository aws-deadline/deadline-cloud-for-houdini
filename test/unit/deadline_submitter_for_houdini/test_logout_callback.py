# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from unittest import mock

from .shared_callback_tests import TEST_DEADLINE_NODE_NAME
from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _NONE_SELECTED_TEXT,
    logout_callback,
)


def test_logout_callback(mock_api):
    adc_node = hou.node(TEST_DEADLINE_NODE_NAME)

    logout_callback({"node": adc_node})

    mock_api.logout.assert_called_once_with()
    adc_node.parm.assert_has_calls(
        [
            mock.call("farm"),
            mock.call().set(_NONE_SELECTED_TEXT),
            mock.call("queue"),
            mock.call().set(_NONE_SELECTED_TEXT),
        ]
    )
    assert adc_node.parm.call_count == 2
    assert adc_node.parm().set.call_count == 2
