# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from unittest.mock import Mock, patch
import pytest
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _get_job_template,
    RenderStrategy,
)


@pytest.fixture
def mock_parm():
    def mock_implementation(name: str):
        if name == "name":
            mock_name = Mock()
            mock_name.evalAsString.return_value = "My Job"
            return mock_name
        if name == "include_adaptor_wheels":
            mock_name = Mock()
            mock_name.eval.return_value = False
            return mock_name
        if name == "description":
            mock_name = Mock()
            mock_name.evalAsString.return_value = "My job description"
            return mock_name
        return Mock()

    return mock_implementation


@pytest.fixture(autouse=True)
def init_mocks():
    with (
        patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._get_houdini_version",
            Mock(return_value="19.5.435"),
        ),
        patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._get_hip_file",
            Mock(return_value="/path/to/hip.hip"),
        ),
    ):
        yield


@pytest.fixture()
def mock_get_steps():
    with patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._get_steps"
    ) as mock:
        yield mock


def test_job_template(mock_get_steps, mock_parm):
    mock_node = Mock()
    mock_node.userData.return_value = None
    mock_node.name = "Test Job"
    mock_node.parm = mock_parm

    mock_get_steps.return_value = [
        {
            "id": "1",
            "name": "/mantra-1",
            "dependency_ids": [],
            "rop": "/mantra",
            "wedgenum": "",
            "wedge_node": "",
            "start": 1,
            "end": 5,
            "step": 1,
            "render_strategy": RenderStrategy.PARALLEL,
        }
    ]
    template = _get_job_template(mock_node)

    assert template == {
        "specificationVersion": "jobtemplate-2023-09",
        "name": "My Job",
        "description": "My job description",
        "parameterDefinitions": [
            {
                "name": "HipFile",
                "type": "PATH",
                "objectType": "FILE",
                "dataFlow": "IN",
                "default": "/path/to/hip.hip",
            }
        ],
        "steps": [
            {
                "name": "/mantra-1",
                "parameterSpace": {
                    "taskParameterDefinitions": [{"name": "Frame", "range": "1-5:1", "type": "INT"}]
                },
                "stepEnvironments": [
                    {
                        "name": "Houdini",
                        "description": "Runs Houdini in the background.",
                        "script": {
                            "embeddedFiles": [
                                {
                                    "name": "initData",
                                    "filename": "init-data.yaml",
                                    "type": "TEXT",
                                    "data": "ignore_input_nodes: true\nrender_node: /mantra\nscene_file: '{{Param.HipFile}}'\nversion: 19.5.435\nwedge_node: ''\nwedgenum: ''\n",
                                }
                            ],
                            "actions": {
                                "onEnter": {
                                    "command": "houdini-openjd",
                                    "args": [
                                        "daemon",
                                        "start",
                                        "--path-mapping-rules",
                                        "file://{{Session.PathMappingRulesFile}}",
                                        "--connection-file",
                                        "{{ Session.WorkingDirectory }}/connection.json",
                                        "--init-data",
                                        "file://{{ Env.File.initData }}",
                                    ],
                                    "cancelation": {"mode": "NOTIFY_THEN_TERMINATE"},
                                },
                                "onExit": {
                                    "command": "houdini-openjd",
                                    "args": [
                                        "daemon",
                                        "stop",
                                        "--connection-file",
                                        "{{ Session.WorkingDirectory }}/connection.json",
                                    ],
                                    "cancelation": {"mode": "NOTIFY_THEN_TERMINATE"},
                                },
                            },
                        },
                    }
                ],
                "script": {
                    "embeddedFiles": [
                        {
                            "name": "runData",
                            "filename": "run-data.yaml",
                            "type": "TEXT",
                            "data": "frame_range:\n  end: {{Task.Param.Frame}}\n  start: {{Task.Param.Frame}}\n  step: 1\nignore_input_nodes: true\nrender_node: /mantra\n",
                        }
                    ],
                    "actions": {
                        "onRun": {
                            "command": "houdini-openjd",
                            "args": [
                                "daemon",
                                "run",
                                "--connection-file",
                                "{{ Session.WorkingDirectory }}/connection.json",
                                "--run-data",
                                "file://{{ Task.File.runData }}",
                            ],
                            "cancelation": {"mode": "NOTIFY_THEN_TERMINATE"},
                        }
                    },
                },
            }
        ],
    }


def test_job_template_sequential_node(mock_get_steps, mock_parm):
    mock_node = Mock()
    mock_node.userData.return_value = None
    mock_node.name = "Test Job"
    mock_node.parm = mock_parm

    mock_get_steps.return_value = [
        {
            "id": "1",
            "name": "/geo-1",
            "dependency_ids": [],
            "rop": "/geo",
            "wedgenum": "",
            "wedge_node": "",
            "start": 1,
            "end": 5,
            "step": 1,
            "render_strategy": RenderStrategy.SEQUENTIAL,
        }
    ]
    template = _get_job_template(mock_node)

    assert "parameterSpace" not in template["steps"][0]
    assert (
        template["steps"][0]["script"]["embeddedFiles"][0]["data"]
        == "frame_range:\n  end: 5\n  start: 1\n  step: 1\nignore_input_nodes: true\nrender_node: /geo\n"
    )
