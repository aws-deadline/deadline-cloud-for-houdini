# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import subprocess
import sys

from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional


ADAPTOR_ONLY_DEPENDENCIES = {"openjd-adaptor-runtime"}


class CPUArch(Enum):
    X86_64: str = "x86_64"
    ARM64: str = "arm64"


def get_project_dict(project_path: Optional[Path] = None) -> dict[str, Any]:
    if sys.version_info < (3, 11):
        with TemporaryDirectory() as toml_env:
            toml_install_pip_args = ["pip", "install", "--target", toml_env, "toml"]
            subprocess.run(toml_install_pip_args, check=True)
            sys.path.insert(0, toml_env)
            import toml
        mode = "r"
    else:
        import tomllib as toml

        mode = "rb"

    with open(str((project_path or get_git_root()) / "pyproject.toml"), mode) as pyproject_toml:
        return toml.load(pyproject_toml)


class Dependency:
    pip_requirement: str
    name: str

    def __init__(self, dep: str):
        self.pip_requirement = dep.strip().split(";", maxsplit=1)[0].replace(" ", "")
        self.name = dep.strip().split(" ", maxsplit=1)[0]

    def for_pip(self) -> str:
        return self.pip_requirement

    def __repr__(self) -> str:
        return self.for_pip()


def get_dependencies(pyproject_dict: dict[str, Any], exclude_adaptor_only=True) -> list[Dependency]:
    if "project" not in pyproject_dict:
        raise Exception("pyproject.toml is missing project section")
    if "dependencies" not in pyproject_dict["project"]:
        raise Exception("pyproject.toml is missing dependencies section")

    return [
        Dependency(dep_str)
        for dep_str in pyproject_dict["project"]["dependencies"]
        if exclude_adaptor_only or dep_str not in ADAPTOR_ONLY_DEPENDENCIES
    ]


def get_git_root() -> Path:
    return Path(__file__).parents[1].resolve()


def get_pip_platform(system_platform: str, cpu_arch: CPUArch = CPUArch.X86_64) -> str:
    if system_platform == "Windows":
        if cpu_arch is CPUArch.X86_64:
            return "win_amd64"
        if cpu_arch is CPUArch.ARM64:
            return "win_arm64"

    if system_platform == "Darwin":
        if cpu_arch is CPUArch.X86_64:
            return "macosx_10_9_x86_64"
        if cpu_arch is CPUArch.ARM64:
            return "macosx_11_0_arm64"

    if system_platform == "Linux":
        if cpu_arch is CPUArch.X86_64:
            return "manylinux_2_17_x86_64"
        if cpu_arch is CPUArch.ARM64:
            return "manylinux_2_17_aarch64"

    raise Exception(f"Unsupported platform/archicture: {system_platform}, {cpu_arch}")
