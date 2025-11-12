# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import os
from unittest import mock
from pathlib import Path
import pytest
from openjd.adaptor_runtime.adaptors import PathMappingRule
from deadline.houdini_adaptor.HoudiniAdaptor import HoudiniAdaptor
from typing import Optional


houdini_pathmap_test_cases = [
    pytest.param([], None, id="No pathmapping rules"),
    pytest.param(
        [
            PathMappingRule(
                source_path_format="Linux",
                source_path="/home/users/usera/folder",
                destination_path="/sessions/session-abc",
            )
        ],
        "{'/home/users/usera/folder': '/sessions/session-abc'}",
        id="Single pathmapping rule",
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
        id="Mutliple pathmapping rules",
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
        id="Backslashes in rule",
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
    assert os.environ.get("HOUDINI_PATHMAP") == houdini_pathmap


redshift_pathmap_test_cases = [
    pytest.param({}, [], "", id="No rules, no pre-existing file or string override"),
    pytest.param(
        {},
        [
            PathMappingRule(
                source_path_format="Linux",
                source_path="/home/users/usera/folder",
                destination_path="/sessions/session-abc",
            )
        ],
        '"/home/users/usera/folder" "/sessions/session-abc"',
        id="Single rule, no pre-existing file or string override",
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
        '"/sourcea/testa" "/sessions/session-abc" "/sourceb/testb" "/sessions/session-abc" "/sourcec/testc" "/sessions/session-abc"',
        id="Multiple rules, no pre-existing file or string override",
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
    expected_output_rules: set[str],
    tmp_path: Path,
) -> None:

    # GIVEN
    with (mock.patch.dict(os.environ, input_env, clear=True),):
        adaptor = HoudiniAdaptor(init_data)
        adaptor._path_mapping_rules = input_rules
        tmp_rs_rule_filepath = Path(tmp_path, "temp_rs_rules.txt")
        with mock.patch(
            "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.Path",
            return_value=tmp_rs_rule_filepath,
        ):
            # WHEN
            adaptor._set_redshift_pathmap()

            # THEN
            if expected_output_rules:
                assert os.environ["REDSHIFT_PATHOVERRIDE_FILE"] == str(tmp_rs_rule_filepath)
                with open(tmp_rs_rule_filepath, mode="r", encoding="utf-8") as rs_rule_file:
                    rs_rules = rs_rule_file.readlines()
                assert len(rs_rules) == 1
                assert rs_rules[0] == expected_output_rules
            else:
                assert os.environ.get("REDSHIFT_PATHOVERRIDE_FILE") is None


@pytest.mark.parametrize(
    ("existing_file_content", "existing_string_content", "input_rules", "expected_output_rules"),
    [
        pytest.param(
            '"/existing/path1" "/existing/dest1"\n"/existing/path2" "/existing/dest2"',
            None,
            [
                PathMappingRule(
                    source_path_format="Linux",
                    source_path="/new/path",
                    destination_path="/sessions/session-abc",
                )
            ],
            '"/new/path" "/sessions/session-abc" "/existing/path1" "/existing/dest1" "/existing/path2" "/existing/dest2"',
            id="Existing override file with rules",
        ),
        pytest.param(
            None,
            '"/existing/path1" "/existing/dest1" "/existing/path2" "/existing/dest2"',
            [
                PathMappingRule(
                    source_path_format="Linux",
                    source_path="/new/path",
                    destination_path="/sessions/session-abc",
                )
            ],
            '"/new/path" "/sessions/session-abc" "/existing/path1" "/existing/dest1" "/existing/path2" "/existing/dest2"',
            id="Existing override string with rules",
        ),
        pytest.param(
            '"/existing/path1" "/existing/dest1" "/existing/path2" "/existing/dest2"\n"/one/moresource" "/one/moredest"',
            '"/existing/path3" "/existing/dest3" "/existing/path4" "/existing/dest4"',
            [
                PathMappingRule(
                    source_path_format="Linux",
                    source_path="/new/path",
                    destination_path="/sessions/session-abc",
                )
            ],
            '"/new/path" "/sessions/session-abc" "/existing/path1" "/existing/dest1" "/existing/path2" "/existing/dest2" "/one/moresource" "/one/moredest"',
            id="Existing override string and file with rules",
        ),
    ],
)
def test_set_redshift_pathmap_with_existing_file_and_string(
    init_data,
    existing_file_content: Optional[str],
    existing_string_content: Optional[str],
    input_rules: list[PathMappingRule],
    expected_output_rules: str,
    tmp_path: Path,
) -> None:

    # GIVEN
    input_env = {}

    if existing_file_content:
        existing_file = Path(tmp_path, "existing_rules.txt")
        with open(existing_file, "w", encoding="utf-8") as f:
            f.write(existing_file_content)
        input_env["REDSHIFT_PATHOVERRIDE_FILE"] = str(existing_file)

    if existing_string_content:
        input_env["REDSHIFT_PATHOVERRIDE_STRING"] = existing_string_content

    with (mock.patch.dict(os.environ, input_env, clear=True),):
        adaptor = HoudiniAdaptor(init_data)
        adaptor._path_mapping_rules = input_rules
        tmp_rs_rule_file = Path(tmp_path, "tmp_rs_rules.txt")

        with mock.patch(
            "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.Path",
            return_value=tmp_rs_rule_file,
        ):
            # WHEN
            adaptor._set_redshift_pathmap()

            # THEN
            assert os.environ["REDSHIFT_PATHOVERRIDE_FILE"] == str(tmp_rs_rule_file)
            with open(tmp_rs_rule_file, mode="r", encoding="utf-8") as rs_rule_file:
                rs_rules = rs_rule_file.readlines()
            assert len(rs_rules) == 1
            assert rs_rules[0] == expected_output_rules
