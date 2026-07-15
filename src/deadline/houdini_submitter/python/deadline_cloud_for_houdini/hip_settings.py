# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from dataclasses import dataclass, field
from typing import Optional

from .constants import FrameRange, RenderStrategy


@dataclass
class HoudiniSubmitterUISettings:
    """Settings that the submitter UI will use."""

    name: str = field(default="Houdini Submission", metadata={"sticky": True})
    description: str = field(default="", metadata={"sticky": True})

    # Shared "Job Properties" values. The field names below intentionally match the
    # attribute names that deadline-cloud's SharedJobPropertiesWidget reads/writes
    # (priority, initial_status, max_failed_tasks_count, max_retries_per_task); if they
    # don't match, that widget silently falls back to its own defaults.
    priority: int = field(default=50, metadata={"sticky": True})
    initial_status: str = field(default="READY", metadata={"sticky": True})
    max_failed_tasks_count: int = field(default=20, metadata={"sticky": True})
    max_retries_per_task: int = field(default=5, metadata={"sticky": True})

    # Frame range parameters.
    # These parameter names are copied over from the Houdini interface.
    trange: int = field(default=FrameRange.RENDER_CURRENT_FRAME.value, metadata={"sticky": True})
    f1: int = field(default=1, metadata={"sticky": True})
    f2: int = field(default=1, metadata={"sticky": True})
    f3: int = field(default=1, metadata={"sticky": True})

    hip_file: str = field(default="", metadata={"sticky": True})
    render_strategy: str = field(default=RenderStrategy.SEQUENTIAL.value, metadata={"sticky": True})
    separate_steps: bool = field(default=False, metadata={"sticky": True})
    auto_unlock_rops: bool = field(default=False, metadata={"sticky": True})
    auto_parse_hip: bool = field(default=True, metadata={"sticky": True})
    auto_save_hip: bool = field(default=True, metadata={"sticky": True})

    include_adaptor_wheels: bool = field(default=False, metadata={"sticky": True})
    adaptor_wheels_dir: Optional[str] = field(default=None, metadata={"sticky": True})
