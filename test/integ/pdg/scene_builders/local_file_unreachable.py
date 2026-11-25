# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds a scene whose step reads a local file outside the working directory that doesn't exist.

The step's geometry comes from a File node that reads an absolute path in the system's
temporary folder. No step writes that file and it isn't on disk, so the job can't
carry it and the task would open a missing file. Runs inside hython.
"""

from __future__ import annotations

import os
import tempfile

import hou  # type: ignore[import-not-found]
from _build import add_items, add_scheduler, main, new_topnet

#: The folder of the missing file, which the refusal names.
MISSING_FOLDER: str = "pdg_integ_unreachable"


def build() -> hou.Node:
    """The scene: items, and a geometry step that reads the missing file."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # The File node's path is written out in full, so it doesn't follow $HIP to a worker.
    missing: str = os.path.join(tempfile.gettempdir(), MISSING_FOLDER, "missing.bgeo.sc")
    source_geo: hou.Node = hou.node("/obj").createNode("geo", "unreachable_geo")
    reader: hou.Node = source_geo.createNode("file", "read_missing")
    reader.parm("file").set(missing.replace("\\", "/"))

    # The step writes what the File node reads, one file per item.
    items: hou.Node = add_items(topnet, 2)
    writer: hou.Node = topnet.createNode("ropgeometry", "write_from_missing")
    writer.setFirstInput(items)
    writer.parm("soppath").set(reader.path())
    writer.parm("sopoutput").set("$HIP/local_file_unreachable/item`@pdg_index`.bgeo.sc")

    writer.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
