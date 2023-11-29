# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import subprocess
import sys

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional


ADAPTOR_ONLY_DEPENDENCIES = {"openjd-adaptor-runtime"}


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
    name: str
    operator: Optional[str]
    version: Optional[str]

    def __init__(self, dep: str):
        components = dep.split(" ")
        self.name = components[0]
        if len(components) > 2:
            self.operator = components[1]
            self.version = components[2]
        else:
            self.operator = None
            self.version = None

    def for_pip(self) -> str:
        if self.operator is not None and self.version is not None:
            return f"{self.name}{self.operator}{self.version}"
        return self.name

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


def get_pip_platform(system_platform: str) -> str:
    if system_platform == "Windows":
        return "win_amd64"
    elif system_platform == "Darwin":
        return "macosx_10_9_x86_64"
    elif system_platform == "Linux":
        return "manylinux2014_x86_64"
    else:
        raise Exception(f"Unsupported platform: {system_platform}")
