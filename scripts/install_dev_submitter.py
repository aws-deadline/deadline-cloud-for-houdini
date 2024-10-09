# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import argparse
import json
import os
import platform
import re
import subprocess
from pathlib import Path

from typing import Optional

from _project import get_git_root, get_dependencies, get_project_dict, get_pip_platform


SUBMITTER_PACKAGE_TEMPLATE = {
    "env": [],
    "hpath": "$DEADLINE_CLOUD_FOR_HOUDINI",
}


class HoudiniVersion:
    major: int
    minor: int
    patch: Optional[int]

    VERSION_REGEX = re.compile(r"^([0-9]+)\.([0-9]+)(?:\.([0-9]+))?")

    PYTHON_VERSIONS = {"19.5": "3.9", "20.0": "3.10", "20.5": "3.11"}

    def __init__(self, arg_version: Optional[str] = None):
        version = self._get_houdini_version(arg_version)
        match = self.VERSION_REGEX.match(version)
        if match is None:
            raise ValueError(f"Invalid version: {version}")
        self.major = int(match.group(1))
        self.minor = int(match.group(2))
        self.patch = int(match.group(3)) if match.group(3) else None

    def major_minor(self) -> str:
        return f"{self.major}.{self.minor}"

    def python_major_minor(self) -> str:
        major_minor = self.major_minor()
        if major_minor in self.PYTHON_VERSIONS:
            return self.PYTHON_VERSIONS[major_minor]
        raise ValueError(f"Unknown Houdini major minor version {major_minor}")

    @classmethod
    def _validate_version(cls, version: str) -> str:
        match = cls.VERSION_REGEX.match(version)
        if match is None:
            raise ValueError(f"Invalid version: {version}")
        return version

    @classmethod
    def _get_houdini_version(cls, arg: Optional[str]) -> str:
        if arg is not None:
            return cls._validate_version(arg)
        houdini_version_file = get_git_root() / "houdini_version.txt"
        if houdini_version_file.exists():
            with open(houdini_version_file, "r", encoding="utf-8") as f:
                return cls._validate_version(f.read().strip())
        return cls._validate_version(
            input("Please enter the Houdini version (Major.Minor[.Patch]): ")
        )


def _get_houdini_user_prefs_path(major_minor: str) -> Path:
    if platform.system() == "Windows":
        return Path.home() / "Documents" / f"houdini{major_minor}"
    elif platform.system() == "Darwin":
        return Path.home() / "Library" / "Preferences" / "houdini" / major_minor
    elif platform.system() == "Linux":
        return Path.home() / f"houdini{major_minor}"
    else:
        raise RuntimeError(f"Unsupported platform: {platform.system()}")


def _get_submitter_src_path() -> Path:
    return get_git_root() / "src" / "deadline" / "houdini_submitter"


def _resolve_dependencies(local_deps: list[Path]) -> dict[str, str]:
    project_dict = get_project_dict()
    local_dep_project_dicts = [get_project_dict(local_dep) for local_dep in local_deps]
    local_dep_names = set([local_dep["project"]["name"] for local_dep in local_dep_project_dicts])
    all_project_dicts = [*local_dep_project_dicts, project_dict]
    dependency_lists = [get_dependencies(project_dict) for project_dict in all_project_dicts]
    filtered_dependency_lists = [
        [dep for dep in dependency_list if dep.name not in local_dep_names]
        for dependency_list in dependency_lists
    ]
    flattened_dependency_list = [
        dep for dependency_list in filtered_dependency_lists for dep in dependency_list
    ]

    args = [
        "pipgrip",
        "--json",
        *[dep.for_pip() for dep in flattened_dependency_list],
    ]
    print(f"Running: {' '.join(args)}")
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _build_deps_env(destination: Path, python_version: str, local_deps: list[Path]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise Exception(f"{str(destination)} is not a directory")

    resolved_dependencies_dict = _resolve_dependencies(local_deps)
    resolved_dependencies = [
        f"{dep_name}=={resolved_version}"
        for dep_name, resolved_version in resolved_dependencies_dict.items()
    ]

    args = [
        "pip",
        "install",
        "--target",
        str(destination),
        "--platform",
        get_pip_platform(platform.system()),
        "--python-version",
        python_version,
        "--only-binary=:all:",
        *resolved_dependencies,
    ]
    subprocess.run(args, check=True)


def install_submitter_package(houdini_version_arg: Optional[str], local_deps: list[Path]) -> None:
    houdini_version = HoudiniVersion(houdini_version_arg)
    major_minor = houdini_version.major_minor()

    plugin_env_path = get_git_root() / "plugin_env"
    os.makedirs(plugin_env_path, exist_ok=True)
    _build_deps_env(
        plugin_env_path,
        houdini_version.python_major_minor(),
        local_deps,
    )

    submitter_package = SUBMITTER_PACKAGE_TEMPLATE.copy()
    submitter_package["env"].append({"DEADLINE_CLOUD_FOR_HOUDINI": str(_get_submitter_src_path())})
    python_path = os.pathsep.join(
        [
            str(_get_submitter_src_path() / "python"),
            *[str(dep.resolve() / "src") for dep in local_deps],
            str(plugin_env_path),
        ]
    )
    submitter_package["env"].append({"PYTHONPATH": python_path})

    user_prefs_path = _get_houdini_user_prefs_path(major_minor)
    packages_path = user_prefs_path / "packages"
    packages_path.mkdir(parents=True, exist_ok=True)
    submitter_package_path = packages_path / "deadline_submitter_for_houdini.json"

    with open(submitter_package_path, "w", encoding="utf-8") as f:
        json.dump(submitter_package, f, indent=4)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--houdini-version",
        help="Houdini version to install the submitter for",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--local-dep",
        help="Path to a repository containing a dependency for in-place install",
        action="append",
        type=str,
    )
    args = parser.parse_args()
    local_deps = [Path(dep) for dep in args.local_dep or []]

    install_submitter_package(args.houdini_version, local_deps)
