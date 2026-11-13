# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Builds the minimal cook scene with the scheduler node's host requirements set to Linux.

Every step of the job must then carry that requirement. Runs inside hython.
"""

from __future__ import annotations

import hou  # type: ignore[import-not-found]
from _build import SCHEDULER_TYPE, main
from minimal_cook import build as build_minimal_cook


def build() -> hou.Node:
    """The minimal cook scene, with workers limited to Linux on the Host Requirements tab."""
    topnet: hou.Node = build_minimal_cook()
    scheduler: hou.Node = next(c for c in topnet.children() if c.type().name() == SCHEDULER_TYPE)
    # Run On is a menu, so it is set by the position of its "custom" entry.
    run_on: hou.Parm = scheduler.parm("hr_mode")
    run_on.set(run_on.parmTemplate().menuItems().index("custom"))
    scheduler.parm("hr_os").set("linux")
    return topnet


if __name__ == "__main__":
    main(build)
