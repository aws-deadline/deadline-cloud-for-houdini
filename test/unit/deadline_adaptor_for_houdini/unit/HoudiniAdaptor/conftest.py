# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import pytest
from unittest.mock import patch, Mock, PropertyMock
from typing import Generator
from deadline.houdini_adaptor.HoudiniAdaptor import HoudiniAdaptor


@pytest.fixture()
def init_data() -> dict:
    """
    Pytest Fixture to return an init_data dictionary that passes validation

    Returns:
        dict: An init_data dictionary
    """
    return {
        "scene_file": "/path/to/scene/houdiniscene-19.5.hip",
        "version": "19.5.435",
        "render_node": "mantra1",
        "wedge_node": "",
        "wedgenum": "",
        "ignore_input_nodes": True,
    }


@pytest.fixture()
def run_data() -> dict:
    """
    Pytest Fixture to return a run_data dictionary that passes validation

    Returns:
        dict: A run_data dictionary
    """
    return {"frame_range": {"start": 1, "end": 5, "step": 2}}


@pytest.fixture(autouse=True)
def mock_config() -> Generator[Mock, None, None]:
    config_mock = Mock()
    config_mock.get_executable_path.return_value = "/path/to/houdini/hython"

    with patch.object(HoudiniAdaptor, "config", new_callable=PropertyMock) as mock:
        mock.return_value = config_mock
        yield config_mock
