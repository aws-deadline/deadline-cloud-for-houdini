# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Fixtures for the PDG integ tests, which cook scenes on a Deadline Cloud farm.

Every test needs ``farm_queue``, which skips the test unless ``PDG_INTEG_FARM_ID`` and
``PDG_INTEG_QUEUE_ID`` are set. A plain ``hatch run integ:test`` doesn't set them, so the
PDG tests skip there, and ``scripts/run_pdg_integ_tests.py`` runs them.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from .farm import FARM_ID_VARIABLE, QUEUE_ID_VARIABLE, FarmQueue, farm_queue_from_env
from .farm import write_deadline_config
from .hython_env import REPO_ROOT, find_hython, hython_env, installed_version, plugin_env_path
from .scenes import SCENES_DIR_VARIABLE

# Asks hython for its Houdini version and its Python's major.minor, which also needs a license.
_VERSIONS_SCRIPT: str = (
    "import sys, hou; print(hou.applicationVersionString()); print('%d.%d' % sys.version_info[:2])"
)


@pytest.fixture(scope="session")
def farm_queue() -> FarmQueue:
    """The farm and queue the tests submit to. Skips every test when either is unset."""
    configured: Optional[FarmQueue] = farm_queue_from_env()
    if configured is None:
        pytest.skip(
            f"The PDG integ tests submit jobs, so they run only with {FARM_ID_VARIABLE} and "
            f"{QUEUE_ID_VARIABLE} set. Run them with scripts/run_pdg_integ_tests.py."
        )
    return configured


@pytest.fixture(scope="session")
def pdg_hython() -> Path:
    """hython, from ``HYTHON_EXECUTABLE`` or ``HOUDINI_VERSION``, as the other integ tests find it."""
    return find_hython(os.environ)


@pytest.fixture(scope="session")
def pdg_plugin_env(pdg_hython: Path) -> Path:
    """The Python dependencies hython loads the scheduler with, for hython's Houdini version.

    They are ``<repo>/plugin_env_<major.minor>``, built with the dev install's builder when
    the folder is missing or empty. Skips every test when hython can't start Houdini, for
    example without a license.
    """
    # Houdini's version names the folder, and its Python picks the wheels in it.
    versions: subprocess.CompletedProcess[str] = subprocess.run(
        [str(pdg_hython), "-c", _VERSIONS_SCRIPT], capture_output=True, text=True, check=False
    )
    if versions.returncode != 0:
        pytest.skip(f"hython cannot start Houdini:\n{versions.stdout}\n{versions.stderr}")
    houdini_version, python_version = versions.stdout.split()[-2:]
    plugin_env: Path = plugin_env_path(houdini_version)
    if plugin_env.is_dir() and any(plugin_env.iterdir()):
        return plugin_env

    # The dev install's scripts import each other by name from their own folder.
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from _project import CPUArch
    from install_dev_submitter import _build_deps_env

    _build_deps_env(plugin_env, python_version, CPUArch(platform.machine().lower()), [])
    return plugin_env


@pytest.fixture(scope="session")
def pdg_hython_env(
    farm_queue: FarmQueue, pdg_plugin_env: Path, tmp_path_factory: pytest.TempPathFactory
) -> Dict[str, str]:
    """The environment hython cooks in: this checkout's plugin, submitter and dependencies.

    The cook submits to ``farm_queue`` through a copy of the user's Deadline config.
    """
    config: Path = write_deadline_config(farm_queue, tmp_path_factory.mktemp("deadline_config"))
    return hython_env(os.environ, plugin_env=pdg_plugin_env, deadline_config=config)


@pytest.fixture(scope="session")
def worker_wheels(pdg_plugin_env: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A new folder with this checkout's wheel, and the client library's wheels hython uses.

    The workers install every wheel in the folder, so the folder holds nothing else. A
    build or download that fails fails the session.
    """
    wheels: Path = tmp_path_factory.mktemp("worker_wheels")
    # Inside `hatch run`, hatch would build with the test environment, which can't build.
    build_env: Dict[str, str] = {k: v for k, v in os.environ.items() if k != "HATCH_ENV_ACTIVE"}
    subprocess.run(
        ["hatch", "build", "-t", "wheel", str(wheels)], cwd=REPO_ROOT, env=build_env, check=True
    )

    # The workers get the deadline and openjd-adaptor-runtime versions hython cooked with.
    requirements: List[str] = [
        f"deadline=={installed_version(pdg_plugin_env, 'deadline')}",
        f"openjd-adaptor-runtime=={installed_version(pdg_plugin_env, 'openjd_adaptor_runtime')}",
    ]
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary", ":all:"]
        + ["--dest", str(wheels), *requirements],
        check=True,
    )
    return wheels


@pytest.fixture(scope="session")
def scenes_dir() -> Optional[Path]:
    """The folder ``PDG_TEST_SCENES_DIR`` names, or None. A test of an external scene needs it."""
    folder: str = os.environ.get(SCENES_DIR_VARIABLE, "")
    return Path(folder).expanduser() if folder else None
