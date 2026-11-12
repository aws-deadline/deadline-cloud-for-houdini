# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler node's HDA sections, read as text: its parms, their defaults, and its callbacks."""

from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

import pytest

_SRC: str = os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "deadline")
_NODE: str = os.path.join(
    _SRC,
    "houdini_pdg",
    "houdini_plugin",
    "otls",
    "deadline_cloud_scheduler.hda",
    "Top_1deadlinecloudscheduler",
)


def _read(folder: str, section: str) -> str:
    """One section of an expanded HDA."""
    with open(os.path.join(folder, section), encoding="utf8") as f:
        return f.read()


def _parms(dialog_script: str) -> Dict[str, str]:
    """Each parm's name, with the text of its block."""
    blocks: Dict[str, str] = {}
    # A parm block starts at ``parm {`` and its name is the next ``name`` line.
    for match in re.finditer(r"\bparm\s*\{\s*name\s+\"?(\w+)\"?(.*?)\n\s*\}", dialog_script, re.S):
        blocks[match.group(1)] = match.group(2)
    return blocks


@pytest.mark.parametrize(
    "function, rop_call",
    [
        ("def callback", "rop_callback(kwargs)"),
        ("def update_queue_parameters_callback", "rop(kwargs)"),
    ],
)
def test_each_rop_call_is_followed_by_moving_the_queue_folders_above_submit(
    function: str, rop_call: str
) -> None:
    """The ROP's queue update adds its folders below Submit, so each path that runs it moves them back."""
    # Read the one function, then check the move comes after the ROP's call.
    module: str = _read(_NODE, "PythonModule")
    start: int = module.index(function + "(")
    end: int = module.find("\ndef ", start + 1)
    body: str = module[start : end if end >= 0 else len(module)]
    move: str = 'move_queue_folders_above_submit(kwargs["node"])'
    assert 0 <= body.index(rop_call) < body.index(move)


def test_create_and_load_lay_the_tabs_out_before_the_rops_queue_update() -> None:
    """The ROP's update needs a login, so laying the tabs out first keeps them on a failed login."""
    # Both scripts call the module's callback, which lays the tabs out before the ROP runs.
    for section in ("OnCreated", "OnLoaded"):
        assert "hdaModule().update_queue_parameters_callback(kwargs)" in _read(_NODE, section)
    module: str = _read(_NODE, "PythonModule")
    body: str = module[module.index("def update_queue_parameters_callback(") :]
    layout: str = 'rebuild_layout_from_definition(kwargs["node"])'
    assert 0 <= body.index(layout) < body.index("rop(kwargs)")


NODE_SCRIPT: str = _read(_NODE, "DialogScript")


def test_submit_is_the_last_parm_of_shared_job_settings_and_nothing_follows_the_tabs() -> None:
    """A Submit below the tabs moves down with the tallest tab, so it sits in the first tab."""
    # Top-level entries are indented four spaces, and the tabs are the only ones besides help.
    top_level: List[str] = re.findall(r"\n    (\w+)\s*\{", NODE_SCRIPT)
    assert top_level == ["help", "group", "group", "group", "group"]
    # The first tab's direct entries are indented eight spaces, and Submit comes last.
    start: int = NODE_SCRIPT.index('\n    group {\n        name    "job_settings"\n')
    tab: str = NODE_SCRIPT[start : NODE_SCRIPT.index("\n    }\n", start)]
    entries: List[Tuple[str, str]] = re.findall(r'\n        (\w+)\s*\{\s*name\s+"(\w+)"', tab)
    assert entries == [("groupsimple", "folder0"), ("groupsimple", "folder1"), ("parm", "submit")]


@pytest.mark.parametrize(
    "parm", ["priority", "failed_tasks_limit", "task_retry_limit", "max_worker_count"]
)
def test_the_job_number_parms_are_plain_integer_fields(parm: str) -> None:
    """The artist types these counts, and a slider made them hard to set exactly."""
    # Each count is an integer parm whose slider is turned off.
    block: str = _parms(NODE_SCRIPT)[parm]
    assert re.search(r"type\s+integer", block)
    assert re.search(r'parmtag\s*\{\s*"sidefx::slider"\s+"none"\s*\}', block), parm
