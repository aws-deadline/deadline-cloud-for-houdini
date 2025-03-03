# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import os
import tempfile
from unittest import mock
from pathlib import Path
import pytest
from openjd.adaptor_runtime.adaptors import PathMappingRule
from deadline.houdini_adaptor.HoudiniAdaptor import HoudiniAdaptor
from .test_adaptor import init_data

houdini_pathmap_test_cases = [
    pytest.param([], "", id="no pathmapping rules"),
    pytest.param(
        [
            PathMappingRule(
                source_path_format="Linux",
                source_path="/home/users/usera/folder",
                destination_path="/sessions/session-abc",
            )
        ],
        "{'/home/users/usera/folder': '/sessions/session-abc'}",
        id="single pathmapping rule",
    ),
    pytest.param(
        [
            PathMappingRule(
                source_path_format="Linux",
                source_path="/sourcea/testa",
                destination_path="/sessions/session-abc",
            ),
            PathMappingRule(
                source_path_format="Linux",
                source_path="/sourceb/testb",
                destination_path="/sessions/session-abc",
            ),
            PathMappingRule(
                source_path_format="Linux",
                source_path="/sourcec/testc",
                destination_path="/sessions/session-abc",
            ),
        ],
        "{'/sourcea/testa': '/sessions/session-abc', '/sourceb/testb': '/sessions/session-abc', '/sourcec/testc': '/sessions/session-abc'}",
        id="mutliple pathmapping rules",
    ),
    pytest.param(
        [
            PathMappingRule(
                source_path_format="Windows",
                source_path="C:\\Users\\usera\\folder",
                destination_path="/sessions/session-abc",
            ),
        ],
        "{'C:/Users/usera/folder': '/sessions/session-abc'}",
        id="backslashes in rule",
    ),
]


@pytest.mark.parametrize(("input_rules", "houdini_pathmap"), houdini_pathmap_test_cases)
@mock.patch.dict(os.environ, {}, clear=True)
def test_set_houdini_pathmap(
    init_data,
    input_rules: list[PathMappingRule],
    houdini_pathmap: str,
) -> None:

    # GIVEN
    adaptor = HoudiniAdaptor(init_data)
    adaptor._path_mapping_rules = input_rules

    assert "HOUDINI_PATHMAP" not in os.environ

    # WHEN
    adaptor._set_houdini_pathmap()

    # THEN
    assert "HOUDINI_PATHMAP" in os.environ
    assert os.environ["HOUDINI_PATHMAP"] == houdini_pathmap


redshift_pathmap_test_cases = [
    pytest.param({}, [], [], id="no rules, no pre-existing file or string override"),
    pytest.param(
        {},
        [
            PathMappingRule(
                source_path_format="Linux",
                source_path="/home/users/usera/folder",
                destination_path="/sessions/session-abc",
            )
        ],
        ['"/home/users/usera/folder" "/sessions/session-abc"'],
        id="single rule, no pre-existing file or string override",
    ),
    pytest.param(
        {},
        [
            PathMappingRule(
                source_path_format="Linux",
                source_path="/sourcea/testa",
                destination_path="/sessions/session-abc",
            ),
            PathMappingRule(
                source_path_format="Linux",
                source_path="/sourceb/testb",
                destination_path="/sessions/session-abc",
            ),
            PathMappingRule(
                source_path_format="Linux",
                source_path="/sourcec/testc",
                destination_path="/sessions/session-abc",
            ),
        ],
        [
            '"/sourcea/testa" "/sessions/session-abc"',
            '"/sourceb/testb" "/sessions/session-abc"',
            '"/sourcec/testc" "/sessions/session-abc"',
        ],
        id="multiple rules, no pre-existing file or string override",
    ),
]


@pytest.mark.parametrize(
    ("input_env", "input_rules", "expected_output_rules"),
    redshift_pathmap_test_cases,
)
def test_set_redshift_pathmap(
    init_data,
    input_env: dict[str, str],
    input_rules: list[PathMappingRule],
    expected_output_rules: list[str],
) -> None:

    # GIVEN
    with (
        mock.patch.dict(os.environ, input_env, clear=True),
        tempfile.TemporaryDirectory() as temp_dir,
    ):
        adaptor = HoudiniAdaptor(init_data)
        adaptor._path_mapping_rules = input_rules
        temp_rs_rule_filepath = Path(temp_dir, "temp_rs_rules.txt")
        with mock.patch(
            "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.Path",
            return_value=temp_rs_rule_filepath,
        ):
            # WHEN
            adaptor._set_redshift_pathmap()

            # THEN
            if expected_output_rules:
                assert os.environ["REDSHIFT_PATHOVERRIDE_FILE"] == str(temp_rs_rule_filepath)
                with open(temp_rs_rule_filepath, mode="r", encoding="utf-8") as rs_rule_file:
                    rs_rules = rs_rule_file.read().splitlines()
                assert rs_rules == expected_output_rules
            else:
                assert input_env.get("REDSHIFT_PATHOVERRIDE_PATH", None) == os.environ.get(
                    "REDSHIFT_PATHOVERRIDE_PATH", None
                )
            assert input_env.get("REDSHIFT_PATHOVERRIDE_STRING", None) == os.environ.get(
                "REDSHIFT_PATHOVERRIDE_STRING", None
            )
