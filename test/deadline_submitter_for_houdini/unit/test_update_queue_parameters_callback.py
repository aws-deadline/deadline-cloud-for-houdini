# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import pytest

from . import shared_callback_tests

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    update_queue_parameters_callback,
)


@pytest.mark.parametrize("empty_farm_id", [None, ""])
def test_no_selected_farm_id(empty_farm_id, mock_api):
    shared_callback_tests.no_selected_farm_id_test(
        empty_farm_id, mock_api, update_queue_parameters_callback
    )


@pytest.mark.parametrize("empty_queue_id", [None, ""])
def test_no_selected_queue_id(empty_queue_id, mock_api):
    shared_callback_tests.no_selected_queue_id_test(
        empty_queue_id, mock_api, update_queue_parameters_callback
    )


def test_farm_and_queue_id_selected(mock_api):
    shared_callback_tests.farm_and_queue_id_selected_test(
        mock_api, update_queue_parameters_callback
    )
