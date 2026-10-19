# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The scheduler settings that the cook uses and the job does not.

The job's settings live in ``PdgSubmitterSettings``. Farm and queue come from the
workstation defaults, as for the ROP.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SchedulerSettings:
    """What the cook reads from the scheduler node."""

    working_dir: str
    #: 0 writes the always-on artifacts. Each level adds to the one below.
    debug_level: int = 0
