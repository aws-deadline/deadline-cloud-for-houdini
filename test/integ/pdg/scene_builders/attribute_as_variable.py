# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose write parm spells a work item attribute as a Houdini variable.

``$wedgeindex`` in the output path is the Wedge node's attribute, not a Houdini
variable. It is empty in the artist's session, where PDG records the path the step will
report, and set on the worker, where the file is written. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, main, new_topnet


def build() -> hou.Node:
    """The scene: items, and a geometry step whose output path uses ``$wedgeindex``."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The Wedge node gives each item the attribute wedgeindex, which the path spells $wedgeindex.
    items: hou.Node = add_items(topnet, 2)
    writer: hou.Node = add_geometry_writer(
        topnet, "write_by_variable", items, "$HIP/attribute_as_variable/item$wedgeindex.bgeo.sc"
    )

    writer.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
