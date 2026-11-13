# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene with a step that generates its work items while the cook runs.

The second step's Generate When is "Each Upstream Item is Cooked", so its items don't
exist when the job is described. Only that step is refused, not a step below it that
PDG flags dynamic for being below it. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, main, new_topnet, set_menu


def build() -> hou.Node:
    """The scene: items, a step, a dynamic step below it, and a step below that."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # A static step, then the dynamic one, which waits for each item above it to cook.
    items: hou.Node = add_items(topnet, 2)
    first: hou.Node = add_geometry_writer(
        topnet, "write_first", items, "$HIP/dynamic_node/first`@pdg_index`.bgeo.sc"
    )
    dynamic: hou.Node = add_geometry_writer(
        topnet, "write_when_cooked", first, "$HIP/dynamic_node/second`@pdg_index`.bgeo.sc"
    )
    set_menu(dynamic.parm("pdg_workitemgeneration"), "Each Upstream Item is Cooked")

    # A step below the dynamic one, which PDG flags dynamic too, and which isn't refused.
    last: hou.Node = add_geometry_writer(
        topnet, "write_last", dynamic, "$HIP/dynamic_node/third`@pdg_index`.bgeo.sc"
    )

    last.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
