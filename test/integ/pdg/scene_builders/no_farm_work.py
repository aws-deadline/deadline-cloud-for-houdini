# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose cook is only a Wedge node.

A Wedge node makes work items but runs no command, so a cook of it alone gives the
farm nothing to do, and the job would have no steps. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_items, add_scheduler, main, new_topnet


def build() -> hou.Node:
    """The scene: one Wedge node, which the cook ends at."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The Wedge node is the cook's only node, because the display flag is on it.
    items: hou.Node = add_items(topnet, 2)
    items.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
