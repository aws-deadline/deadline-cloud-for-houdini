# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from unittest import mock

import pytest

from . import shared_callback_tests

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import login_callback


@pytest.fixture(scope="function", autouse=True)
def mock_login_dialog():
    with mock.patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.DeadlineLoginDialog"
    ) as login_dialog:
        yield login_dialog


@pytest.mark.parametrize("empty_farm_id", [None, ""])
def test_no_selected_farm_id(empty_farm_id, mock_login_dialog, mock_api):
    shared_callback_tests.no_selected_farm_id_test(empty_farm_id, mock_api, login_callback)
    mock_login_dialog.login.assert_called_once()


@pytest.mark.parametrize("empty_queue_id", [None, ""])
def test_no_selected_queue_id(empty_queue_id, mock_login_dialog, mock_api):
    shared_callback_tests.no_selected_queue_id_test(empty_queue_id, mock_api, login_callback)
    mock_login_dialog.login.assert_called_once()


def test_farm_and_queue_id_selected(mock_login_dialog, mock_api):
    shared_callback_tests.farm_and_queue_id_selected_test(mock_api, login_callback)
    mock_login_dialog.login.assert_called_once()
