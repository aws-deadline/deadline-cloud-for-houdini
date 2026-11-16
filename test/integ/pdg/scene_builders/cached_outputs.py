# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose step's outputs already exist next to it.

With Cache Mode Automatic, PDG treats an item whose output file exists as cooked from
cache and never offers it to the scheduler, so its task would never run and the job
would wait. The builder writes the outputs beside the saved scene, because the test
cooks the scene in a new folder. Runs inside hython.
"""

from __future__ import annotations

import os
import sys

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, new_topnet, save_scene

# How many items the step has, and so how many output files the builder writes.
ITEM_COUNT: int = 2

# Where each item's output goes, under the scene's folder, as the step and the builder name it.
_OUTPUT_FOLDER: str = "cached_outputs"
_OUTPUT_NAME: str = "item{index}.bgeo.sc"


def build() -> hou.Node:
    """The scene: items, and a geometry step with Cache Mode Automatic, its default."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The step writes one file per item, where the builder writes them first.
    items: hou.Node = add_items(topnet, ITEM_COUNT)
    output: str = f"$HIP/{_OUTPUT_FOLDER}/{_OUTPUT_NAME.format(index='`@pdg_index`')}"
    writer: hou.Node = add_geometry_writer(topnet, "write_cached", items, output)

    writer.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


def write_outputs(scene_folder: str, writer: hou.Node) -> None:
    """Write each item's output file in ``scene_folder``, as the step would have."""
    # The files hold the step's own box, as a cook of the step would leave them.
    folder: str = os.path.join(scene_folder, _OUTPUT_FOLDER)
    os.makedirs(folder, exist_ok=True)
    geometry: hou.Geometry = hou.node(writer.parm("soppath").eval()).geometry()
    for index in range(ITEM_COUNT):
        geometry.saveToFile(os.path.join(folder, _OUTPUT_NAME.format(index=index)))


if __name__ == "__main__":
    # The outputs go beside the saved scene, where the step's path resolves.
    scene_path: str = sys.argv[1]
    topnet: hou.Node = build()
    save_scene(scene_path)
    write_outputs(os.path.dirname(os.path.abspath(scene_path)), topnet.node("write_cached"))
