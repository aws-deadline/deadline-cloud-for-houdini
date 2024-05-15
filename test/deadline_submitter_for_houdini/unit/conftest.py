# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from unittest import mock

import pytest

from .mock_hou import hou_module as hou


@pytest.fixture(scope="function", autouse=True)
def reset_hou_mocks():
    for each in [d for d in dir(hou) if not d.startswith("__")]:
        attr = getattr(hou, each)
        if hasattr(attr, "reset_mock"):
            attr.reset_mock()
        else:
            del attr


@pytest.fixture(scope="function")
def mock_api():
    """Mocks the AWS Deadline Cloud API"""
    with mock.patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.api"
    ) as api_mock:
        yield api_mock
