# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Which hython the PDG integ tests cook with, and the environment it cooks in.

hython loads this checkout's scheduler plugin, submitter and Python dependencies, and
nothing from the user's own Houdini packages.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Mapping

#: The repo's root, which holds the plugin, the submitter and the dev install's scripts.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]


def find_hython(env: Mapping[str, str], platform: str = sys.platform) -> Path:
    """hython, from ``HYTHON_EXECUTABLE`` or ``HOUDINI_VERSION``, as the other integ tests find it.

    Raises ValueError when neither is set.
    """
    # A named hython wins. Otherwise Houdini's default install folder for the version.
    if env.get("HYTHON_EXECUTABLE"):
        return Path(env["HYTHON_EXECUTABLE"])
    version: str = env.get("HOUDINI_VERSION", "")
    if not version:
        raise ValueError("Either HYTHON_EXECUTABLE or HOUDINI_VERSION must be set")
    if platform == "win32":
        return Path(f"C:\\Tools\\houdini-{version}\\bin\\hython.exe")
    if platform == "darwin":
        return Path(
            f"/Applications/Houdini/Houdini{version}/Frameworks/Houdini.framework/Versions/"
            "Current/Resources/bin/hython"
        )
    return Path(f"/opt/hfs{version}/bin/hython")


def plugin_env_path(houdini_version: str) -> Path:
    """``<repo>/plugin_env_<major.minor>``, the dev install's dependency folder for this Houdini."""
    major_minor: str = ".".join(houdini_version.split(".")[:2])
    return REPO_ROOT / f"plugin_env_{major_minor}"


def hython_env(
    base_env: Mapping[str, str], *, plugin_env: Path, deadline_config: Path
) -> Dict[str, str]:
    """``base_env`` with what hython needs to cook with this checkout and the given Deadline config.

    Houdini loads the scheduler and submitter from HOUDINI_PATH, and their Python code and
    dependencies from PYTHONPATH, which replaces any the caller had. The user's Houdini
    packages are skipped, so a dev install of another checkout isn't loaded instead.
    """
    houdini_path: List[str] = [
        str(REPO_ROOT / "src" / "deadline" / "houdini_pdg" / "houdini_plugin"),
        str(REPO_ROOT / "src" / "deadline" / "houdini_submitter"),
        # Houdini's own folders, which "&" stands for.
        "&",
    ]
    python_path: List[str] = [
        str(REPO_ROOT / "src"),
        str(REPO_ROOT / "src" / "deadline" / "houdini_submitter" / "python"),
        str(plugin_env),
    ]
    return {
        **base_env,
        "HOUDINI_PACKAGE_SKIP": "1",
        "HOUDINI_PATH": os.pathsep.join(houdini_path),
        "PYTHONPATH": os.pathsep.join(python_path),
        "DEADLINE_CONFIG_FILE_PATH": str(deadline_config),
    }


def installed_version(plugin_env: Path, distribution: str) -> str:
    """The version of a distribution installed in ``plugin_env``, from its dist-info folder.

    ``distribution`` is the folder's spelling, such as ``openjd_adaptor_runtime``. Raises
    FileNotFoundError when it isn't installed there.
    """
    found: List[Path] = sorted(plugin_env.glob(f"{distribution}-*.dist-info"))
    if not found:
        raise FileNotFoundError(f"{plugin_env} has no {distribution}. Delete it to rebuild it.")
    return found[0].name[len(distribution) + 1 : -len(".dist-info")]
