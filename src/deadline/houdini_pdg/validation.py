# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Decide whether a graph can be cooked, before anything is uploaded.

A cook that fails after ``CreateJob`` has already cost the artist an attachment
upload, and some misconfigurations lose files while every task reports success.
So every unsupported scenario is named, and :func:`validate` reports all of them
at once.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Optional, Tuple

from .graph import GraphSnapshot, NodeInfo
from .scene_reads import ReadReport


class RefusalScenario(Enum):
    """A scenario the scheduler refuses. The value is a stable key for logs and telemetry."""

    DYNAMIC_NODE = "dynamic_node"
    #: An in-process node that writes files a step reads, or reads files a step
    #: writes. Neither reaches the other side without a shared filesystem.
    IN_PROCESS_NODE = "in_process_node"
    RUNTIME_FILE_UNPRODUCED = "runtime_file_unproduced"
    ROP_BATCH = "rop_batch"
    MULTIPLE_SCHEDULERS = "multiple_schedulers"
    HOST_REQUIREMENTS = "host_requirements"
    SERVICE_NODE = "service_node"
    QUOTA_EXCEEDED = "quota_exceeded"
    OUTPUT_OUTSIDE_COOK = "output_outside_cook"
    LOCAL_FILE_UNREACHABLE = "local_file_unreachable"
    ATTRIBUTE_AS_VARIABLE = "attribute_as_variable"
    #: A step PDG should have generated has no items yet, so the graph was read
    #: too early.
    STEP_WITHOUT_ITEMS = "step_without_items"


@dataclass(frozen=True)
class Refusal:
    """One reason the graph cannot be cooked, and what the artist should do."""

    scenario: RefusalScenario
    #: The node, step, parm, or file at fault.
    subject: str
    detail: str
    remedy: str


@dataclass(frozen=True)
class Advisory:
    """Something the artist should know that does not stop the cook."""

    scenario: RefusalScenario
    subject: str
    detail: str


@dataclass(frozen=True)
class Quotas:
    """The job-structure quotas a cook must fit. The defaults are the service's; a farm may have raised them."""

    max_tasks_per_job: int = 10_000
    max_tasks_per_step: int = 10_000
    max_steps_per_job: int = 200
    max_step_dependencies: int = 128
    max_step_consumers: int = 32


@dataclass(frozen=True)
class ValidationContext:
    """Everything validation reads. ``exists`` is its only access to the disk."""

    nodes: Tuple[NodeInfo, ...]
    snapshot: GraphSnapshot
    #: None when the scene walk did not run. The path checks then advise that
    #: they were skipped, instead of passing silently.
    reads: Optional[ReadReport] = None
    working_dir: str = ""
    env: Optional[Mapping[str, str]] = None
    quotas: Quotas = Quotas()
    exists: Callable[[str], bool] = os.path.exists
    scheduler_names: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    """Every refusal and advisory for one graph."""

    refusals: Tuple[Refusal, ...] = ()
    advisories: Tuple[Advisory, ...] = ()

    def accepted(self) -> bool:
        """Whether the graph has no refusals."""
        raise NotImplementedError


def validate(ctx: ValidationContext) -> Verdict:
    """Run every check and return all their findings. Never raises."""
    raise NotImplementedError
