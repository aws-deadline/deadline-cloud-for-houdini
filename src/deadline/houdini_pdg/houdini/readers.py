# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Read live PDG and Houdini objects into plain values, without importing either.

The readers take the objects by duck typing and tolerate any attribute that is
missing or raises, so unit tests drive them with small fakes.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from ..graph import NodeInfo
from ..scene_reads import SceneReads


def node_infos(
    graph_nodes: Iterable[Any],
    dependency_graph: Any,
    cook_set: Optional[Iterable[str]] = None,
) -> Tuple[NodeInfo, ...]:
    """Every TOP node, with its generated work items.

    Membership, wiring, and whether a node is a step come from the topology,
    because generation may not have reached every node yet. A node is a step when
    its ``scriptInfo.command`` is non-empty. ``dependency_graph`` is PDG's
    ``dependencyGraph(expand=True)``, and contributes only the items.
    """
    raise NotImplementedError


def collect_scene_reads(top_nodes: Mapping[str, Any], max_nodes: int = 5_000) -> SceneReads:
    """The file-reference parms each step's scene reads, by step name.

    Follows node-reference parms and input wires. It enters children only inside
    the step's own subtree, never enters another TOP node, and stops after
    ``max_nodes`` nodes per step.
    """
    raise NotImplementedError


def describe_work_item(work_item: Any, attribute_limit: int = 40) -> Dict[str, Any]:
    """A JSON-ready description of a work item, for the debug directory."""
    raise NotImplementedError
