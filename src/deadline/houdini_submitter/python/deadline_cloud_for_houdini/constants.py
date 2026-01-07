# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from enum import Enum


class FrameRange(Enum):
    RENDER_CURRENT_FRAME = 0
    RENDER_FRAME_RANGE = 1
    RENDER_FRAME_RANGE_ONLY_STRICT = 2


_NONE_SELECTED_TEXT = "<none selected>"
_REFRESHING_TEXT = "<refreshing>"


class RenderStrategy(Enum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"
