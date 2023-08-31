# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import re
import shutil
import subprocess
import sys

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

SUPPORTED_PYTHON_VERSIONS = ["3.9", "3.10", "3.11"]
SUPPORTED_PLATFORMS = ["win_amd64", "manylinux2014_x86_64", "macosx_10_9_x86_64"]
NATIVE_DEPENDENCIES = ["xxhash"]


def _get_project_dict() -> dict[str, Any]:
    if sys.version_info < (3, 11):
        with TemporaryDirectory() as toml_env:
            toml_install_pip_args = ["pip", "install", "--target", toml_env, "toml"]
            subprocess.run(toml_install_pip_args, check=True)
            sys.path.insert(0, toml_env)
            import toml
    else:
        import libtoml as toml

    with open("pyproject.toml") as pyproject_toml:
        return toml.load(pyproject_toml)


def _get_dependencies(pyproject_dict: dict[str, Any]) -> list[str]:
    if "project" not in pyproject_dict:
        raise Exception("pyproject.toml is missing project section")
    if "dependencies" not in pyproject_dict["project"]:
        raise Exception("pyproject.toml is missing dependencies section")

    dependencies = pyproject_dict["project"]["dependencies"]
    deps_noopenjobio = filter(lambda dep: not dep.startswith("openjobio"), dependencies)
    return list(map(lambda dep: dep.replace(" ", ""), deps_noopenjobio))


def _get_package_version_regex(package: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(package)} *(.*)$")


def _get_package_version(package: str, install_path: Path) -> str:
    version_regex = _get_package_version_regex(package)
    pip_args = ["pip", "list", "--path", str(install_path)]
    output = subprocess.run(pip_args, check=True, capture_output=True).stdout.decode("utf-8")
    for line in output.split("\n"):
        match = version_regex.match(line)
        if match:
            return match.group(1)
    raise Exception(f"Could not find version for package {package}")


def _build_base_environment(working_directory: Path, dependencies: list[str]) -> Path:
    (working_directory / "base_env").mkdir()
    base_env_path = working_directory / "base_env"
    base_env_pip_args = [
        "pip",
        "install",
        "--target",
        str(base_env_path),
        "--only-binary=:all:",
        *dependencies,
    ]
    subprocess.run(base_env_pip_args, check=True)
    return base_env_path


def _download_native_dependencies(working_directory: Path, base_env: Path) -> list[Path]:
    versioned_native_dependencies = [
        f"{package_name}=={_get_package_version(package_name, base_env)}"
        for package_name in NATIVE_DEPENDENCIES
    ]
    native_dependency_paths = []
    for version in SUPPORTED_PYTHON_VERSIONS:
        for platform in SUPPORTED_PLATFORMS:
            native_dependency_path = (
                working_directory / "native" / f"{version.replace('.', '_')}_{platform}"
            )
            native_dependency_paths.append(native_dependency_path)
            native_dependency_path.mkdir(parents=True)
            native_dependency_pip_args = [
                "pip",
                "install",
                "--target",
                str(native_dependency_path),
                "--platform",
                platform,
                "--python-version",
                version,
                "--only-binary=:all:",
                *versioned_native_dependencies,
            ]
            subprocess.run(native_dependency_pip_args, check=True)
    return native_dependency_paths


def _copy_native_to_base_env(base_env: Path, native_dependency_paths: list[Path]) -> None:
    for native_dependency_path in native_dependency_paths:
        for file in native_dependency_path.rglob("*"):
            if file.is_file():
                relative = file.relative_to(native_dependency_path)
                in_base_env = base_env / relative
                if not in_base_env.exists():
                    in_base_env.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(str(file), str(in_base_env))


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


def _copy_zip_to_destination(zip_path: Path) -> None:
    dependency_bundle_dir = Path.cwd() / "dependency_bundle"
    dependency_bundle_dir.mkdir(exist_ok=True)
    zip_desntination = dependency_bundle_dir / zip_path.name
    if zip_desntination.exists():
        zip_desntination.unlink()
    shutil.copy(str(zip_path), str(zip_desntination))


def build_deps_bundle() -> None:
    with TemporaryDirectory() as working_directory:
        working_directory = Path(working_directory)
        project_dict = _get_project_dict()
        dependencies = _get_dependencies(project_dict)
        base_env = _build_base_environment(working_directory, dependencies)
        native_dependency_paths = _download_native_dependencies(working_directory, base_env)
        _copy_native_to_base_env(base_env, native_dependency_paths)
        zip_path = _get_zip_path(working_directory, project_dict)
        _zip_bundle(base_env, zip_path)
        print(list(working_directory.glob("*")))
        _copy_zip_to_destination(zip_path)


if __name__ == "__main__":
    build_deps_bundle()
