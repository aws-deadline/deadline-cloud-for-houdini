# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import pytest
from pathlib import Path


@pytest.fixture
def hython_location() -> Path:
    return Path(os.environ["HYTHON_EXECUTABLE"])


@pytest.fixture
def script_location() -> Path:
    return Path(__file__).parent / "test_scripts"
