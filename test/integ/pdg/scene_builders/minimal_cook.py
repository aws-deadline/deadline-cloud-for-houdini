# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds the smallest scene that cooks on the farm with files flowing between steps.

Five Wedge items each write a geometry file, then copy it. One item after a Wait for
All reads all five copies and writes them merged into one file. Each step reads files
an earlier step wrote on another worker, so the scene checks that outputs reach the
steps that need them. Everything is written under the working directory. Runs inside
hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import add_scheduler, main, new_topnet

# How many items the Wedge node makes, and so how many tasks the first two steps have.
ITEM_COUNT: int = 5

# The File Copy node's menu entries: copy while cooking, on a worker, the upstream file,
# into a folder. The node has a Cook Type parm too, which Houdini reads with its own menu.
_COPY_OUT_OF_PROCESS: int = 2
_COOK_TYPE_OUT_OF_PROCESS: int = 2
_COPY_UPSTREAM_FILE: int = 0
_COPY_TO_FOLDER: int = 0

# The tag PDG gives a geometry file. The copies get it too, so the last step finds them by it.
_GEOMETRY_TAG: str = "file/geo"


def build() -> hou.Node:
    """The scene, with the Deadline Cloud Scheduler as the network's scheduler."""
    topnet: hou.Node = new_topnet()
    add_scheduler(topnet)

    # Five items. Each one's box is a different size, so the five files differ.
    wedge: hou.Node = topnet.createNode("wedge", "items")
    wedge.parm("wedgecount").set(ITEM_COUNT)
    box_geo: hou.Node = hou.node("/obj").createNode("geo", "box_geo")
    box: hou.Node = box_geo.createNode("box", "box")
    box.parm("scale").setExpression('1 + pdgattrib("wedgeindex", 0)', hou.exprLanguage.Hscript)

    # Each item's box, written to its own file. The item index keeps the files apart.
    write: hou.Node = topnet.createNode("ropgeometry", "write_geometry")
    write.setFirstInput(wedge)
    write.parm("soppath").set(box.path())
    write.parm("sopoutput").set("$HIP/minimal_cook/geo/item`@pdg_index`.bgeo.sc")

    # Each file copied on a worker, which needs the file the step above wrote.
    copy: hou.Node = topnet.createNode("filecopy", "copy_geometry")
    copy.setFirstInput(write)
    copy.parm("copyduring").set(_COPY_OUT_OF_PROCESS)
    copy.parm("pdg_cooktype").set(_COOK_TYPE_OUT_OF_PROCESS)
    copy.parm("filesource").set(_COPY_UPSTREAM_FILE)
    copy.parm("copyto").set(_COPY_TO_FOLDER)
    copy.parm("destfolder").set("$HIP/minimal_cook/copies")
    copy.parm("resultdatatag").set(_GEOMETRY_TAG)

    # One item that reads every copy, through one File node per input, and writes them merged.
    wait: hou.Node = topnet.createNode("waitforall", "all_items")
    wait.setFirstInput(copy)
    merge_geo: hou.Node = hou.node("/obj").createNode("geo", "merge_geo")
    merge: hou.Node = merge_geo.createNode("merge", "merge")
    for index in range(ITEM_COUNT):
        reader: hou.Node = merge_geo.createNode("file", f"copy{index}")
        reader.parm("file").set(f'`pdginput({index}, "{_GEOMETRY_TAG}", 1)`')
        merge.setInput(index, reader)
    merged: hou.Node = topnet.createNode("ropgeometry", "merge_geometry")
    merged.setFirstInput(wait)
    merged.parm("soppath").set(merge.path())
    merged.parm("sopoutput").set("$HIP/minimal_cook/merged.bgeo.sc")

    merged.setDisplayFlag(True)
    topnet.layoutChildren()
    return topnet


if __name__ == "__main__":
    main(build)
