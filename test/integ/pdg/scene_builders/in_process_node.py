# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene with a node between two steps that cooks on the workstation.

A Python Script node set to Cook (In-Process) runs in the artist's Houdini session, not
on a worker. A file it writes there never reaches the step below, which runs on the
farm. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, main, new_topnet, set_menu

# What the in-process node does for each item: record the file the step above wrote.
_SCRIPT: str = """\
import os
listing = os.path.join(os.path.dirname(work_item.inputResultData[0].path), "listing.txt")
with open(listing, "a") as listing_file:
    listing_file.write(work_item.inputResultData[0].path + "\\n")
work_item.addResultData(listing, "file/text", 0)
"""


def build() -> hou.Node:
    """The scene: items, a step, the in-process node, and a step below it."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The step above the in-process node, which runs on a worker.
    items: hou.Node = add_items(topnet, 2)
    first: hou.Node = add_geometry_writer(
        topnet, "write_first", items, "$HIP/in_process_node/first`@pdg_index`.bgeo.sc"
    )

    # The node that runs here, between two steps, and writes a file.
    in_process: hou.Node = topnet.createNode("pythonscript", "list_in_process")
    in_process.setFirstInput(first)
    set_menu(in_process.parm("pdg_cooktype"), "Cook (In-Process)")
    in_process.parm("script").set(_SCRIPT)

    # The step below it, which runs on a worker.
    last: hou.Node = add_geometry_writer(
        topnet, "write_last", in_process, "$HIP/in_process_node/last`@pdg_index`.bgeo.sc"
    )

    last.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
