# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene with a step in the cook whose Cook Type is Service.

A Service step's items cook in a long-running service process, which this scheduler
doesn't start. A Service node outside the cook isn't refused, so this one is the
node the cook ends at. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_items, add_scheduler, main, new_topnet, set_menu


def build() -> hou.Node:
    """The scene: items, and one geometry step set to cook as a Service."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The one step, set to cook in a service process.
    items: hou.Node = add_items(topnet, 2)
    writer: hou.Node = add_geometry_writer(
        topnet, "write_as_service", items, "$HIP/service_node/item`@pdg_index`.bgeo.sc"
    )
    set_menu(writer.parm("pdg_cooktype"), "Service")

    writer.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
