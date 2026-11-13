# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene where one step sends its work items to a Local Scheduler.

The cook is one job on the Deadline Cloud Scheduler node, so a step that overrides the
network's scheduler would run somewhere else. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, main, new_topnet


def build() -> hou.Node:
    """The scene: items, a step on the network's scheduler, and a step on a Local Scheduler."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)
    local: hou.Node = topnet.createNode("localscheduler", "localscheduler")

    # The first step uses the network's scheduler, and the second overrides it.
    items: hou.Node = add_items(topnet, 2)
    first: hou.Node = add_geometry_writer(
        topnet, "write_first", items, "$HIP/multiple_schedulers/first`@pdg_index`.bgeo.sc"
    )
    local_step: hou.Node = add_geometry_writer(
        topnet, "write_locally", first, "$HIP/multiple_schedulers/local`@pdg_index`.bgeo.sc"
    )
    local_step.parm("topscheduler").set(local.path())

    local_step.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
