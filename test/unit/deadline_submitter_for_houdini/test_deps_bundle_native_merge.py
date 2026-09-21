# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Guards which compiled artifact the dependency bundle ships for each interpreter.

The bundle is one flat ``PYTHONPATH`` directory, so it holds a single file per name however
many Pythons Houdini embeds. When two versions install the same filename the survivor is
the only copy any interpreter can load, and one built for a newer Python fails to import
on an older one.

These tests drive the merge over synthetic trees named the way the real wheels name their
extension modules. They assert which artifact is selected, not that it loads -- that would
need the target interpreter, which the unit suite has no access to.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parents[3] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    # Appended, not prepended: scripts/ holds generically named modules (common.py) that
    # would otherwise shadow same-named imports for the rest of the session.
    sys.path.append(str(SCRIPTS_DIR))

import deps_bundle  # noqa: E402

# awscrt's abi3 wheels all install this one name, whatever Python they were built for.
ABI3_ARTIFACT = "_awscrt.abi3.so"

# awscrt publishes version-specific wheels below this version, abi3 wheels from here up.
FIRST_ABI3_PYTHON = (3, 11)

# Stands in for a build host whose interpreter is none of the supported versions, so the
# merge must overwrite it.
BASE_ENV_SENTINEL = "base-env-host-resolved"


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _tag(version: str) -> str:
    """The interpreter tag a wheel puts in a version-specific extension module name."""
    return version.replace(".", "")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def supported_versions() -> list[str]:
    versions = sorted(deps_bundle.SUPPORTED_PYTHON_VERSIONS, key=_version_key)
    assert len(versions) >= 2, "a filename collision needs at least two supported versions"
    return versions


@pytest.fixture
def abi3_versions(supported_versions) -> list[str]:
    versions = [v for v in supported_versions if _version_key(v) >= FIRST_ABI3_PYTHON]
    assert versions and len(versions) < len(supported_versions), (
        "FIRST_ABI3_PYTHON needs supported versions on both sides of it, or these tests stop "
        "covering one of the two naming schemes"
    )
    return versions


@pytest.fixture
def merged_bundle(tmp_path, supported_versions, abi3_versions) -> Path:
    """Run the merge over trees named the way the real wheels name their artifacts.

    Each file's content records the version whose install produced it, so the merged tree
    reports where its contents came from.
    """
    base_env = tmp_path / "base_env"
    _write(base_env / ABI3_ARTIFACT, BASE_ENV_SENTINEL)

    native_paths = []
    for version in supported_versions:
        tree = tmp_path / "native" / _tag(version)
        native_paths.append(tree)
        if version in abi3_versions:
            _write(tree / ABI3_ARTIFACT, version)
        else:
            _write(tree / f"_awscrt.cpython-{_tag(version)}-darwin.so", version)
        _write(tree / "xxhash" / f"_xxhash.cpython-{_tag(version)}-darwin.so", version)
        _write(tree / "yaml" / f"_yaml.cpython-{_tag(version)}-darwin.so", version)
        _write(tree / "psutil" / "_psutil_osx.abi3.so", "shared")

    deps_bundle._copy_native_to_base_env(base_env, native_paths)
    return base_env


def test_colliding_abi3_artifact_comes_from_the_lowest_supported_abi(merged_bundle, abi3_versions):
    """abi3 is forward-compatible only, so the lowest copy is the one that serves all."""
    lowest_abi3_version = abi3_versions[0]
    shipped = (merged_bundle / ABI3_ARTIFACT).read_text()

    assert shipped != BASE_ENV_SENTINEL, (
        f"{ABI3_ARTIFACT} is the base environment's host-resolved copy; the merge must "
        f"overwrite it with the copy built for Python {lowest_abi3_version}"
    )
    assert shipped == lowest_abi3_version, (
        f"{ABI3_ARTIFACT} was built for Python {shipped}, so it cannot be imported by "
        f"Python {lowest_abi3_version}; the copy built for the lowest supported abi3 "
        f"version is the one every supported interpreter can load"
    )


def test_abi3_collision_between_trees_keeps_the_lowest_version(tmp_path):
    """The merge's first-tree-wins rule, over a name more than one tree supplies.

    SUPPORTED_PYTHON_VERSIONS currently has a single version at or above FIRST_ABI3_PYTHON,
    so the fixture above cannot produce an abi3 collision between two trees; this drives the
    rule directly rather than depending on the shipped list ever having two.
    """
    base_env = tmp_path / "base_env"
    _write(base_env / ABI3_ARTIFACT, BASE_ENV_SENTINEL)

    abi3_only_versions = ["3.11", "3.12", "3.13"]
    native_paths = []
    for version in abi3_only_versions:
        tree = tmp_path / "native" / _tag(version)
        native_paths.append(tree)
        _write(tree / ABI3_ARTIFACT, version)

    deps_bundle._copy_native_to_base_env(base_env, native_paths)

    assert (base_env / ABI3_ARTIFACT).read_text() == abi3_only_versions[0], (
        f"{ABI3_ARTIFACT} must come from the lowest abi3 tree; a later tree's copy does not "
        "load on the interpreters below it"
    )


def test_version_specific_artifacts_are_kept_for_every_supported_version(
    merged_bundle, supported_versions, abi3_versions
):
    """Version-tagged names never collide, so every supported version keeps its own."""
    for version in supported_versions:
        for package, module in (("xxhash", "_xxhash"), ("yaml", "_yaml")):
            artifact = merged_bundle / package / f"{module}.cpython-{_tag(version)}-darwin.so"
            assert (
                artifact.exists()
            ), f"the bundle carries no {package} artifact for Python {version}"
            assert artifact.read_text() == version

    for version in supported_versions:
        if version in abi3_versions:
            continue
        awscrt_non_abi3 = merged_bundle / f"_awscrt.cpython-{_tag(version)}-darwin.so"
        assert (
            awscrt_non_abi3.exists()
        ), f"the bundle carries no awscrt artifact for Python {version}"
        assert awscrt_non_abi3.read_text() == version


def test_native_trees_are_merged_lowest_python_version_first(tmp_path, monkeypatch):
    """Download order picks the collision winner, and it sorts numerically: as text, "3.9"
    would land after "3.10". These versions are chosen to expose that, not to describe what
    is supported.
    """
    monkeypatch.setattr(deps_bundle, "SUPPORTED_PYTHON_VERSIONS", ["3.13", "3.9", "3.11", "3.10"])
    monkeypatch.setattr(deps_bundle, "_get_package_version", lambda package, install_path: "1.2.3")

    requested_versions: list[str] = []

    def record(args, **kwargs):
        requested_versions.append(args[args.index("--python-version") + 1])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(deps_bundle.subprocess, "run", record)

    tree_paths = deps_bundle._download_native_dependencies(tmp_path, tmp_path / "base_env")

    assert requested_versions == ["3.9", "3.10", "3.11", "3.13"]
    assert [path.name for path in tree_paths] == ["3_9", "3_10", "3_11", "3_13"]


def test_get_package_version_matches_pip_list_casing(monkeypatch):
    """NATIVE_DEPENDENCIES spells `pyyaml`, but `pip list` reports it as `PyYAML`."""
    output = b"Package  Version\n-------- -------\nPyYAML   6.0.3\nxxhash   3.6.0\n"
    monkeypatch.setattr(
        deps_bundle.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=output),
    )

    assert deps_bundle._get_package_version("pyyaml", Path("/unused")) == "6.0.3"
