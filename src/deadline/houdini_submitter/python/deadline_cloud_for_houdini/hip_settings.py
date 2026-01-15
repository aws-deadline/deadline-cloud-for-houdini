# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from dataclasses import dataclass, field
from typing import Optional

from .constants import FrameRange, RenderStrategy


@dataclass
class HoudiniSubmitterUISettings:
    """Settings that the submitter UI will use."""

    name: str = field(default="Houdini Submission", metadata={"sticky": True})
    description: str = field(default="", metadata={"sticky": True})

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
