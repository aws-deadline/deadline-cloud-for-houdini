# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose one step has more tasks than a job may have.

A Generic Generator makes 10,001 items, and the geometry step below gets one task per
item. Deadline Cloud's default limit is 10,000 tasks per job and per step. The cook
counts the items while PDG generates, and refuses as soon as the count passes the
limit. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_scheduler, main, new_topnet

#: One more item than Deadline Cloud's default limit of tasks per job.
ITEM_COUNT: int = 10_001


def build() -> hou.Node:
    """The scene: a generator of 10,001 items, and a geometry step below it."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The generator runs no command, so the geometry step is the job's only step.
    generator: hou.Node = topnet.createNode("genericgenerator", "items")
    generator.parm("itemcount").set(ITEM_COUNT)
    writer: hou.Node = add_geometry_writer(
        topnet, "write_too_many", generator, "$HIP/quota_exceeded/item`@pdg_index`.bgeo.sc"
    )

    writer.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
