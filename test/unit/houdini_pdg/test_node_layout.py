# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler node's tabs, rebuilt from its definition when a scene loads or a node is made."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pytest

# The fakes come first, because they install the fake ``hou`` the host module imports.
from .houdini_fakes import FakeHou, install_hou

from deadline.houdini_pdg.houdini.host import (
    move_queue_folders_above_submit,
    rebuild_layout_from_definition,
)


class FakeTemplate:
    """A ``hou.ParmTemplate``: a field, or a folder with the templates inside it."""

    def __init__(
        self, name: str, label: str = "", children: Optional[List["FakeTemplate"]] = None
    ) -> None:
        self._name: str = name
        self._label: str = label or name
        self.children: Optional[List[FakeTemplate]] = children

    def name(self) -> str:
        return self._name

    def label(self) -> str:
        return self._label

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


def _shared_tab(name: str, properties: str, settings: str) -> FakeTemplate:
    """The Shared Job Settings tab, with its two inner folders of the definition's own parms."""
    return FakeTemplate(
        name,
        "Shared Job Settings",
        [
            FakeTemplate(
                properties, "Job Properties", [FakeTemplate("name"), FakeTemplate("priority")]
            ),
            FakeTemplate(
                settings, "Deadline Cloud Settings", [FakeTemplate("farm"), FakeTemplate("queue")]
            ),
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


def _definition() -> List[FakeTemplate]:
    """The current definition's layout: four tabs, with Submit last in Shared Job Settings."""
    shared: FakeTemplate = _shared_tab("job_settings", "folder0", "folder1")
    assert shared.children is not None
    shared.children.append(FakeTemplate("submit"))
    return _four_tabs(shared, "job_settings_1", "job_settings_2", "host_requirements")


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
