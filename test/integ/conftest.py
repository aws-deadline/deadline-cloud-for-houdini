# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def hython_location() -> Path:
    if not os.environ.get("HYTHON_EXECUTABLE"):
        houdini_version_env = os.environ.get("HOUDINI_VERSION")
        if not houdini_version_env:
            raise ValueError("Either HYTHON_EXECUTABLE or HOUDINI_VERSION must be set")

        if os.name == "nt":
            hython_path = f"C:\\Tools\\houdini-{houdini_version_env}\\bin\\hython.exe"
        elif sys.platform == "darwin":
            hython_path = f"/Applications/Houdini/Houdini{houdini_version_env}/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython"
        else:  # Linux
            hython_path = f"/opt/hfs{houdini_version_env}/bin/hython"

        os.environ["HYTHON_EXECUTABLE"] = hython_path

    return Path(os.environ["HYTHON_EXECUTABLE"])


@pytest.fixture
def script_location() -> Path:
    return Path(__file__).parent / "test_scripts"
