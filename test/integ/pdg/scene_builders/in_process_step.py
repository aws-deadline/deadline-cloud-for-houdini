# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose one step is set to cook In-Process.

PDG cooks an In-Process step's items in the artist's Houdini session and never offers
them to the scheduler, so the job's tasks for it would never run. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, main, new_topnet, set_menu


def build() -> hou.Node:
    """The scene: items, and one geometry step set to cook In-Process."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The one step, set to cook in this Houdini session.
    items: hou.Node = add_items(topnet, 2)
    writer: hou.Node = add_geometry_writer(
        topnet, "write_in_process", items, "$HIP/in_process_step/item`@pdg_index`.bgeo.sc"
    )
    set_menu(writer.parm("pdg_cooktype"), "In-Process")

    writer.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
