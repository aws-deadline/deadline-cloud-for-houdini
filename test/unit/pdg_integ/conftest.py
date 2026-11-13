# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
from pathlib import Path

# These tests import the code under test as ``test.integ.pdg``, from the repo root. pytest
# puts only test/unit on the path while it collects them, so the root goes first.
_REPO_ROOT: str = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
