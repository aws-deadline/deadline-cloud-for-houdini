# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""``PdgHost`` over live PDG and Houdini.

Only this module and the registration module import ``hou`` or ``pdg``. Unit
tests cannot import it, so they check it as source.
"""

from __future__ import annotations

from typing import Sequence

import hou  # type: ignore[import-not-found]
import pdg  # type: ignore[import-not-found]

from ..graph import NodeInfo
from ..ports import ItemOutcome, SceneFiles, WorkItemHandle
from ..scene_reads import SceneReads
from ..validation import Verdict


class HoudiniPdgHost:
    """Implements ``PdgHost`` for one scheduler node."""

    def __init__(self, scheduler: "pdg.scheduler.PyScheduler") -> None:
        self._scheduler = scheduler

    def read_graph(self) -> Sequence[NodeInfo]:
        """The topology and the generated items, from one ``dependencyGraph`` read."""
        raise NotImplementedError

    def read_scene(self) -> SceneReads:
        """The scene reads of every step's TOP node."""
        raise NotImplementedError

    def scene_files(self) -> SceneFiles:
        """The saved ``.hip`` and the assets the ROP submitter's scan finds."""
        raise NotImplementedError

    def serialize(self, item: WorkItemHandle) -> str:
        """PDG's own JSON for the work item."""
        raise NotImplementedError

    def notify(self, item: WorkItemHandle, outcome: ItemOutcome) -> None:
        """Report the outcome through PDG's ``onWorkItem*`` callbacks."""
        raise NotImplementedError

    def set_working_dir(self, path: str) -> None:
        """Use ``path`` as both the local and the remote working directory."""
        raise NotImplementedError


class PreCookChecks:
    """The checks that run before a cook starts, from the TOP Cook button or the submit dialog.

    The unsaved ``.hip`` prompt first, then the refusals the topology alone can
    show. With no UI, the prompt becomes a refusal.
    """

    def __init__(self) -> None:
        raise NotImplementedError

    def run(self, node: "hou.Node", *, interactive: bool) -> Verdict:
        """The verdict for starting a cook on ``node``. A refusal or a cancel starts nothing."""
        raise NotImplementedError
