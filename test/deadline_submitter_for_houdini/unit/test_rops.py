# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from typing import Optional
from unittest.mock import Mock, patch

import pytest

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _get_render_strategy_for_node,
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
    node.parm.return_value = None

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

    def mock_parm(name: str):
        if name == "initsim":
            parm = Mock()
            parm.eval.return_value = 1
            return parm
        else:
            return None

    node.parm = mock_parm

    steps = _get_rop_steps(node)

    assert len(steps) == 1
    assert steps[0]["id"] == "1"
    assert steps[0]["render_strategy"] == RenderStrategy.SEQUENTIAL


@pytest.mark.parametrize(
    "category,initsim,override,expected_render_strategy",
    [
        ("Driver/material", 0, "", RenderStrategy.PARALLEL),
        ("Driver/geometry", 0, "", RenderStrategy.PARALLEL),
        ("Driver/geometry", 1, "", RenderStrategy.SEQUENTIAL),
        ("Driver/geometry", 1, None, RenderStrategy.SEQUENTIAL),
        ("Driver/geometry", 1, "PARALLEL", RenderStrategy.PARALLEL),
        ("Driver/material", None, None, RenderStrategy.PARALLEL),
        ("Driver/material", None, "", RenderStrategy.PARALLEL),
        ("Driver/material", None, "SEQUENTIAL", RenderStrategy.SEQUENTIAL),
    ],
)
def test_get_render_strategy_for_node(
    category: str,
    initsim: Optional[int],
    override: Optional[Mock],
    expected_render_strategy: RenderStrategy,
):
    node = Mock()
    node.type.return_value = Mock()
    node.type.return_value.nameWithCategory.return_value = category

    def mock_parm(name: str):
        result = Mock()
        if name == "initsim":
            result.eval.return_value = initsim
            result.evalAsString.return_value = str(initsim)
        elif name == "deadline_cloud_render_strategy" and override:
            result.eval.return_value = override
            result.evalAsString.return_value = override
        else:
            result = None
        return result

    node.parm = mock_parm

    assert _get_render_strategy_for_node(node) == expected_render_strategy


def test_get_render_strategy_for_node_raises_exception_for_invalid_strategy():
    node = Mock()
    node.parm.return_value.evalAsString.return_value = "not a valid strategy"

    with pytest.raises(Exception):
        _get_render_strategy_for_node(node)
