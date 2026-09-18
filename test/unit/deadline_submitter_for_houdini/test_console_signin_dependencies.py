# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Guards the dependency floor that AWS Console sign-in depends on.

Console sign-in is not exercised by the integration tests: it needs an interactive
browser OAuth handshake and Deadline Cloud Monitor, while CI authenticates by
assuming a role, so credentials are host-provided and the console path is never
taken. What can break silently is the dependency declaration, which is what this
test pins.

The test reads ``pyproject.toml`` rather than installed distribution metadata.
``importlib.metadata`` reflects what was captured at install time, so an edit to
``pyproject.toml`` would not be seen until the environment is reinstalled -- and
"somebody edited that line" is precisely the regression being guarded.
"""

import sys
from pathlib import Path

from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.9 and 3.10 only
    import tomli as tomllib

PYPROJECT = Path(__file__).parents[3] / "pyproject.toml"

# Console sign-in landed in deadline 0.60.4 and nowhere earlier: 0.60.1 through
# 0.60.3 have no AWS_CONSOLE_LOGIN credentials source and do not declare a
# `console` extra at all. 0.60.3 is the highest version that must be excluded.
HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN = "0.60.3"


def _base_dependencies() -> list[Requirement]:
    node = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "project" in node, "pyproject.toml has no project table"
    assert "dependencies" in node["project"], "pyproject.toml has no project.dependencies"
    return [Requirement(r) for r in node["project"]["dependencies"]]


def test_deadline_floor_excludes_releases_without_console_signin():
    """Guards the floor itself, not whatever a resolver happened to select.

    An installed-version check cannot do this: with a loosened ">= 0.60.1"
    requirement, pip still resolves the newest 0.60.x, so the regression passes
    unnoticed. Below 0.60.4 there is also no `console` extra, so the bundler's
    request for deadline[console] would make pip backtrack past the extra, drop
    awscrt, warn once, and exit 0.
    """
    deadline_reqs = [r for r in _base_dependencies() if r.name == "deadline"]
    assert deadline_reqs, "pyproject.toml declares no requirement on deadline"
    for req in deadline_reqs:
        assert not req.specifier.contains(HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN), (
            f"allows deadline {HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN}, which has no "
            f"console sign-in support: {req}"
        )
