# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import argparse
import json
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional

from _project import CPUArch, get_dependencies, get_git_root, get_pip_platform, get_project_dict
from pypanel import (
    get_rendered_path,
    get_submitter_panel_source_path,
    get_template_path,
    render_pypanel,
)

SUBMITTER_PACKAGE_TEMPLATE = {
    "env": [],
    "hpath": "$DEADLINE_CLOUD_FOR_HOUDINI",
}


class HoudiniVersion:
    major: int
    minor: int
    patch: Optional[int]

    VERSION_REGEX = re.compile(r"^([0-9]+)\.([0-9]+)(?:\.([0-9]+))?")

    PYTHON_VERSIONS = {
        "19.5": "3.9",
        "20.0": "3.10",
        "20.5": "3.11",
        "21.0": "3.11",
        "22.0": "3.13",
    }

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
            with open(houdini_version_file, encoding="utf-8") as f:
                return cls._validate_version(f.read().strip())
        return cls._validate_version(
            input("Please enter the Houdini version (Major.Minor[.Patch]): ")
        )


def _get_houdini_user_prefs_path(major_minor: str) -> Path:
    if platform.system() == "Windows":
        # Check if running in CodeBuild environment
        if os.environ.get("CODEBUILD_BUILD_ID"):
            return Path.home() / f"houdini{major_minor}"
        else:
            return Path.home() / "Documents" / f"houdini{major_minor}"
    elif platform.system() == "Darwin":
        return Path.home() / "Library" / "Preferences" / "houdini" / major_minor
    elif platform.system() == "Linux":
        return Path.home() / f"houdini{major_minor}"
    else:
        raise RuntimeError(f"Unsupported platform: {platform.system()}")


def _get_submitter_src_path() -> Path:
    return get_git_root() / "src" / "deadline" / "houdini_submitter"


# The pypanel template (src/deadline/houdini_submitter/python_panels/deadline_cloud.pypanel.template)
# carries a placeholder token inside its <script> CDATA which is replaced with the contents of
# submitter_panel.py at install time (see scripts/pypanel.py). This keeps the Python a normal,
# lintable/typable file while still deploying it embedded in the pypanel.
def _install_python_panel() -> None:
    """Render the Python panel from its template + submitter_panel.py, co-located with the template.

    Injects the current submitter_panel.py source into the committed pypanel template and writes
    the rendered ``deadline_cloud.pypanel`` next to it under the submitter source dir. That dir is
    the plugin's ``hpath`` (see SUBMITTER_PACKAGE_TEMPLATE), so Houdini discovers the panel via
    HOUDINI_PATH -- no copy into the Houdini user-prefs ``python_panels`` dir is needed. This
    mirrors the production installer, which ships the same rendered file under the install dir.
    """
    submitter_src = _get_submitter_src_path()
    rendered = render_pypanel(
        get_template_path(submitter_src),
        get_submitter_panel_source_path(submitter_src),
    )

    destination = get_rendered_path(submitter_src)
    destination.parent.mkdir(parents=True, exist_ok=True)

    print(f"Installing Houdini python panel to: {destination}")
    destination.write_text(rendered, encoding="utf-8")


def _resolve_dependencies(local_deps: list[Path], python_version: str) -> list[str]:
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

    return [dep.for_pip() for dep in flattened_dependency_list]


def _build_deps_env(
    destination: Path, python_version: str, cpu_arch: CPUArch, local_deps: list[Path]
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise Exception(f"{destination!s} is not a directory")

    resolved_dependencies = _resolve_dependencies(local_deps, python_version)

    pip_platform = get_pip_platform(platform.system(), cpu_arch)

    # Install dependencies from requirements file on Windows
    if platform.system() == "Windows":
        requirements_file = get_git_root() / "requirements-dcc-env.txt"
        if requirements_file.exists():
            args = [
                "pip",
                "install",
                "--upgrade",
                "--target",
                str(destination),
                "--platform",
                pip_platform,
                "--python-version",
                python_version,
                "--only-binary=:all:",
                "-r",
                str(requirements_file),
            ]
            print(f"Running: {' '.join(args)}")
            subprocess.run(args, check=True)

    # Install resolved dependencies
    args = [
        "pip",
        "install",
        "--upgrade",
        "--target",
        str(destination),
        "--platform",
        pip_platform,
        "--python-version",
        python_version,
        "--only-binary=:all:",
        *resolved_dependencies,
    ]
    print(f"Running: {' '.join(args)}")
    subprocess.run(args, check=True)


def install_submitter_package(
    houdini_version_arg: Optional[str],
    cpu_arch: CPUArch,
    local_deps: list[Path],
) -> None:
    houdini_version = HoudiniVersion(houdini_version_arg)
    major_minor = houdini_version.major_minor()

    plugin_env_suffix = f"_{major_minor}"
    plugin_env_path = get_git_root() / f"plugin_env{plugin_env_suffix}"

    os.makedirs(plugin_env_path, exist_ok=True)
    _build_deps_env(
        plugin_env_path,
        houdini_version.python_major_minor(),
        cpu_arch,
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

    print(f"Installing Houdini plugin to: {submitter_package_path}")
    with open(submitter_package_path, "w", encoding="utf-8") as f:
        json.dump(submitter_package, f, indent=4)

    _install_python_panel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--houdini-version",
        help="Houdini version to install the submitter for",
        type=str,
        default=None,
    )
    cpu_arch_choices = set(e.value for e in CPUArch)
    parser.add_argument(
        "--cpu-arch",
        help="Architecture for python's native deps, should match Houdini's target archicture",
        type=str,
        default=(
            platform.machine().lower() if platform.machine().lower() in cpu_arch_choices else None
        ),
        choices=cpu_arch_choices,
    )
    parser.add_argument(
        "--local-dep",
        help="Path to a repository containing a dependency for in-place install",
        action="append",
        type=str,
    )
    args = parser.parse_args()
    cpu_arch = CPUArch(args.cpu_arch)
    local_deps = [Path(dep) for dep in args.local_dep or []]

    install_submitter_package(args.houdini_version, cpu_arch, local_deps)
