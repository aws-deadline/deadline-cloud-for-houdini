# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from unittest import mock

from botocore.exceptions import ClientError

from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _apply_farm_and_queue_settings,
    _NONE_SELECTED_TEXT,
)


class TestApplyFarmAndQueueSettings:
    def test_access_denied_falls_back_to_farm_id(self, mock_api):
        """When user lacks GetFarm permission, display farm ID instead of name."""
        farm_id = "farm-1234567890"
        node = hou.node()

        mock_api.get_boto3_client.return_value.get_farm.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access Denied"}},
            "GetFarm",
        )

        with mock.patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.get_setting",
            side_effect=lambda key: farm_id if key == "defaults.farm_id" else None,
        ):
            _apply_farm_and_queue_settings(node)

        # Check that farm was set to farm_id (not display name) and queue to none selected
        node.parm.assert_has_calls(
            [
                mock.call("farm"),
                mock.call().set(farm_id),
                mock.call("queue"),
                mock.call().set(_NONE_SELECTED_TEXT),
            ]
        )
