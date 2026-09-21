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
# Packages with compiled extension modules, fetched once per version in
# SUPPORTED_PYTHON_VERSIONS so the bundle carries a loadable artifact for each.
#
# awscrt: wheels are not uniformly abi3 -- 3.9/3.10 get a version-specific
# _awscrt.cpython-<tag>-<platform>.so, 3.11+ get the shared _awscrt.abi3.so.
# pyyaml: ships a version-specific `_yaml` extension module and silently falls back to a
# pure-Python parser when the artifact doesn't match, masking the same failure mode.
#
# Gap predating this list: it stops at 3.11, but Houdini 22.0 embeds 3.13 (see
# scripts/install_dev_submitter.py), which only the base environment covers. Extending it
# changes the shipped bundle, so that is its own change.
NATIVE_DEPENDENCIES = ["xxhash", "psutil", "awscrt", "pyyaml"]


def _get_package_version_regex(package: str) -> re.Pattern:
    # Case-insensitive: `pip list` prints the distribution's own casing (`pyyaml` -> `PyYAML`).
    # The required whitespace keeps a prefix sibling like `pyyaml-env-tag` from matching.
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


_REQUIREMENT_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9._-]+)(?:\[(?P<extras>[^\]]*)\])?(?P<spec>.*)"
)


def _parse_requirement(requirement: str) -> tuple[str, list[str], str] | None:
    """Split a requirement into (name, extras, specifier), or None if it doesn't match the
    `name[extras]spec` shape.
    """
    match = _REQUIREMENT_PATTERN.fullmatch(requirement)
    if not match:
        return None
    extras = [extra for extra in (match.group("extras") or "").split(",") if extra]
    return match.group("name"), extras, match.group("spec")


def _add_console_extra(requirement: str) -> str:
    """Add deadline's `console` extra to a requirement string, preserving its specifier."""
    parsed = _parse_requirement(requirement)
    if not parsed or parsed[0].lower() != "deadline":
        return requirement
    name, extras, spec = parsed
    if "console" not in extras:
        extras = [*extras, "console"]
    return f"{name}[{','.join(extras)}]{spec}"


def _requests_console_extra(requirement: str) -> bool:
    """Whether a requirement is a `deadline` requirement whose extras include `console`."""
    parsed = _parse_requirement(requirement)
    return parsed is not None and parsed[0].lower() == "deadline" and "console" in parsed[1]


def _verify_console_resolution(base_env: Path) -> None:
    """Fail the build if the resolved closure carries no awscrt.

    This is the one console-extra failure pip reports success for. A resolve that cannot
    satisfy the `deadline` specifier exits non-zero on its own, and every version in that
    range requests the same botocore floor, so there is no version pip can silently settle
    on that drops it. What pip does accept is a closure with no awscrt at all -- it arrives
    only through botocore's `crt` extra, which only deadline's `console` extra requests, so
    losing the extra still installs cleanly and ships a bundle whose sign-in fails a
    pre-flight check. NATIVE_DEPENDENCIES keeping awscrt is load-bearing for the same reason.
    """
    _get_package_version("awscrt", base_env)


def _build_base_environment(working_directory: Path, dependencies: list[Dependency]) -> Path:
    (working_directory / "base_env").mkdir()
    base_env_path = working_directory / "base_env"
    # Requested here (not in project.dependencies) because those also resolve into the
    # adaptor package under a platform tag with no usable awscrt wheel (see pyproject.toml).
    # Requesting the extra rather than pinning awscrt directly keeps the bundle on the
    # botocore floor the extra needs (the console login provider lives in botocore) and takes
    # awscrt from the exact version botocore's crt extra pins. That floor (>=1.42.89 for
    # deadline 0.60.x) has to stay inside the botocore cap in the constraints file below; if it
    # stops fitting, pip reports ResolutionImpossible and exits non-zero.
    dependencies_for_pip = [_add_console_extra(d.for_pip()) for d in dependencies]
    if not any(_requests_console_extra(d) for d in dependencies_for_pip):
        # Checks that something requests the extra, not that _add_console_extra changed
        # anything: it is idempotent, so a `deadline[console]` already in project.dependencies
        # is valid input this guard must accept.
        raise Exception(
            "no dependency requests deadline's `console` extra after _add_console_extra; "
            f"expected a requirement on `deadline` in: {dependencies_for_pip}"
        )

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
    _verify_console_resolution(base_env_path)
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
            # These trees exist only for their compiled artifacts and overwrite the base
            # environment during the merge; --no-deps keeps each tree from resolving (and
            # clobbering) full dependency closures independently.
            "--no-deps",
            *versioned_native_dependencies,
        ]
        subprocess.run(native_dependency_pip_args, check=True)
    return native_dependency_paths


def _copy_native_to_base_env(base_env: Path, native_dependency_paths: list[Path]) -> None:
    """Flatten the per-version native trees into the bundle, lowest version first.

    ``native_dependency_paths`` is ascending by Python version; the first tree to supply a
    path wins a filename collision, overwriting the base environment -- which resolved these
    packages for the build host's interpreter, not necessarily one the bundle targets.

    A version-specific name (xxhash's ``_xxhash.cpython-<tag>-*``, pyyaml's
    ``yaml/_yaml.cpython-<tag>-*``, awscrt's 3.9/3.10 wheels) is unique per version and never
    collides. An abi3 name is identical across versions and always collides: psutil's single
    abi3 wheel makes that a no-op, while awscrt publishes one abi3 wheel per Python from 3.11
    up. abi3 is forward-compatible only, so taking the first (lowest-version) tree is what
    keeps the one copy every supported interpreter can load.
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
        # A list, not a lazy filter: anything downstream that reads this twice -- the pip
        # argument list and the diagnostic naming it -- would see the second pass empty.
        deps_noopenjd: list[Dependency] = [
            dep for dep in dependencies if not dep.name.startswith("openjd")
        ]
        base_env = _build_base_environment(working_directory, deps_noopenjd)
        native_dependency_paths = _download_native_dependencies(working_directory, base_env)
        _copy_native_to_base_env(base_env, native_dependency_paths)
        zip_path = _get_zip_path(working_directory, project_dict)
        _zip_bundle(base_env, zip_path)
        print(list(working_directory.glob("*")))
        _copy_zip_to_destination(zip_path)


if __name__ == "__main__":
    build_deps_bundle()
