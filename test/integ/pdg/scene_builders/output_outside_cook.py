# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose first step writes outside the cook's working directory.

The job uploads only what a task writes in the working directory or a listed output
folder. The first step writes to an absolute path in the system's temporary folder, so
the step below, on another worker, would never get those files. Runs inside hython.
"""

from __future__ import annotations

import os
import tempfile

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, main, new_topnet

#: The folder outside the working directory that the first step writes to.
OUTSIDE_FOLDER: str = "pdg_integ_outside_cook"


def build() -> hou.Node:
    """The scene: items, a step that writes outside the working directory, and a step below it."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The path is written out in full, outside $HIP, in a folder the job uploads no
    # outputs from.
    outside_folder: str = os.path.join(tempfile.gettempdir(), OUTSIDE_FOLDER).replace("\\", "/")
    items: hou.Node = add_items(topnet, 2)
    outside: hou.Node = add_geometry_writer(
        topnet, "write_outside", items, f"{outside_folder}/item`@pdg_index`.bgeo.sc"
    )
    # Only a step that another step reads from is refused, so one is below it.
    last: hou.Node = add_geometry_writer(
        topnet, "write_inside", outside, "$HIP/output_outside_cook/item`@pdg_index`.bgeo.sc"
    )

    last.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
