# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for finding hython and building the environment it cooks in, with no Houdini."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

import pytest

from test.integ.pdg.hython_env import (
    REPO_ROOT,
    find_hython,
    hython_env,
    installed_version,
    plugin_env_path,
)


def test_a_named_hython_wins_over_the_houdini_version() -> None:
    """HYTHON_EXECUTABLE is used as given, whatever HOUDINI_VERSION says."""
    env: Dict[str, str] = {"HYTHON_EXECUTABLE": "/my/hython", "HOUDINI_VERSION": "21.0.596"}
    assert find_hython(env, platform="linux") == Path("/my/hython")


@pytest.mark.parametrize(
    "platform, expected",
    [
        pytest.param("linux", "/opt/hfs21.0.596/bin/hython", id="linux"),
        pytest.param("win32", "C:\\Tools\\houdini-21.0.596\\bin\\hython.exe", id="windows"),
        pytest.param(
            "darwin",
            "/Applications/Houdini/Houdini21.0.596/Frameworks/Houdini.framework/Versions/"
            "Current/Resources/bin/hython",
            id="macos",
        ),
    ],
)
def test_the_houdini_version_gives_the_default_install_on_each_platform(
    platform: str, expected: str
) -> None:
    """Without a named hython, the version's default install is used, as the other integ tests do."""
    assert find_hython({"HOUDINI_VERSION": "21.0.596"}, platform=platform) == Path(expected)


def test_without_either_variable_there_is_no_hython() -> None:
    """The error names both variables, so the developer knows what to set."""
    with pytest.raises(ValueError, match="HYTHON_EXECUTABLE or HOUDINI_VERSION"):
        find_hython({"HYTHON_EXECUTABLE": ""}, platform="linux")


def test_the_dependency_folder_is_named_for_houdinis_major_and_minor_version() -> None:
    """Every patch release of one Houdini shares the dev install's folder."""
    assert plugin_env_path("21.0.596") == REPO_ROOT / "plugin_env_21.0"
    assert plugin_env_path("20.5") == REPO_ROOT / "plugin_env_20.5"


def test_hython_loads_this_checkout_and_the_given_config(tmp_path: Path) -> None:
    """The plugin and submitter come first on HOUDINI_PATH, then Houdini's own folders.

    PYTHONPATH is replaced, so a path the test run had doesn't reach Houdini's Python, and
    the user's Houdini packages are skipped.
    """
    # A caller environment with its own PYTHONPATH, and a setting hython should keep.
    base: Dict[str, str] = {"PYTHONPATH": "/somewhere/else", "AWS_PROFILE": "studio"}

    env: Dict[str, str] = hython_env(
        base, plugin_env=tmp_path / "plugin_env", deadline_config=tmp_path / "config"
    )

    src: Path = REPO_ROOT / "src" / "deadline"
    assert env["HOUDINI_PATH"].split(os.pathsep) == [
        str(src / "houdini_pdg" / "houdini_plugin"),
        str(src / "houdini_submitter"),
        "&",
    ]
    assert env["PYTHONPATH"].split(os.pathsep) == [
        str(REPO_ROOT / "src"),
        str(src / "houdini_submitter" / "python"),
        str(tmp_path / "plugin_env"),
    ]
    assert env["HOUDINI_PACKAGE_SKIP"] == "1"
    assert env["DEADLINE_CONFIG_FILE_PATH"] == str(tmp_path / "config")
    assert env["AWS_PROFILE"] == "studio"
    assert base == {"PYTHONPATH": "/somewhere/else", "AWS_PROFILE": "studio"}


def test_an_installed_version_is_read_from_its_dist_info_folder(tmp_path: Path) -> None:
    """The workers get the exact version hython cooked with, not one that only shares a prefix."""
    # Two distributions whose names start alike, as deadline and deadline_job_attachments do.
    (tmp_path / "deadline-0.60.11.dist-info").mkdir()
    (tmp_path / "deadline_job_attachments-0.1.5.dist-info").mkdir()
    (tmp_path / "openjd_adaptor_runtime-0.9.6.dist-info").mkdir()

    assert installed_version(tmp_path, "deadline") == "0.60.11"
    assert installed_version(tmp_path, "openjd_adaptor_runtime") == "0.9.6"


def test_a_missing_distribution_says_to_rebuild_the_folder(tmp_path: Path) -> None:
    """A folder built without a library can't give its version, and deleting it rebuilds it."""
    with pytest.raises(FileNotFoundError, match="has no deadline. Delete it to rebuild it"):
        installed_version(tmp_path, "deadline")


def test_the_repo_root_is_this_checkout() -> None:
    """The plugin, submitter and dev install's scripts are all found from the repo's root."""
    assert REPO_ROOT == Path(__file__).resolve().parents[3]
    assert (REPO_ROOT / "scripts" / "install_dev_submitter.py").is_file()
