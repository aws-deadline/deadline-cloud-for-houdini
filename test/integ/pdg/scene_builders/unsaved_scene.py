# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene that cooks fine, for a cook that never saves it.

``scenes.yaml`` marks this scene ``saved: false``, so the cook script builds it in its
own Houdini session and cooks it there. Workers load the saved file, and there is none,
so the scheduler refuses the cook. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, main, new_topnet


def build() -> hou.Node:
    """The scene: items, and one geometry step."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # One step that writes a box per item, so only the missing save is refused.
    items: hou.Node = add_items(topnet, 2)
    writer: hou.Node = add_geometry_writer(
        topnet, "write_geometry", items, "$HIP/unsaved_scene/item`@pdg_index`.bgeo.sc"
    )

    writer.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
