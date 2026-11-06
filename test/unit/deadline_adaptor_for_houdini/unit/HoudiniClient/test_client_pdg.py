# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for how the Houdini client starts a PDG session."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import List

from .mock_hou import hou_module as hou  # noqa:F401

import openjd.adaptor_runtime_client

import deadline.houdini_adaptor
from deadline.houdini_adaptor.HoudiniClient.houdini_client import HoudiniClient
from deadline.houdini_adaptor.pdg_actions import EXIT_CODE_MARKER


def test_the_client_adds_the_pdg_action_only_when_a_pdg_session_starts() -> None:
    """A ROP session must keep exactly its old actions, so the PDG one arrives only with the session action."""
    # The client connects only when it polls, so building one needs no server.
    client: HoudiniClient = HoudiniClient(server_path="unused")
    assert "pdg_session" in client.actions
    assert "pdg_run_work_item" not in client.actions
    client.actions["pdg_session"]({})
    assert "pdg_run_work_item" in client.actions


# Runs in a child Python that sees only the two namespace folders, as Houdini's Python
# does. It starts a PDG session, runs one item, and lists what it imported.
_CLIENT_SESSION: str = """
import gzip, json, os, sys, types
hou = types.ModuleType("hou")
hou.applicationVersionString = lambda: "21.0"
sys.modules["hou"] = hou
if sys.platform == "win32":
    # pywin32's extension modules need the DLLs in pywin32_system32. Its .pth bootstrap, which -S
    # skips, adds that folder to the DLL search, so do the same before any pywin32 import.
    dll_folders = [os.add_dll_directory(p) for p in sys.path
                   if os.path.basename(p.rstrip("/\\\\")) == "pywin32_system32"]
from houdini_adaptor.HoudiniClient.houdini_client import HoudiniClient
holder = types.SimpleNamespace(actions={})
HoudiniClient._start_pdg_session(holder, {})
pdg_dir = sys.argv[1]
pdg_temp = os.path.join(pdg_dir, "pdgtemp")
os.makedirs(os.path.join(pdg_temp, "data"))
item = {"workitem": {"name": "item", "id": 1, "index": 0, "command": sys.argv[2]}}
with gzip.open(os.path.join(pdg_temp, "data", "item.json.gz"), "wt") as f:
    json.dump(item, f)
args = {"item_name": "item", "pdg_dir": pdg_dir, "pdg_temp": pdg_temp,
        "scene_file": "", "record_outputs": True}
holder.actions["pdg_run_work_item"](args)
print("imported " + json.dumps(sorted(sys.modules)))
"""


def _pywin32_folders() -> List[str]:
    """pywin32's folders on Windows, which its .pth files add and ``-S`` skips. None elsewhere.

    openjd's runtime client imports pywin32 on Windows, so the child can't start a session without them.
    """
    if sys.platform != "win32":
        return []
    folders: List[str] = []
    # The running interpreter's site-packages, where pip puts pywin32.
    for root in dict.fromkeys(sysconfig.get_paths()[k] for k in ("purelib", "platlib")):
        for name in ("win32", os.path.join("win32", "lib"), "pywin32_system32"):
            folder: str = os.path.join(root, name)
            if os.path.isdir(folder):
                folders.append(folder)
    return folders


# Modules that reach AWS or need compiled wheels, which Houdini's Python can't load.
_NOT_IN_HOUDINI: List[str] = (
    "boto3 botocore client job_attachments houdini_pdg.task_storage adaptor_runtime"
    " jsonschema yaml pydantic"
).split()


def test_the_client_session_runs_an_item_with_only_the_namespace_folders_on_the_path(
    tmp_path: Path,
) -> None:
    """Houdini's Python sees only the namespace folders and none of the adaptor's wheels.

    So the session must start by short names, run an item, and import nothing that reaches AWS.
    """
    namespaces: List[str] = [
        os.path.dirname(os.path.dirname(openjd.adaptor_runtime_client.__file__)),
        os.path.dirname(os.path.dirname(deadline.houdini_adaptor.__file__)),
        *_pywin32_folders(),
    ]
    script: Path = tmp_path / "session.py"
    script.write_text(_CLIENT_SESSION)
    # A raw string, so a Windows work folder's backslashes print as written.
    command: str = (
        f'"{sys.executable}" -c "print(r\'PDG_RESULT: 1;-1;__PDG_DIR__/a.exr;file/image;0\')"'
    )
    result: subprocess.CompletedProcess = subprocess.run(
        [sys.executable, "-S", str(script), str(tmp_path / "pdg"), command],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(namespaces)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{EXIT_CODE_MARKER}0" in result.stdout
    # The item ran, and handed its outputs over for the adaptor to publish.
    record: Path = tmp_path / "pdg" / "pdgtemp" / "data" / "item.outputs.json"
    assert json.loads(record.read_text()) == {
        "outputs": [{"path": "__PDG_DIR__/a.exr", "tag": "file/image"}]
    }
    (listing,) = [line for line in result.stdout.splitlines() if line.startswith("imported ")]
    modules: List[str] = json.loads(listing[len("imported ") :])
    assert not [m for m in modules if m.split(".")[0] in ("deadline", "openjd")]
    assert not [m for m in modules for b in _NOT_IN_HOUDINI if m == b or m.startswith(b + ".")]
