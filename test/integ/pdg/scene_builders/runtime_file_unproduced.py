# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose step reads a file that no step writes and that doesn't exist.

A Python Processor at the top makes one work item that names a missing file as its
output, and the step below gets that file as its input. Nothing in the job writes it
and it isn't on disk, so a worker would never have it. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_geometry_writer, add_scheduler, main, new_topnet

#: The missing file's name, which the refusal names.
MISSING_FILE: str = "never_written.bgeo.sc"

# The processor's generate code: one item, whose output is the missing file next to the scene.
_GENERATE: str = f"""\
import os
import hou
item = item_holder.addWorkItem(index=0)
missing = os.path.join(os.path.dirname(hou.hipFile.path()), "runtime_file_unproduced", "{MISSING_FILE}")
item.addResultData(missing, "file/geo", 0)
"""


def build() -> hou.Node:
    """The scene: a processor that names the missing file, and a step that reads it."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The processor runs before the job exists, so it is not refused itself.
    processor: hou.Node = topnet.createNode("pythonprocessor", "name_missing_file")
    processor.parm("generate").set(_GENERATE)
    reader: hou.Node = add_geometry_writer(
        topnet, "read_missing", processor, "$HIP/runtime_file_unproduced/item`@pdg_index`.bgeo.sc"
    )

    reader.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
