# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Cook one test scene with the Deadline Cloud Scheduler node, and record what happened.

Runs inside hython, which ``run_cook`` starts::

    hython cook_scene_in_hython.py (--hip SCENE | --build BUILDER) --result OUT.json
                                   --job-name NAME [--cook-node PATH] [--wheels DIR]
                                   [--debug-level N]

It starts the cook the way the TOP Cook button does, with ``cookWorkItems(block=True)``,
which returns once the cook has ended. Then it writes ``cook_result.json``: what pytest
needs and can only read inside hython. That is PDG's work items, the scheduler node's
errors, the refusal lines the cook logged, and the cook's debug directory.

The farm and queue come from the Deadline config that ``DEADLINE_CONFIG_FILE_PATH``
names. The scene's changes to its scheduler node are not saved, and workers load the
saved scene, which doesn't use that node.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# The version of cook_result.json's format.
RESULT_VERSION: int = 1

# The logger every refusal line goes through, from the scheduler and from the cook.
_PACKAGE_LOGGER: str = "deadline.houdini_pdg"

# The format of a refusal line. Its arguments are the subject, scenario, detail and remedy.
_REFUSAL_LINE: str = "Refused, %s (%s): %s %s"

# The type name of the Deadline Cloud Scheduler node.
_SCHEDULER_TYPE: str = "deadlinecloudscheduler"

# Each cook writes its debug directory in this folder of its working directory.
_DEBUG_DIR_NAME: str = "pdg_debug"


class RefusalLineHandler(logging.Handler):
    """Collects the scenario and subject of every refusal line the cook logs.

    They come from the record's arguments, not its text, because a subject can hold
    parentheses.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.refusals: List[Dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep a refusal line's scenario and subject. Other lines are ignored."""
        if record.msg == _REFUSAL_LINE and isinstance(record.args, tuple):
            subject: Any = record.args[0]
            scenario: Any = record.args[1]
            self.refusals.append({"scenario": str(scenario), "subject": str(subject)})


def main(argv: Sequence[str]) -> int:
    """Cook the scene and write cook_result.json. Returns 0 once the file is written."""
    args: argparse.Namespace = _parse_args(argv)
    refusal_lines: RefusalLineHandler = capture_refusal_lines()

    # The scene, its network and its scheduler node, set up for this cook.
    hip_file: str = _open_scene(args)
    import hou  # type: ignore[import-not-found]

    topnet: Any = _find_topnet(args.cook_node)
    cook_node: Any = hou.node(args.cook_node) if args.cook_node else topnet.displayNode()
    scheduler: Any = find_or_create_scheduler(topnet)
    set_scheduler_parms(
        scheduler, job_name=args.job_name, debug_level=args.debug_level, wheels=args.wheels
    )

    # The Cook button's path, which returns once the cook has ended.
    started_at: str = _utc_now()
    start_refused: bool = _cook(cook_node)
    returned_at: str = _utc_now()

    # Everything the test reads after the cook, written in one file.
    debug_dir: Optional[str] = find_debug_dir(scheduler.parm("working_dir").eval())
    result: Dict[str, Any] = {
        "version": RESULT_VERSION,
        "hip_file": hip_file,
        "job_name": args.job_name,
        "cook_node": cook_node.path(),
        "started_at": started_at,
        "returned_at": returned_at,
        "start_refused": start_refused,
        "debug_dir": debug_dir,
        "logged_refusals": refusal_lines.refusals,
        "scheduler_errors": list(scheduler.errors()),
        "pdg_work_items": read_pdg_work_items(cook_node),
    }
    Path(args.result).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


def _cook(cook_node: Any) -> bool:
    """Cook the node and its inputs, blocking. Returns True when PDG raised at the cook's start.

    PDG raises when the scheduler refuses at the cook's start, because it then returns False.
    """
    import hou

    try:
        cook_node.cookWorkItems(block=True)
    except hou.OperationFailed:
        return True
    return False


def capture_refusal_lines() -> RefusalLineHandler:
    """A handler, added to the package's logger, that collects every refusal line from now on."""
    handler: RefusalLineHandler = RefusalLineHandler()
    logging.getLogger(_PACKAGE_LOGGER).addHandler(handler)
    return handler


def find_or_create_scheduler(topnet: Any) -> Any:
    """The network's Deadline Cloud Scheduler node, made its default scheduler.

    A scene with none gets a new one. Some scenes hold only a Local Scheduler, or a
    scheduler node whose type isn't installed.
    """
    # The first one, when the network has any, so a scene's own settings are kept.
    existing: List[Any] = [c for c in topnet.children() if c.type().name() == _SCHEDULER_TYPE]
    scheduler: Any = existing[0] if existing else topnet.createNode(_SCHEDULER_TYPE)
    topnet.parm("topscheduler").set(scheduler.name())
    return scheduler


def set_scheduler_parms(scheduler: Any, *, job_name: str, debug_level: int, wheels: str) -> None:
    """Set the job's name and the debug level, and, when ``wheels`` names a folder, have the
    workers install the wheels in it. The changes are not saved."""
    scheduler.parm("name").set(job_name)
    scheduler.parm("debug_level").set(debug_level)
    if wheels:
        scheduler.parm("include_adaptor_wheels").set(1)
        scheduler.parm("adaptor_wheels").set(wheels)


def read_pdg_work_items(cook_node: Any) -> List[Dict[str, Any]]:
    """Every work item of every node in the cook node's graph, with its state and outputs.

    Each output path is localized, so tokens such as ``__PDG_DIR__`` become the local path.
    """
    items: List[Dict[str, Any]] = []
    for pdg_node in cook_node.getPDGGraphContext().graph.nodes():
        for work_item in pdg_node.workItems:
            items.append(
                {
                    "node": pdg_node.name,
                    "name": work_item.name,
                    "index": work_item.index,
                    "state": _enum_member(work_item.state),
                    "outputs": [
                        {"path": work_item.localizePath(f.path), "tag": f.tag}
                        for f in work_item.outputFiles
                    ],
                }
            )
    return items


def find_debug_dir(working_dir: str) -> Optional[str]:
    """The one cook's debug directory under ``working_dir``, or None when the cook made none.

    Each test scene cooks in its own empty folder, so a second directory means the scene's
    working directory is outside that folder, and the test can't tell which one is its own.
    """
    # Each folder under pdg_debug/ is one cook's.
    debug_root: Path = Path(working_dir) / _DEBUG_DIR_NAME
    found: List[Path] = sorted(p for p in debug_root.glob("*") if p.is_dir())
    if len(found) > 1:
        raise ValueError(
            f"{debug_root} holds {len(found)} cooks' debug directories. Set the scheduler "
            "node's working directory to the scene's folder."
        )
    return str(found[0]) if found else None


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """The command line in the module docstring."""
    # The scene: a saved file, or a builder to run without saving.
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="cook_scene_in_hython.py")
    scene: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group(required=True)
    scene.add_argument("--hip", help="the saved scene to load")
    scene.add_argument("--build", help="a scene builder to run here, leaving the scene unsaved")

    # Where the result goes, and the scheduler node's settings for this cook.
    parser.add_argument("--result", required=True, help="where to write cook_result.json")
    parser.add_argument("--job-name", required=True, help="the job's name on the farm")
    parser.add_argument(
        "--cook-node", default="", help="the node to cook; the display node by default"
    )
    parser.add_argument("--wheels", default="", help="the folder of wheels the workers install")
    parser.add_argument("--debug-level", type=int, default=0, help="the scheduler's debug level")
    return parser.parse_args(argv)


def _open_scene(args: argparse.Namespace) -> str:
    """Load the saved scene, or build an unsaved one here. Returns the .hip path, or ``""``."""
    import hou

    if args.hip:
        hou.hipFile.load(args.hip, suppress_save_prompt=True, ignore_load_warnings=True)
        return str(args.hip)
    # A builder imports its helpers from its own folder.
    sys.path.insert(0, os.path.dirname(os.path.abspath(args.build)))
    runpy.run_path(args.build)["build"]()
    return ""


def _find_topnet(cook_node: str) -> Any:
    """The cook node's TOP network, or, when no node is named, the scene's only TOP network
    with work in it.

    A scene saved from Houdini's UI also has /tasks/topnet1, which holds only a scheduler.
    """
    import hou

    if cook_node:
        return hou.node(cook_node).parent()
    # A TOP network holds TOP nodes and is not itself one, as a TOP subnet would be.
    topnets: List[Any] = [
        n
        for n in hou.node("/").allSubChildren()
        if n.childTypeCategory() == hou.topNodeTypeCategory()
        and n.type().category() != hou.topNodeTypeCategory()
        and any(not child.isScheduler() for child in n.children())
    ]
    if len(topnets) != 1:
        raise ValueError(
            f"The scene has {len(topnets)} TOP networks with work in them. Name the node to "
            "cook as the scene's cook_node."
        )
    return topnets[0]


def _enum_member(value: Any) -> str:
    """A PDG enum value's member name: ``CookedSuccess`` for ``workItemState.CookedSuccess``."""
    return str(value).rsplit(".", 1)[-1]


def _utc_now() -> str:
    """The time now, in UTC, as ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
