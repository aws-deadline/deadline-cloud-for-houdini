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


def _flag(block: str, flag: str) -> bool:
    """Whether a parm block carries a bare flag line, such as ``invisible`` or ``joinnext``."""
    return bool(re.search(rf"^\s*{flag}\s*$", block, re.M))


@pytest.mark.parametrize("parm", ["login", "logout"])
def test_the_login_and_logout_buttons_are_hidden(parm: str) -> None:
    """On macOS the login dialog can crash Houdini, so the node hides them as the ROP does."""
    assert _flag(_parms(NODE_SCRIPT)[parm], "invisible"), parm


@pytest.mark.parametrize("parm", ["settings", "update_queue_parameters", "submit"])
def test_settings_update_queue_parameters_and_submit_stay_visible(parm: str) -> None:
    """Without Settings on the node, the farm and queue only change through the config file."""
    assert not _flag(_parms(NODE_SCRIPT)[parm], "invisible"), parm


def _visible_rows() -> List[List[str]]:
    """The parm rows the artist sees, in file order: joined parms share a row, hidden ones drop out."""
    # A row ends at the first parm without ``joinnext``. Hidden parms are left out of the row.
    rows: List[List[str]] = [[]]
    for name, block in _parms(NODE_SCRIPT).items():
        if not _flag(block, "invisible"):
            rows[-1].append(name)
        if not _flag(block, "joinnext"):
            rows.append([])
    return [row for row in rows if row]


def test_settings_and_update_queue_parameters_share_one_row_after_farm_and_queue() -> None:
    """The two buttons sat on one row before, and hiding Login and Logout must not split them."""
    rows: List[List[str]] = _visible_rows()
    start: int = rows.index(["farm"])
    assert rows[start : start + 4] == [
        ["farm"],
        ["queue"],
        ["settings", "update_queue_parameters"],
        ["submit"],
    ]


def test_no_visible_parm_joins_a_hidden_one() -> None:
    """A visible parm joined to a hidden one would leave a gap on its row where the hidden one was."""
    # Walk the parms in file order, and check what each visible, joined parm joins.
    blocks: List[Tuple[str, str]] = list(_parms(NODE_SCRIPT).items())
    for (name, block), (next_name, next_block) in zip(blocks, blocks[1:]):
        if _flag(block, "joinnext") and not _flag(block, "invisible"):
            assert not _flag(next_block, "invisible"), (name, next_name)


#: The commands that change the farm and queue, since the node's Settings window has no picker.
_FARM_AND_QUEUE_COMMANDS: List[str] = [
    "deadline config set defaults.farm_id",
    "deadline config set defaults.queue_id",
]


@pytest.mark.parametrize(
    "path",
    [os.path.join(_NODE, "Help"), os.path.join(_SRC, "houdini_pdg", "README.md")],
    ids=["help", "readme"],
)
def test_the_docs_name_the_commands_that_change_the_farm_and_queue(path: str) -> None:
    """The workstation settings window has no farm or queue, so the docs must name what does."""
    with open(path, encoding="utf8") as f:
        text: str = " ".join(f.read().split())
    for command in _FARM_AND_QUEUE_COMMANDS:
        assert command in text, command
    assert "Update Queue Parameters" in text


@pytest.mark.parametrize(
    "path",
    [os.path.join(_NODE, "Help"), os.path.join(_SRC, "houdini_pdg", "README.md")],
    ids=["help", "readme"],
)
def test_the_docs_say_to_set_the_farm_region_after_the_farm(path: str) -> None:
    """Setting the farm alone looks it up in the profile's region, and the region is stored per farm."""
    # Read the text with its line breaks folded, then check the region command follows the farm one.
    with open(path, encoding="utf8") as f:
        text: str = " ".join(f.read().split())
    farm: int = text.index("deadline config set defaults.farm_id")
    region: int = text.find("deadline config set defaults.farm_region <region>")
    assert region > farm
    assert "another AWS Region" in text


def test_the_help_does_not_say_settings_changes_the_farm_or_queue() -> None:
    """Settings opens the workstation settings, which have no farm or queue to change."""
    # Read the Settings parm's own entry, up to the next parm's entry.
    help_text: str = _read(_NODE, "Help")
    start: int = help_text.index("#id: settings")
    end: int = help_text.index("#id:", start + 1)
    entry: str = help_text[start:end]
    assert "farm" not in entry and "queue" not in entry
    assert "click __Settings__" not in help_text
