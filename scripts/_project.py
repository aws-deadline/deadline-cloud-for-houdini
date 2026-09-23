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
    AMD64: str = "amd64"


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


def get_uv_platform(system_platform: str, cpu_arch: CPUArch = CPUArch.X86_64) -> str:
    """Target triple for uv's `--python-platform`, used when resolving for another Python.

    uv takes a target triple, not a wheel platform tag, and derives the compatible wheel tags
    from it rather than letting the caller state them.

    On macOS that means the minimum OS version is uv's choice and cannot be set: both
    `*-apple-darwin` triples accept `macosx_12_0` wheels (verified against
    PySide6-Essentials 6.8.3, whose only macOS wheel is `macosx_12_0_universal2`). That is a
    higher floor than the `macosx_10_9_x86_64` / `macosx_11_0_arm64` tags this repo used with
    pip, so resolving for a Mac older than 12.0 can now yield wheels that install and then
    fail to import. uv exposes no deployment-target flag to pin this back down.

    On Linux `manylinux2014` is the older alias of the `manylinux_2_17` tags used previously --
    the same glibc 2.17 floor, so no change in practice.
    """
    if system_platform == "Windows":
        if cpu_arch in [CPUArch.AMD64, CPUArch.X86_64]:
            return "x86_64-pc-windows-msvc"
        if cpu_arch is CPUArch.ARM64:
            return "aarch64-pc-windows-msvc"

    if system_platform == "Darwin":
        if cpu_arch is CPUArch.X86_64:
            return "x86_64-apple-darwin"
        if cpu_arch is CPUArch.ARM64:
            return "aarch64-apple-darwin"

    if system_platform == "Linux":
        if cpu_arch is CPUArch.X86_64:
            return "x86_64-manylinux2014"
        if cpu_arch is CPUArch.ARM64:
            return "aarch64-manylinux2014"

    raise Exception(f"Unsupported platform/archicture: {system_platform}, {cpu_arch}")
