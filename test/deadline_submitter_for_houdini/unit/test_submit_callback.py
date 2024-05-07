# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    submit_callback,
)


def test_error_message_for_missing_inputs():
    """Tests that if there is not ancestors to the Deadline Cloud node,
    a message is displayed and the function returns immediately"""
    adc_node = hou.node("/Driver/deadline_cloud")
    adc_node.inputAncestors.return_value = []

    submit_callback({"node": adc_node})

    hou.ui.displayMessage.assert_called_once_with(
        "The AWS Deadline Cloud render node (ROP) must have an input ROP specified to submit a job",
        title="Missing Input Render Node",
        severity=hou.severityType.Warning,
    )

    adc_node.parm.assert_not_called()
