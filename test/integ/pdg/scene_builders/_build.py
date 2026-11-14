# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Helpers the scene builders share. Runs inside hython.

Each builder defines ``build()``, which makes its scene in the current Houdini session
and returns the TOP network, and ends with ``main(build)``. Run as a script, a builder
saves the scene to the path it is given::

    hython <builder>.py <scene.hip>
"""

from __future__ import annotations

import sys
from typing import Callable

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


def save_scene(path: str) -> None:
    """Save the session as ``path``."""
    hou.hipFile.save(path)


def main(build: Callable[[], hou.Node]) -> None:
    """Build the scene, then save it to the path given as the first argument."""
    build()
    save_scene(sys.argv[1])
