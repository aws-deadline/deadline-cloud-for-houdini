# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler settings that the cook uses and the job does not.

The job's settings live in ``PdgSubmitterSettings``. Farm and queue come from the
workstation defaults, as for the ROP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class SchedulerSettings:
    """What the cook reads from the scheduler node."""

    working_dir: str
    #: 0 writes the always-on artifacts. Each level adds to the one below.
    debug_level: int = 0
    #: False turns every graph refusal into a warning, so the cook goes ahead.
    validate_graph: bool = True
    #: The node's extra output folders, absolute. Each task uploads what it writes there.
    output_directories: Tuple[str, ...] = ()
