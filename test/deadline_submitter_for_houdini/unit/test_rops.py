# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from .mock_hou import hou_module as hou

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import _get_rop_steps


def test_get_rop_steps():
    hou.hscript.return_value = (
        "1 [ ] /out/mantra1 \t( 1 5 1 )\n2 [ 1 ] /out/karma1/lopnet/rop_usdrender \t( 1 5 1 )\n",
        "",
    )
    node = hou.node
    hou.node.type().name.return_value = "deadline"
    steps = _get_rop_steps(node)
    assert len(steps) == 2
    assert steps[0]["id"] == "1"
    assert steps[0]["deps"] == []
    assert steps[0]["name"] == "/out/mantra1-1"
    assert steps[0]["rop"] == "/out/mantra1"
    assert steps[0]["start"] == 1
    assert steps[0]["step"] == 1
    assert steps[0]["stop"] == 5
    assert steps[1]["id"] == "2"
    assert steps[1]["deps"] == ["1"]
    assert steps[1]["name"] == "/out/karma1/lopnet/rop_usdrender-2"
    assert steps[1]["rop"] == "/out/karma1/lopnet/rop_usdrender"
    assert steps[1]["start"] == 1
    assert steps[1]["step"] == 1
    assert steps[1]["stop"] == 5
