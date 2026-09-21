# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Guards the dependency floor that AWS Console sign-in depends on.

Console sign-in is not exercised by the integration tests: CI authenticates by assuming a
role, so the console path is never taken there. What can break silently is the dependency
declaration, which is what this test pins.

Reads ``pyproject.toml`` directly rather than installed distribution metadata, since
``importlib.metadata`` would not see an edit until the environment is reinstalled.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.9 and 3.10 only
    import tomli as tomllib

SCRIPTS_DIR = Path(__file__).parents[3] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    # Appended, not prepended: scripts/ holds generically named modules (common.py) that
    # would otherwise shadow same-named imports for the rest of the session.
    sys.path.append(str(SCRIPTS_DIR))

import deps_bundle  # noqa: E402

PYPROJECT = Path(__file__).parents[3] / "pyproject.toml"

# 0.60.1-0.60.3 have no AWS_CONSOLE_LOGIN credentials source and declare no `console`
# extra; 0.60.3 is the highest version that must stay excluded.
HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN = "0.60.3"


class _StopBuild(Exception):
    """Cuts build_deps_bundle short once the assertion's subject has been captured."""


def _raw_dependencies() -> list[str]:
    node = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "project" in node, "pyproject.toml has no project table"
    assert "dependencies" in node["project"], "pyproject.toml has no project.dependencies"
    return node["project"]["dependencies"]


def _base_dependencies() -> list[Requirement]:
    return [Requirement(r) for r in _raw_dependencies()]


def test_deadline_floor_excludes_releases_without_console_signin():
    """Pins the declared deadline floor, independent of whatever a resolver selects."""
    deadline_reqs = [r for r in _base_dependencies() if r.name == "deadline"]
    assert deadline_reqs, "pyproject.toml declares no requirement on deadline"
    for req in deadline_reqs:
        assert not req.specifier.contains(HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN), (
            f"allows deadline {HIGHEST_DEADLINE_WITHOUT_CONSOLE_SIGNIN}, which has no "
            f"console sign-in support: {req}"
        )


@pytest.mark.parametrize(
    "requirement, expected",
    [
        ("deadline>=0.60.4,<0.61", "deadline[console]>=0.60.4,<0.61"),
        ("deadline[gui]>=0.60.4", "deadline[gui,console]>=0.60.4"),
        ("deadline[console]>=0.60.4", "deadline[console]>=0.60.4"),
        ("openjd-adaptor-runtime>=0.7,<0.10", "openjd-adaptor-runtime>=0.7,<0.10"),
        # Extra names normalize per PEP 685, so a re-spelling is still recognised as present
        # rather than duplicated.
        ("deadline[Console]>=0.60.4", "deadline[console]>=0.60.4"),
        ("deadline[CONSOLE]>=0.60.4", "deadline[console]>=0.60.4"),
        ("deadline[Gui]>=0.60.4", "deadline[gui,console]>=0.60.4"),
        ("Deadline>=0.60.4", "Deadline[console]>=0.60.4"),
    ],
)
def test_add_console_extra_pins_behavior(requirement, expected):
    """Pins _add_console_extra's contract: preserve extras, be idempotent, ignore others."""
    assert deps_bundle._add_console_extra(requirement) == expected


@pytest.mark.parametrize(
    "requirement",
    ["deadline[console]>=0.60.4", "deadline[Console]>=0.60.4", "Deadline[CONSOLE]>=0.60.4"],
)
def test_requests_console_extra_ignores_spelling(requirement):
    """The guard reads normalized extras, so a re-spelling still counts as requesting it."""
    assert deps_bundle._requests_console_extra(requirement)


def test_parse_requirement_rejects_detached_extras():
    """Extras that do not directly follow the name would rebuild into an invalid requirement."""
    assert deps_bundle._parse_requirement("deadline [gui]>=1") is None
    assert deps_bundle._add_console_extra("deadline [gui]>=1") == "deadline [gui]>=1"


def test_add_console_extra_changes_the_real_base_dependencies():
    """Pins that the injection reaches pyproject.toml's real dependencies.

    _add_console_extra no-ops on anything it does not recognise as `deadline`, so a rename or
    a re-spelling would leave the bundle with no console extra while the parametrized test
    above kept passing.
    """
    dependencies_for_pip = [
        deps_bundle._add_console_extra(deps_bundle.Dependency(dep).for_pip())
        for dep in _raw_dependencies()
    ]
    assert any(deps_bundle._requests_console_extra(dep) for dep in dependencies_for_pip), (
        "no dependency requests deadline's `console` extra after _add_console_extra: "
        f"{dependencies_for_pip}"
    )


def test_build_base_environment_requires_something_to_request_the_console_extra(
    tmp_path, monkeypatch
):
    """The postcondition fails the build when nothing requests the extra, and names what it
    rejected.
    """
    monkeypatch.setattr(deps_bundle.subprocess, "run", lambda args, **kwargs: None)

    with pytest.raises(Exception, match="console") as raised:
        deps_bundle._build_base_environment(tmp_path, [deps_bundle.Dependency("not-deadline>=1")])

    assert "not-deadline>=1" in str(raised.value)


def test_build_deps_bundle_passes_a_reiterable_dependency_collection(monkeypatch):
    """The dependencies handed to _build_base_environment must survive a second pass.

    They were a lazy `filter`, which anything reading them twice would see as empty.
    """
    monkeypatch.setattr(deps_bundle, "get_project_dict", lambda: {"project": {}})
    monkeypatch.setattr(
        deps_bundle,
        "get_dependencies",
        lambda project_dict: [
            deps_bundle.Dependency("deadline[console] >= 0.60.4,< 0.61"),
            deps_bundle.Dependency("openjd-adaptor-runtime >= 0.7,< 0.10"),
        ],
    )

    captured: list = []

    def capture(working_directory, dependencies):
        captured.append(dependencies)
        raise _StopBuild

    monkeypatch.setattr(deps_bundle, "_build_base_environment", capture)

    with pytest.raises(_StopBuild):
        deps_bundle.build_deps_bundle()

    dependencies = captured[0]
    first_pass = [dep.for_pip() for dep in dependencies]
    second_pass = [dep.for_pip() for dep in dependencies]

    assert first_pass == ["deadline[console]>=0.60.4,<0.61"], "openjd must be filtered out"
    assert second_pass == first_pass, "the collection is one-shot; a second reader sees nothing"


def test_build_base_environment_accepts_an_already_declared_console_extra(tmp_path, monkeypatch):
    """_add_console_extra is idempotent, so a declared `deadline[console]` is valid input."""
    recorded: list[list[str]] = []

    def record(args, **kwargs):
        recorded.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(deps_bundle.subprocess, "run", record)
    monkeypatch.setattr(deps_bundle, "_verify_console_resolution", lambda *args: None)

    deps_bundle._build_base_environment(
        tmp_path, [deps_bundle.Dependency("deadline[console]>=0.60.4,<0.61")]
    )

    assert any("deadline[console]>=0.60.4,<0.61" in args for args in recorded)


def test_verify_console_resolution_accepts_a_resolution_carrying_awscrt(tmp_path, monkeypatch):
    """awscrt resolved, so the closure carries the console extra and the build proceeds."""
    monkeypatch.setattr(deps_bundle, "_get_package_version", lambda package, path: "0.31.2")

    deps_bundle._verify_console_resolution(tmp_path)


def test_verify_console_resolution_requires_awscrt(tmp_path, monkeypatch):
    """awscrt reaches the bundle only through the extra, so its absence fails the build.

    This is the one case pip exits 0 for: a closure that lost the extra installs cleanly.
    """

    def version(package, install_path):
        raise Exception(f"Could not find version for package {package}")

    monkeypatch.setattr(deps_bundle, "_get_package_version", version)

    with pytest.raises(Exception, match="awscrt"):
        deps_bundle._verify_console_resolution(tmp_path)
