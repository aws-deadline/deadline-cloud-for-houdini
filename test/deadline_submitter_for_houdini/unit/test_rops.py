# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from unittest.mock import Mock, patch

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _get_rop_steps,
    RenderStrategy,
)


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.hou")
def test_get_rop_steps(mock_hou):
    mock_hou.hscript.return_value = (
        "1 [ ] /out/mantra1 \t( 1 5 1 )\n2 [ 1 ] /out/karma1/lopnet/rop_usdrender \t( 1 5 1 )\n",
        "",
    )
    node = mock_hou.node()
    steps = _get_rop_steps(node)
    assert len(steps) == 2
    assert steps[0]["id"] == "1"
    assert steps[0]["dependency_ids"] == []
    assert steps[0]["name"] == "/out/mantra1-1"
    assert steps[0]["rop"] == "/out/mantra1"
    assert steps[0]["start"] == 1
    assert steps[0]["end"] == 5
    assert steps[0]["step"] == 1
    assert steps[0]["render_strategy"] == RenderStrategy.PARALLEL

    assert steps[1]["id"] == "2"
    assert steps[1]["dependency_ids"] == ["1"]
    assert steps[1]["name"] == "/out/karma1/lopnet/rop_usdrender-2"
    assert steps[1]["rop"] == "/out/karma1/lopnet/rop_usdrender"
    assert steps[1]["start"] == 1
    assert steps[1]["end"] == 5
    assert steps[1]["step"] == 1
    assert steps[1]["render_strategy"] == RenderStrategy.PARALLEL


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.hou")
def test_get_rop_steps_simulation(mock_hou):
    mock_hou.hscript.return_value = (
        "1 [ ] /out/geo \t( 1 5 1 )\n",
        "",
    )

    node = mock_hou.node.return_value
    node.type.return_value = Mock()
    node.type.return_value.name.return_value = "geo"
    node.type.return_value.nameWithCategory.return_value = "Driver/geometry"
    node.parm.return_value.eval.return_value = 1

    steps = _get_rop_steps(node)

    assert len(steps) == 1
    assert steps[0]["id"] == "1"
    assert steps[0]["render_strategy"] == RenderStrategy.SEQUENTIAL
