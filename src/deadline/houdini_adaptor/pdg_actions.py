# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The Houdini adaptor's actions for one PDG work item.

A work item's command can be anything: hython, ffmpeg, or montage. So the actions
never branch on the node type. They stage the payload, set the ``PDG_*``
environment, resolve PDG's tokens, run the command, and pass its exit code
through unchanged.

The adaptor does not register these actions yet. This module does not import
``hou``, so it can be tested without Houdini.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Sequence

from openjd.adaptor_runtime_client import PathMappingRule

from deadline.houdini_pdg.ports import Clock, TaskStorage


@dataclass(frozen=True)
class WorkItemPayload:
    """A work item as PDG serialized it."""

    data: Mapping[str, Any]

    @property
    def item_name(self) -> str:
        """``workitem.name``, else ``{nodeName}_{id}``, else the top-level ``name``."""
        raise NotImplementedError

    @property
    def command(self) -> str:
        """``workitem.command``, else ``__pdg_commandstring[0]``, else ``command``."""
        raise NotImplementedError

    def environment(self) -> Dict[str, str]:
        """The attributes flagged ``EnvExport`` and not ``Internal``, formatted as PDG formats them.

        The payload's own ``environment`` field is always empty, so the attributes
        are the only source.
        """
        raise NotImplementedError

    def mapped(self, rules: Sequence[PathMappingRule]) -> "WorkItemPayload":
        """The payload with every string rewritten by the first rule that matches it."""
        raise NotImplementedError


class TokenResolver:
    """Replaces PDG's ``__PDG_*__`` tokens in a command with this worker's values.

    An unknown token is left as is, so the failure it causes names it.
    """

    def __init__(
        self, *, hfs: str, pdg_dir: str, pdg_temp: str, item_name: str, python_version: str
    ) -> None:
        raise NotImplementedError

    def resolve(self, command: str) -> str:
        """The command with every known token replaced. ``__PDG_RESULT_SERVER__`` becomes empty."""
        raise NotImplementedError


class PdgActions:
    """The adaptor actions for PDG work items, keyed ``pdg_*`` so they never collide with the ROP's.

    The adaptor's Houdini session runs many tasks, so each item's environment is
    the session's plus that item's values only.
    """

    def __init__(self, *, storage: TaskStorage, clock: Clock) -> None:
        raise NotImplementedError

    @property
    def action_dict(self) -> Dict[str, Callable[[Dict[str, Any]], None]]:
        """The actions to merge into ``ClientInterface.actions``."""
        raise NotImplementedError

    def stage_payload(self, args: Dict[str, Any]) -> None:
        """Fetch the payload, apply the session's path mapping, and write it where PDG's loader looks."""
        raise NotImplementedError

    def set_environment(self, args: Dict[str, Any]) -> None:
        """Set the ``PDG_*`` variables and the item's exported attributes."""
        raise NotImplementedError

    def run_command(self, args: Dict[str, Any]) -> None:
        """Run the command in ``$PDG_DIR``, streaming its output, and publish its outputs when the step declares none."""
        raise NotImplementedError
