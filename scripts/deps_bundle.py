# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import re
import shutil
import subprocess

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from _project import get_project_dict, get_dependencies, Dependency

SUPPORTED_PYTHON_VERSIONS = ["3.9", "3.10", "3.11"]
SUPPORTED_PLATFORMS = ["Windows", "Linux", "Darwin"]
# Packages with compiled extension modules, fetched once per supported Python version so the
# bundle carries a loadable artifact for each interpreter.
#
# awscrt is here because its wheels are not uniformly abi3: Python 3.9 and 3.10 get
# _awscrt.cpython-<tag>-<platform>.so while 3.11+ get _awscrt.abi3.so. Resolving it only in
# the base environment would ship whichever the build host produced, so any Houdini whose
# interpreter that single artifact does not cover would fail to import awscrt and AWS
# Console sign-in would break there.
# pyyaml is here because it ships a version-specific `_yaml` extension module: resolved only
# in the base environment it lands built for a single interpreter, and pyyaml hides that by
# falling back to its pure-Python parser on the other two.
NATIVE_DEPENDENCIES = ["xxhash", "psutil", "awscrt", "pyyaml"]


def _get_package_version_regex(package: str) -> re.Pattern:
    # Case-insensitive because `pip list` prints the distribution's own casing, which need not
    # match how the requirement is spelled -- `pyyaml` is reported as `PyYAML`. The required
    # whitespace keeps a prefix sibling like `pyyaml-env-tag` from matching.
    return re.compile(rf"^{re.escape(package)}\s+(\S+)\s*$", re.IGNORECASE)


def _get_package_version(package: str, install_path: Path) -> str:
    version_regex = _get_package_version_regex(package)
    pip_args = ["pip", "list", "--path", str(install_path)]
    output = subprocess.run(pip_args, check=True, capture_output=True).stdout.decode("utf-8")
    for line in output.split("\n"):
        match = version_regex.match(line)
        if match:
            return match.group(1)
    raise Exception(f"Could not find version for package {package}")


def _add_console_extra(requirement: str) -> str:
    """Add deadline's `console` extra to a requirement string, preserving its specifier."""
    match = re.fullmatch(
        r"(?P<name>[A-Za-z0-9._-]+)(?:\[(?P<extras>[^\]]*)\])?(?P<spec>.*)", requirement
    )
    if not match or match.group("name").lower() != "deadline":
        return requirement
    extras = [extra for extra in (match.group("extras") or "").split(",") if extra]
    if "console" not in extras:
        extras.append("console")
    return f"{match.group('name')}[{','.join(extras)}]{match.group('spec')}"


def _build_base_environment(working_directory: Path, dependencies: list[Dependency]) -> Path:
    (working_directory / "base_env").mkdir()
    base_env_path = working_directory / "base_env"
    # The bundle is the submitter, which needs AWS Console sign-in. The console extra is
    # requested here rather than declared in project.dependencies, because those are also
    # resolved into the adaptor package, where a compiled awscrt wheel is both unusable and
    # unavailable for one of the platform tags that build targets (see pyproject.toml).
    #
    # Requesting the extra rather than installing awscrt directly means the bundle tracks
    # whatever the extra actually requires -- notably a botocore floor, since the console
    # login provider lives in botocore, not in deadline -- and takes awscrt from the exact
    # version botocore's crt extra pins, rather than resolving it independently and drifting.
    #
    # That extra's botocore floor interacts with the boto3/botocore<1.43 cap in the
    # constraints file below: deadline 0.60.x's console extra needs botocore>=1.42.89,
    # which sits inside the cap, and every botocore in that window pins awscrt==0.31.2.
    # There is no quiet-degradation path here: awscrt only ever arrives as botocore's
    # exact ``==`` pin, never as a range pip could walk back through, so if a future
    # deadline raised the floor past the cap, pip would fail this build loudly with
    # ResolutionImpossible rather than resolve an older awscrt.
    dependencies_for_pip = [_add_console_extra(d.for_pip()) for d in dependencies]

    # Write a constraints file to keep transitive dependencies compatible with the oldest
    # supported Python (3.9). Several packages use PEP 604 type unions (X | Y) which are syntax
    # errors on Python 3.9 despite metadata sometimes claiming >=3.9 support:
    #   - urllib3 2.x uses `bytes | str` syntax
    #   - boto3/botocore 1.43+ use `str | None` syntax (Requires-Python correctly says >=3.10,
    #     but pip running on 3.13 resolves them anyway since we don't pass --python-version)
    constraints_path = working_directory / "constraints.txt"
    constraints_path.write_text("urllib3<2\n" "boto3<1.43\n" "botocore<1.43\n")

    base_env_pip_args = [
        "pip",
        "install",
        "--target",
        str(base_env_path),
        "--constraint",
        str(constraints_path),
        "--only-binary=:all:",
        *dependencies_for_pip,
    ]
    subprocess.run(base_env_pip_args, check=True)
    return base_env_path


def _python_version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _download_native_dependencies(working_directory: Path, base_env: Path) -> list[Path]:
    versioned_native_dependencies = [
        f"{package_name}=={_get_package_version(package_name, base_env)}"
        for package_name in NATIVE_DEPENDENCIES
    ]
    native_dependency_paths = []
    # Ascending order is load-bearing: _copy_native_to_base_env resolves a filename
    # collision in favour of the tree it sees first.
    for version in sorted(SUPPORTED_PYTHON_VERSIONS, key=_python_version_key):
        native_dependency_path = working_directory / "native" / f"{version.replace('.', '_')}"
        native_dependency_paths.append(native_dependency_path)
        native_dependency_path.mkdir(parents=True)
        native_dependency_pip_args = [
            "pip",
            "install",
            "--target",
            str(native_dependency_path),
            "--python-version",
            version,
            "--only-binary=:all:",
            # These trees exist only for their compiled artifacts, and they overwrite the
            # base environment during the merge. Without --no-deps each tree would carry the
            # packages' full transitive closures, resolved independently of the base
            # environment's, and clobber whatever it had resolved for anything they share.
            # Today none of NATIVE_DEPENDENCIES has runtime dependencies, but that is a
            # property of the current graph, not of this code.
            "--no-deps",
            *versioned_native_dependencies,
        ]
        subprocess.run(native_dependency_pip_args, check=True)
    return native_dependency_paths


def _copy_native_to_base_env(base_env: Path, native_dependency_paths: list[Path]) -> None:
    """Flatten the per-version native trees into the bundle, lowest version first.

    ``native_dependency_paths`` is ordered by ascending Python version and the first tree
    to supply a path wins, overwriting the base environment. The base environment resolved
    these packages for whatever interpreter the build host happens to run, which need not
    be a version the bundle targets, so it must not decide which artifact ships.

    Which artifacts survive follows from how the wheels name their extension modules, so no
    rule is needed per package. A version-specific name is unique per version and so cannot
    collide: ``xxhash`` ships one wheel per version and every interpreter keeps its own
    ``_xxhash.cpython-<tag>-<platform>.so``, and ``pyyaml`` is the same case, one wheel per
    version installing ``yaml/_yaml.cpython-<tag>-<platform>.so``; ``awscrt``'s wheels for
    Python 3.9 and 3.10 are version-specific too. An abi3 name is the same for every
    version and so collides, and there the two cases differ. ``psutil`` publishes a single
    abi3 wheel that serves all of them, so every tree holds identical bytes and the
    collision is a no-op. ``awscrt`` publishes a separate abi3 wheel per Python from 3.11
    up, each installing ``_awscrt.abi3.so``, so the copies differ and only one can ship;
    abi3 is forward compatible, which makes the one built for the lowest supported abi3
    Python the only copy that loads on all of them, and taking the first tree is what
    keeps it.
    """
    copied: set[Path] = set()
    for native_dependency_path in native_dependency_paths:
        for file in native_dependency_path.rglob("*"):
            if file.is_file():
                relative = file.relative_to(native_dependency_path)
                if relative in copied:
                    continue
                in_base_env = base_env / relative
                in_base_env.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(str(file), str(in_base_env))
                copied.add(relative)


def _get_zip_path(working_directory: Path, project_dict: dict[str, Any]) -> Path:
    if "project" not in project_dict:
        raise Exception("pyproject.toml is missing project section")
    if "name" not in project_dict["project"]:
        raise Exception("pyproject.toml is missing name section")
    transformed_project_name = (
        f"{project_dict['project']['name'].replace('-', '_')}_submitter-deps.zip"
    )
    return working_directory / transformed_project_name


def _zip_bundle(base_env: Path, zip_path: Path) -> None:
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(base_env))


def _copy_zip_to_destination(zip_path: Path) -> Path:
    dependency_bundle_dir = Path.cwd() / "dependency_bundle"
    dependency_bundle_dir.mkdir(exist_ok=True)
    zip_destination = dependency_bundle_dir / zip_path.name
    if zip_destination.exists():
        zip_destination.unlink()
    shutil.copy(str(zip_path), str(zip_destination))

    return zip_destination


def build_deps_bundle() -> None:
    with TemporaryDirectory() as working_directory:
        working_directory = Path(working_directory)
        project_dict = get_project_dict()
        dependencies: list[Dependency] = get_dependencies(project_dict)
        deps_noopenjd: list[Dependency] = filter(
            lambda dep: not dep.name.startswith("openjd"), dependencies
        )
        base_env = _build_base_environment(working_directory, deps_noopenjd)
        native_dependency_paths = _download_native_dependencies(working_directory, base_env)
        _copy_native_to_base_env(base_env, native_dependency_paths)
        zip_path = _get_zip_path(working_directory, project_dict)
        _zip_bundle(base_env, zip_path)
        print(list(working_directory.glob("*")))
        _copy_zip_to_destination(zip_path)


if __name__ == "__main__":
    build_deps_bundle()
