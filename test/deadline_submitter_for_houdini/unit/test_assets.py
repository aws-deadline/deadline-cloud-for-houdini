# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from .mock_hou import hou_module as hou
from deadline.houdini_submitter.python.deadline_submitter_for_houdini import (
    _get_scene_asset_references,
)


def test_get_scene_asset_references():
    hou.hscript.return_value = (
        "1 [ ] /out/mantra1 \t( 1 5 1 )\n2 [ 1 ] /out/karma1/lopnet/rop_usdrender \t( 1 5 1 )\n",
        "",
    )
    node = hou.node
    hou.node.type().name.return_value = "deadline-cloud"
    parm = hou.Parm
    hou.Parm.node.return_value = node
    hou.Parm.name.return_value = "shadowmap_file"
    hou.node.type().nameWithCategory.return_value = "Driver/ifd"
    hou.hipFile.path.return_value = "/some/path/test.hip"
    hou.node.parm().eval.return_value = "/tmp/foo.$F.exr"
    hou.fileReferences.return_value = (
        (None, "$HIP/houdini19.5/otls/Deadline-Cloud.hda"),
        (parm, "temp:$OS.rat"),
    )
    a = _get_scene_asset_references(node)
    assert a.input_filenames == {"$HIP/houdini19.5/otls/Deadline-Cloud.hda", "/some/path/test.hip"}
    assert a.input_directories == set()
    assert a.output_directories == set()
