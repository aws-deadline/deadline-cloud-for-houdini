# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose step writes its frames in batches of five.

PDG sends each batch as one batch parent item, and the job has no task for a batch
parent, so the scheduler refuses batching. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_scheduler, main, new_topnet, set_menu

# The step's frames, which make two batches of five.
_FIRST_FRAME: int = 1
_LAST_FRAME: int = 10
_FRAMES_PER_BATCH: int = 5


def build() -> hou.Node:
    """The scene: one geometry step that makes its own items from a frame range, in batches."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The step makes one item per frame, and puts every five frames in one batch. Its
    # frame range parms default to the expressions $FSTART and $FEND, so those are removed.
    writer: hou.Node = add_geometry_writer(
        topnet, "write_batched", None, "$HIP/rop_batch/frame$F4.bgeo.sc"
    )
    set_menu(writer.parm("framegeneration"), "Frame Range")
    writer.parm("f1").deleteAllKeyframes()
    writer.parm("f1").set(_FIRST_FRAME)
    writer.parm("f2").deleteAllKeyframes()
    writer.parm("f2").set(_LAST_FRAME)
    writer.parm("framesperbatch").set(_FRAMES_PER_BATCH)

    writer.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
