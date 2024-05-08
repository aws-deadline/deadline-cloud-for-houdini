# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from unittest import mock
from .mock_hou import hou_module as hou
from deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets import (
    _get_scene_asset_references,
)


def test_get_scene_asset_references():
    hou.hscript.return_value = (
        "1 [ ] /out/mantra1 \t( 1 5 1 )\n2 [ 1 ] /out/karma1/lopnet/rop_usdrender \t( 1 5 1 )\n",
        "",
    )
    node = hou.node
    hou.node.type().name.return_value = "deadline-cloud"
    mock_parm = hou.Parm
    hou.Parm.node.return_value = node
    hou.Parm.name.return_value = "shadowmap_file"
    hou.node.type().nameWithCategory.return_value = "Driver/ifd"
    hou.hipFile.path.return_value = "/some/path/test.hip"
    hou.node.parm().eval.return_value = "/tmp/foo.$F.exr"

    dir_parm = mock.Mock()
    dir_parm.node.return_value = None
    dir_parm.evalAsString.return_value = "/path/assets/"

    file_parm = mock.Mock()
    file_parm.node.return_value = None
    file_parm.evalAsString.return_value = "/path/asset.png"

    hou.fileReferences.return_value = (
        # These references should be resolved and added as job attachments
        (dir_parm, "$HIP/houdini19.5/"),
        (file_parm, "$HIP/houdini19.5/otls/Deadline-Cloud.hda"),
        # These references should all be skipped based on their reference prefix
        (mock_parm, "opdef:$OS.rat"),
        (mock_parm, "oplib:$OS.rat"),
        (mock_parm, "temp:$OS.rat"),
        (mock_parm, "op:$OS.rat"),
    )
    mock_os = mock.Mock()
    mock_os.path.isdir = lambda path: path.endswith("/")
    mock_os.path.isfile = lambda path: not path.endswith("/")

    with mock.patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini._assets.os", mock_os
    ):
        asset_refs = _get_scene_asset_references(node)

    assert asset_refs.input_filenames == {"/path/asset.png", "/some/path/test.hip"}
    assert asset_refs.input_directories == {"/path/assets/"}
    assert asset_refs.output_directories == set()
