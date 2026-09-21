# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Guards the dependency floor that AWS Console sign-in depends on.

Console sign-in is not exercised by the integration tests: CI authenticates by assuming a
role, so the console path is never taken there. What can break silently is the dependency
declaration, which is what this test pins.

Reads ``pyproject.toml`` directly rather than installed distribution metadata, since
``importlib.metadata`` would not see an edit until the environment is reinstalled.
"""

import sys
from pathlib import Path

from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.9 and 3.10 only
    import tomli as tomllib

PYPROJECT = Path(__file__).parents[3] / "pyproject.toml"

# 0.60.1-0.60.3 have no AWS_CONSOLE_LOGIN credentials source and declare no `console`
# extra; 0.60.3 is the highest version that must stay excluded.
HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN = "0.60.3"


def _base_dependencies() -> list[Requirement]:
    node = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "project" in node, "pyproject.toml has no project table"
    assert "dependencies" in node["project"], "pyproject.toml has no project.dependencies"
    return [Requirement(r) for r in node["project"]["dependencies"]]


def test_deadline_floor_excludes_releases_without_console_signin():
    """Pins the declared deadline floor, independent of whatever a resolver selects."""
    deadline_reqs = [r for r in _base_dependencies() if r.name == "deadline"]
    assert deadline_reqs, "pyproject.toml declares no requirement on deadline"
    for req in deadline_reqs:
        assert not req.specifier.contains(HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN), (
            f"allows deadline {HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN}, which has no "
            f"console sign-in support: {req}"
        )
