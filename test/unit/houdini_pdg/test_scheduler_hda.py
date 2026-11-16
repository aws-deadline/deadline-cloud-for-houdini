# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler node's HDA sections, read as text: its parms, their defaults, and its callbacks."""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

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
    # A parm block starts at ``parm {`` and its name is the next ``name`` line. It ends at its
    # own closing brace, so a nested block, such as a menu script, stays inside it.
    for match in re.finditer(r"\bparm\s*\{\s*name\s+\"?(\w+)\"?", dialog_script):
        depth: int = 1
        end: int = match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(dialog_script[end], 0)
            end += 1
        blocks[match.group(1)] = dialog_script[match.end() : end - 1]
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


def _settings_folder() -> str:
    """The text of the Deadline Cloud Settings folder, ``folder1``, in the node's script."""
    start: int = NODE_SCRIPT.index('name    "folder1"')
    return NODE_SCRIPT[start : NODE_SCRIPT.index('name    "submit"', start)]


@pytest.mark.parametrize("parm", ["farm", "queue"])
def test_farm_and_queue_are_visible_menus_in_the_deadline_cloud_settings_folder(parm: str) -> None:
    """The ROP's update sets these parms by name, so the menus keep the names and stay strings."""
    # Each is a shown string parm in the folder, with a Python menu script and the node's callback.
    block: str = _parms(_settings_folder())[parm]
    assert re.search(r"type\s+string", block), parm
    assert not _flag(block, "invisible"), parm
    menu: Optional["re.Match[str]"] = re.search(r"\bmenu\s*\{(.*?)\n\s*\}", block, re.S)
    assert menu is not None, parm
    assert re.search(r"language\s+python", menu.group(1)), parm
    # The menu gets the parm's value, so it can name the saved farm or queue before its list loads.
    call: str = f"farm_and_queue_menus.list_menu_items('{parm}', kwargs['parm'].evalAsString())"
    assert call in menu.group(1), parm
    assert 'parmtag { "script_callback" "hou.phm().callback(kwargs)" }' in block, parm


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
def test_the_docs_name_the_farm_and_queue_commands_and_set_the_region_after_the_farm(
    path: str,
) -> None:
    """The workstation settings window has no farm or queue, so the docs must name what does.

    Setting the farm alone looks it up in the profile's region, and the region is stored per farm.
    """
    # Read the text with its line breaks folded, then check the region command follows the farm one.
    with open(path, encoding="utf8") as f:
        text: str = " ".join(f.read().split())
    for command in _FARM_AND_QUEUE_COMMANDS:
        assert command in text, command
    assert "Update Queue Parameters" in text
    farm: int = text.index("deadline config set defaults.farm_id")
    region: int = text.find("deadline config set defaults.farm_region <region>")
    assert region > farm
    assert "another AWS Region" in text


def _function(name: str) -> str:
    """The text of one function in the node's PythonModule."""
    # A function ends where the next top-level one starts, or at the end of the module.
    module: str = _read(_NODE, "PythonModule")
    start: int = module.index(f"def {name}(")
    end: int = module.find("\ndef ", start + 1)
    return module[start : end if end >= 0 else len(module)]


@pytest.mark.parametrize(
    "function, rop_call",
    [("callback", "rop_callback(kwargs)"), ("update_queue_parameters_callback", "rop(kwargs)")],
)
def test_the_farm_and_queue_ids_go_back_after_every_rop_update(
    function: str, rop_call: str
) -> None:
    """The ROP writes names or "<refreshing>" into the menus, which must store the IDs."""
    # The IDs go back in a ``finally``, so a failed update doesn't leave "<refreshing>" stored.
    body: str = _function(function)
    rop: int = body.index(rop_call)
    assert (
        rop < body.index("finally:", rop) < body.index("farm_and_queue_menus.show_saved_ids(", rop)
    )


def test_a_farm_or_queue_pick_goes_to_the_menus_and_then_to_the_queue_update() -> None:
    """A pick saves the choice and refreshes the node, and never runs the ROP's own button code."""
    # The pick branch builds the update, hands it to the menus, and returns before the ROP's call.
    body: str = _function("callback")
    pick: int = body.index('if name in ("farm", "queue"):')
    update: int = body.index("functools.partial(update_queue_parameters_callback, kwargs)", pick)
    call: int = body.index('farm_and_queue_menus.save_pick(kwargs["node"], name, update)', update)
    assert call < body.index("return", call) < body.index("rop_callback(kwargs)")


def test_the_buttons_read_the_lists_again_and_create_and_load_start_them() -> None:
    """After a login or a settings change, the lists must be fetched again, not kept."""
    assert "farm_and_queue_menus.load_lists(reload=True)" in _function("callback")
    wrapper: str = _function("update_queue_parameters_callback")
    assert 0 <= wrapper.index("farm_and_queue_menus.load_lists()") < wrapper.index("rop(kwargs)")
