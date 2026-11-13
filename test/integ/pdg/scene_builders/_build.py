# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Helpers the scene builders share. Runs inside hython.

Each builder defines ``build()``, which makes its scene in the current Houdini session
and returns the TOP network, and ends with ``main(build)``. Run as a script, a builder
saves the scene to the path it is given::

    hython <builder>.py <scene.hip>
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

import hou  # type: ignore[import-not-found]

# The type name of the Deadline Cloud Scheduler node.
SCHEDULER_TYPE: str = "deadlinecloudscheduler"


def new_topnet() -> hou.Node:
    """An empty TOP network in /obj, without the Local Scheduler Houdini adds to a new one."""
    topnet: hou.Node = hou.node("/obj").createNode("topnet", "topnet1")
    for child in topnet.children():
        child.destroy()
    return topnet


def add_scheduler(topnet: hou.Node) -> hou.Node:
    """A Deadline Cloud Scheduler node in ``topnet``, made the network's scheduler."""
    scheduler: hou.Node = topnet.createNode(SCHEDULER_TYPE)
    topnet.parm("topscheduler").set(scheduler.name())
    return scheduler


def add_items(topnet: hou.Node, count: int) -> hou.Node:
    """A Wedge node named ``items`` that makes ``count`` work items. It is not a step."""
    wedge: hou.Node = topnet.createNode("wedge", "items")
    wedge.parm("wedgecount").set(count)
    return wedge


def add_geometry_writer(
    topnet: hou.Node, name: str, upstream: Optional[hou.Node], output: str
) -> hou.Node:
    """A ROP Geometry Output step named ``name`` that writes a box to ``output`` per item.

    The box comes from its own geometry network in /obj. With no ``upstream``, the step
    makes its own items from its frame range.
    """
    # The box to write, then the step that writes it.
    box_geo: hou.Node = hou.node("/obj").createNode("geo", f"{name}_geo")
    box: hou.Node = box_geo.createNode("box", "box")
    writer: hou.Node = topnet.createNode("ropgeometry", name)
    if upstream is not None:
        writer.setFirstInput(upstream)
    writer.parm("soppath").set(box.path())
    writer.parm("sopoutput").set(output)
    return writer


def set_menu(parm: hou.Parm, label: str) -> None:
    """Set a menu parm to the entry with this label.

    TOP menus such as Generate When list their entries out of value order, so the label
    is the stable name. Every menu the builders set has numbers as its values.
    """
    value: str = parm.menuItems()[parm.menuLabels().index(label)]
    parm.set(int(value))


def save_scene(path: str) -> None:
    """Save the session as ``path``."""
    hou.hipFile.save(path)


def main(build: Callable[[], hou.Node]) -> None:
    """Build the scene, then save it to the path given as the first argument."""
    build()
    save_scene(sys.argv[1])
