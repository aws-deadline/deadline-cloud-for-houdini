# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler node's tabs, rebuilt from its definition when a scene loads or a node is made."""

from __future__ import annotations

import copy
import functools
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pytest

# The fakes come first, because they install the fake ``hou`` the host module imports.
from .houdini_fakes import FakeHou, install_hou
from .test_scheduler_hda import NODE_SCRIPT, _flag, _parms

from deadline.houdini_pdg.houdini.host import (
    move_queue_folders_above_submit,
    rebuild_layout_from_definition,
)


class FakeTemplate:
    """A ``hou.ParmTemplate``: a field, or a folder with the templates inside it."""

    def __init__(
        self,
        name: str,
        label: str = "",
        children: Optional[List["FakeTemplate"]] = None,
        hidden: bool = False,
        menu_script: str = "",
    ) -> None:
        self._name: str = name
        self._label: str = label or name
        self.children: Optional[List[FakeTemplate]] = children
        self._hidden: bool = hidden
        self._menu_script: str = menu_script

    def name(self) -> str:
        return self._name

    def label(self) -> str:
        return self._label

    def isHidden(self) -> bool:
        return self._hidden

    def itemGeneratorScript(self) -> str:
        return self._menu_script

    def type(self) -> str:
        return "Folder" if self.children is not None else "String"

    def parmTemplates(self) -> Tuple["FakeTemplate", ...]:
        return tuple(self.children or ())

    def names(self) -> List[str]:
        """This template's name and every name inside it."""
        return [self._name] + [name for child in self.parmTemplates() for name in child.names()]


def _find_folder(templates: Sequence[FakeTemplate], label: str) -> Optional[FakeTemplate]:
    """The first folder with ``label``, searched depth first, as ``findFolder`` searches."""
    for template in templates:
        if template.children is None:
            continue
        if template.label() == label:
            return template
        found: Optional[FakeTemplate] = _find_folder(template.children, label)
        if found is not None:
            return found
    return None


def _siblings(templates: List[FakeTemplate], name: str) -> Optional[List[FakeTemplate]]:
    """The list holding the first template named ``name``, searched depth first."""
    for template in templates:
        if template.name() == name:
            return templates
        if template.children is not None:
            found: Optional[List[FakeTemplate]] = _siblings(template.children, name)
            if found is not None:
                return found
    return None


class FakeParmTemplateGroup:
    """A ``hou.ParmTemplateGroup``, a copy of the node's layout."""

    def __init__(self, entries: List[FakeTemplate]) -> None:
        self.entries: List[FakeTemplate] = entries

    def findFolder(self, label: str) -> Optional[FakeTemplate]:
        return _find_folder(self.entries, label)

    def remove(self, name: str) -> None:
        # Houdini raises when the name isn't in the group.
        siblings: Optional[List[FakeTemplate]] = _siblings(self.entries, name)
        assert siblings is not None, name
        siblings[:] = [template for template in siblings if template.name() != name]

    def insertBefore(self, name: str, template: FakeTemplate) -> None:
        # The new template goes into the same folder as the one it is put before.
        siblings: Optional[List[FakeTemplate]] = _siblings(self.entries, name)
        assert siblings is not None, name
        index: int = [entry.name() for entry in siblings].index(name)
        siblings.insert(index, copy.deepcopy(template))


class FakeParmTuple:
    """A ``hou.ParmTuple`` that reads and writes the node's stored value."""

    def __init__(self, node: "FakeLayoutNode", name: str) -> None:
        self._node: FakeLayoutNode = node
        self._name: str = name

    def eval(self) -> Any:
        return self._node.values[self._name]

    def set(self, value: Any) -> None:
        self._node.values[self._name] = value


class FakeSpareParm:
    """A spare ``hou.Parm``, as the ROP's queue update reads it."""

    def __init__(self, name: str, folders: Tuple[str, ...]) -> None:
        self._name: str = name
        self._folders: Tuple[str, ...] = folders

    def name(self) -> str:
        return self._name

    def containingFolders(self) -> Tuple[str, ...]:
        return self._folders


def _labels_to(templates: Sequence[FakeTemplate], name: str) -> Optional[Tuple[str, ...]]:
    """The labels of the folders holding the template named ``name``, outermost first."""
    for template in templates:
        if template.name() == name:
            return ()
        if template.children is not None:
            inner: Optional[Tuple[str, ...]] = _labels_to(template.children, name)
            if inner is not None:
                return (template.label(),) + inner
    return None


def _all_templates(templates: Sequence[FakeTemplate]) -> List[FakeTemplate]:
    """Every template in ``templates`` and inside its folders, depth first."""
    found: List[FakeTemplate] = []
    for template in templates:
        found.append(template)
        found += _all_templates(template.parmTemplates())
    return found


class FakeLayoutNode:
    """A node's layout as Houdini keeps it: the definition's, or one stored on the node.

    Removing the spare parms brings back the definition's layout and drops the spare
    values, and an added spare parm starts at its default, as Houdini does. Setting a
    layout stores it on the node, so the node no longer follows its definition's layout.
    """

    def __init__(
        self,
        definition: List[FakeTemplate],
        layout: List[FakeTemplate],
        spare: Set[str],
        values: Dict[str, Any],
    ) -> None:
        self.definition: List[FakeTemplate] = definition
        self.layout: List[FakeTemplate] = layout
        self.spare: Set[str] = set(spare)
        self.values: Dict[str, Any] = dict(values)
        self.stores_layout: bool = False

    def parmTemplateGroup(self) -> FakeParmTemplateGroup:
        return FakeParmTemplateGroup(copy.deepcopy(self.layout))

    def parmTuple(self, name: str) -> FakeParmTuple:
        assert name in self.values, name
        return FakeParmTuple(self, name)

    def parm(self, name: str) -> FakeParmTuple:
        # One-value parms, such as the labels, are read and set as one-value tuples.
        return self.parmTuple(name)

    def spareParms(self) -> List[FakeSpareParm]:
        # Houdini lists spare fields, not the simple folders that hold them.
        folders: Set[str] = {
            entry.name() for entry in _all_templates(self.layout) if entry.children is not None
        }
        return [
            FakeSpareParm(name, _labels_to(self.layout, name) or ())
            for name in sorted(self.spare - folders)
        ]

    def removeSpareParmFolder(self, folder: Tuple[str, ...]) -> None:
        # The last label names the folder to remove, and the one before it names its holder.
        holder: Optional[FakeTemplate] = _find_folder(self.layout, folder[-2])
        assert holder is not None and holder.children is not None, folder
        removed: List[FakeTemplate] = [e for e in holder.children if e.label() == folder[-1]]
        assert removed, folder
        holder.children[:] = [e for e in holder.children if e.label() != folder[-1]]
        self.spare -= {name for entry in removed for name in entry.names()}

    def removeSpareParms(self) -> None:
        self.layout = copy.deepcopy(self.definition)
        self.stores_layout = False
        for name in self.spare:
            self.values.pop(name, None)
        self.spare = set()

    def setParmTemplateGroup(self, group: FakeParmTemplateGroup) -> None:
        # The group is stored as the node's layout, and anything the definition lacks is spare.
        self.layout = copy.deepcopy(group.entries)
        self.stores_layout = True
        declared: Set[str] = {name for entry in self.definition for name in entry.names()}
        self.spare = {name for entry in self.layout for name in entry.names()} - declared

    def addSpareParmTuple(self, template: FakeTemplate, in_folder: Tuple[str, ...]) -> None:
        # Houdini refuses a folder path that the layout doesn't have.
        (label,) = in_folder
        folder: Optional[FakeTemplate] = _find_folder(self.layout, label)
        assert folder is not None and folder.children is not None, label
        folder.children.append(copy.deepcopy(template))
        for name in template.names():
            self.spare.add(name)
            self.values[name] = ""

    def tabs(self) -> List[str]:
        """The labels of the top-level folders, in order."""
        return [entry.label() for entry in self.layout if entry.children is not None]

    def folder(self, label: str) -> List[str]:
        """The names directly inside the folder with ``label``."""
        found: Optional[FakeTemplate] = _find_folder(self.layout, label)
        assert found is not None, label
        return [child.name() for child in found.parmTemplates()]


_PACKAGES: str = "queue_env_do_not_use_CondaPackages"
_CHANNELS: str = "queue_env_do_not_use_CondaChannels"


def _conda() -> FakeTemplate:
    """The ROP's Conda folder, with its two prefixed fields."""
    return FakeTemplate(
        "queue_env_do_not_use_Conda",
        "Conda",
        [FakeTemplate(_PACKAGES), FakeTemplate(_CHANNELS)],
    )


#: The parms of the Deadline Cloud Settings folder, in the node script's order.
_SETTINGS_PARMS: Tuple[str, ...] = ("farm", "queue", "login", "logout", "settings")


def _shared_tab(
    name: str, properties: str, settings: str, settings_parms: Optional[List[FakeTemplate]] = None
) -> FakeTemplate:
    """The Shared Job Settings tab, with its two inner folders of the definition's own parms.

    The Deadline Cloud Settings parms are ``settings_parms``, or the plain ones an older node has.
    """
    labels: List[FakeTemplate] = settings_parms or [FakeTemplate(name) for name in _SETTINGS_PARMS]
    return FakeTemplate(
        name,
        "Shared Job Settings",
        [
            FakeTemplate(
                properties, "Job Properties", [FakeTemplate("name"), FakeTemplate("priority")]
            ),
            FakeTemplate(settings, "Deadline Cloud Settings", labels),
        ],
    )


def _four_tabs(shared: FakeTemplate, pdg: str, attachments: str, host: str) -> List[FakeTemplate]:
    """The four tabs, with the given folder names, after the given Shared Job Settings tab."""
    return [
        shared,
        FakeTemplate(pdg, "PDG", [FakeTemplate("working_dir")]),
        FakeTemplate(attachments, "Job Attachments", [FakeTemplate("output_directories")]),
        FakeTemplate(host, "Host Requirements", [FakeTemplate("hr_mode"), FakeTemplate("hr_os")]),
    ]


def _settings_in_node_script() -> List[FakeTemplate]:
    """The Deadline Cloud Settings parms as the node's script defines them: hidden, and any menu."""
    # Read each parm's block, then its hidden flag and the text of its menu script, if any.
    blocks: Dict[str, str] = _parms(NODE_SCRIPT)
    templates: List[FakeTemplate] = []
    for name in _SETTINGS_PARMS:
        menu: Optional["re.Match[str]"] = re.search(r"\bmenu\s*\{(.*?)\n\s*\}", blocks[name], re.S)
        hidden: bool = _flag(blocks[name], "invisible")
        templates.append(
            FakeTemplate(name, hidden=hidden, menu_script=menu.group(1) if menu else "")
        )
    return templates


def _current_tabs(settings_parms: Optional[List[FakeTemplate]] = None) -> List[FakeTemplate]:
    """The current four tabs, with Submit last in Shared Job Settings, and the given settings parms."""
    shared: FakeTemplate = _shared_tab("job_settings", "folder0", "folder1", settings_parms)
    assert shared.children is not None
    shared.children.append(FakeTemplate("submit"))
    return _four_tabs(shared, "job_settings_1", "job_settings_2", "host_requirements")


def _definition() -> List[FakeTemplate]:
    """The current definition's layout, with the Deadline Cloud Settings read from the node's script."""
    return _current_tabs(_settings_in_node_script())


#: The values of the definition's own parms, which must survive the rebuild.
_REAL_VALUES: Dict[str, Any] = {
    "name": ("bonfire",),
    "priority": (77,),
    "farm": ("farm-1",),
    "queue": ("queue-1",),
    "working_dir": ("$HIP",),
    "output_directories": (1,),
    "hr_mode": ("custom",),
    "hr_os": ("linux windows",),
    "submit": (0,),
}
_CONDA_VALUES: Dict[str, Any] = {
    _PACKAGES: ("houdini=21.0.* mantra",),
    _CHANNELS: ("deadline-cloud",),
}


@pytest.fixture
def scene(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHou:
    """A saved scene, so the fake ``hou`` names its parm template types."""
    hip: str = os.path.join(str(tmp_path), "scene", "shot.hip")
    os.makedirs(os.path.dirname(hip))
    return install_hou(monkeypatch, hip)


def _frozen_submit_below_tabs_node() -> FakeLayoutNode:
    """A node made on the four-tab definition that had Submit below the tabs, outside any tab.

    The ROP's queue update stored that layout on the node, so its tabs are spare folders
    and Submit still follows them.
    """
    # The old four tabs as spare folders, with the Conda folder in the first, then Submit.
    shared: FakeTemplate = _shared_tab("job_settings51", "folder2", "folder3")
    assert shared.children is not None
    shared.children.append(_conda())
    layout: List[FakeTemplate] = _four_tabs(shared, "folder4", "folder5", "folder6")
    layout.append(FakeTemplate("submit"))
    spare: Set[str] = {"job_settings51", "folder2", "folder3", "folder4", "folder5", "folder6"}
    spare |= set(_conda().names())
    values: Dict[str, Any] = {**_REAL_VALUES, **_CONDA_VALUES, "queue_env_do_not_use_Conda": (0,)}
    return FakeLayoutNode(_definition(), layout, spare, values)


def test_an_old_nodes_submit_below_the_tabs_moves_into_shared_job_settings(
    scene: FakeHou,
) -> None:
    """Submit below the tabs moved down with the tallest tab, so a loaded node must not keep it there."""
    # Rebuild the node saved with Submit after the tabs, then read where Submit is.
    node: FakeLayoutNode = _frozen_submit_below_tabs_node()
    rebuild_layout_from_definition(node)
    assert node.tabs() == ["Shared Job Settings", "PDG", "Job Attachments", "Host Requirements"]
    assert all(entry.children is not None for entry in node.layout)
    assert node.folder("Shared Job Settings") == [
        "folder0",
        "folder1",
        "queue_env_do_not_use_Conda",
        "submit",
    ]
    # Only the Conda fields are spare, and every value comes back.
    assert node.spare == set(_conda().names())
    assert {name: node.values[name] for name in _REAL_VALUES} == _REAL_VALUES
    assert {name: node.values[name] for name in _CONDA_VALUES} == _CONDA_VALUES


def test_a_node_with_no_queue_folder_keeps_the_definitions_layout(scene: FakeHou) -> None:
    """A stored layout stops a node from following its definition, so a node with no folder keeps none."""
    # A node that was never logged in, so the ROP's update never added a folder.
    node: FakeLayoutNode = FakeLayoutNode(_definition(), _definition(), set(), dict(_REAL_VALUES))
    move_queue_folders_above_submit(node)
    assert not node.stores_layout
    assert node.folder("Shared Job Settings") == ["folder0", "folder1", "submit"]


def _settings_flags(node: FakeLayoutNode) -> Dict[str, Tuple[bool, bool]]:
    """Whether each Deadline Cloud Settings parm is hidden, and whether it has a menu script."""
    settings: Optional[FakeTemplate] = _find_folder(node.layout, "Deadline Cloud Settings")
    assert settings is not None
    return {
        child.name(): (child.isHidden(), bool(child.itemGeneratorScript()))
        for child in settings.parmTemplates()
    }


def _stored_current_tabs_node(buttons_hidden: bool = False) -> FakeLayoutNode:
    """A node made on the current tabs while Farm and Queue were labels, with its layout stored.

    The ROP's queue update stored the layout with the Conda folder, so the labels are kept, and
    so are the Login, Logout and Settings flags the node was saved with.
    """
    # The current tabs with the two labels and the buttons, and the Conda folder above Submit.
    buttons: List[FakeTemplate] = [
        FakeTemplate(name, hidden=buttons_hidden) for name in _SETTINGS_PARMS[2:]
    ]
    stored: List[FakeTemplate] = _current_tabs(
        [FakeTemplate("farm"), FakeTemplate("queue"), *buttons]
    )
    shared: Optional[FakeTemplate] = _find_folder(stored, "Shared Job Settings")
    assert shared is not None and shared.children is not None
    shared.children.insert(2, _conda())
    # The node follows the current definition, but its stored layout wins until it's rebuilt.
    values: Dict[str, Any] = {**_REAL_VALUES, **_CONDA_VALUES, "queue_env_do_not_use_Conda": (0,)}
    node: FakeLayoutNode = FakeLayoutNode(_definition(), stored, set(_conda().names()), values)
    node.stores_layout = True
    return node


#: The node's own flags: Farm and Queue are shown menus, and Login and Logout are hidden.
_DEFINED_SETTINGS: Dict[str, Tuple[bool, bool]] = {
    "farm": (False, True),
    "queue": (False, True),
    "login": (True, False),
    "logout": (True, False),
    "settings": (False, False),
}


@pytest.mark.parametrize(
    "make_node",
    [
        _frozen_submit_below_tabs_node,
        _stored_current_tabs_node,
        functools.partial(_stored_current_tabs_node, buttons_hidden=True),
    ],
    ids=["submit below the tabs", "current tabs, buttons shown", "current tabs, buttons hidden"],
)
def test_an_old_node_loads_with_the_farm_and_queue_menus_and_login_and_logout_hidden(
    scene: FakeHou, make_node: Any
) -> None:
    """A node saved with the labels, and with any button shown or hidden, keeps its own layout.

    So the rebuild must bring the menus and the definition's flags, and keep the values.
    """
    node: FakeLayoutNode = make_node()
    flags: Dict[str, Tuple[bool, bool]] = _settings_flags(node)
    assert [flags["farm"], flags["queue"]] == [(False, False), (False, False)]
    # After the rebuild each parm has the definition's flag and menu, and the values are kept.
    rebuild_layout_from_definition(node)
    assert _settings_flags(node) == _DEFINED_SETTINGS
    assert node.parm("farm").eval() == ("farm-1",)
    assert node.parm("queue").eval() == ("queue-1",)
    assert {name: node.values[name] for name in _CONDA_VALUES} == _CONDA_VALUES


def test_the_rops_queue_update_after_the_rebuild_keeps_the_menus_and_can_set_them(
    scene: FakeHou,
) -> None:
    """The ROP's update sets Farm and Queue and rewrites the layout, and must keep the menus."""
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini.queue_parameters import (
        remove_queue_parameters_from_node,
    )

    node: FakeLayoutNode = _stored_current_tabs_node()
    rebuild_layout_from_definition(node)
    # As the ROP's update does: set the parms, drop the queue folder, add it back, then move it.
    node.parm("farm").set("Farm One")
    node.parm("queue").set("Queue One")
    remove_queue_parameters_from_node(node)  # type: ignore[arg-type]
    node.addSpareParmTuple(_conda(), ("Shared Job Settings",))
    move_queue_folders_above_submit(node)
    # The menus are still shown and read back what was set.
    assert node.stores_layout
    assert _settings_flags(node) == _DEFINED_SETTINGS
    assert node.parm("farm").eval() == "Farm One"
    assert node.parm("queue").eval() == "Queue One"
    assert node.folder("Shared Job Settings") == [
        "folder0",
        "folder1",
        "queue_env_do_not_use_Conda",
        "submit",
    ]
