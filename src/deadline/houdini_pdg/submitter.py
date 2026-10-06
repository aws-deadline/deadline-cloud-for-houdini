# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Submit a cook as one Deadline Cloud job, through the same ``BaseSubmitter`` path as the ROP.

Farm, queue, and queue parameters, including the Conda packages, come from the
same place as the ROP's: the workstation defaults and the node's queue parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from deadline.client.api import (
    BaseSubmitter,
    BaseSubmitterSettings,
    create_job_from_job_bundle,
)
from deadline.client.job_bundle.submission import AssetReferences

from .graph import GraphSnapshot


@dataclass
class PdgSubmitterSettings(BaseSubmitterSettings):
    """The job's settings. The shared fields use the ROP's parm names."""

    # The cook releases each task once its payload is stored, so the job starts
    # suspended. A failed work item fails the cook, so no failure is tolerated.
    initial_status: str = "SUSPENDED"
    max_retries_per_task: int = 1
    max_failed_tasks_count: int = 0
    hip_file: str = ""
    scene_assets: List[str] = field(default_factory=list)
    #: Same names as the ROP's settings, so the shared adaptor-wheels override applies.
    include_adaptor_wheels: bool = False
    adaptor_wheels: str = ""


class PdgSubmitter(BaseSubmitter):
    """Builds and submits the job for one snapshot. Implements ``JobSubmitter``.

    Runs on a PDG thread with no dialog, so it never prompts and reports progress
    through ``log``.
    """

    def __init__(
        self,
        *,
        node: Any,
        working_dir: str,
        snapshot: Optional[GraphSnapshot] = None,
        create_job: Callable[..., Optional[str]] = create_job_from_job_bundle,
        log: Callable[[str], None] = print,
    ) -> None:
        raise NotImplementedError

    def get_settings(self) -> PdgSubmitterSettings:
        """The job settings, read from the scheduler node with the ROP's shared reader."""
        raise NotImplementedError

    def get_job_template(
        self,
        settings: BaseSubmitterSettings,
        host_requirements: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One step per snapshot step, each with an INT ``TaskIndex`` parameter, plus the shared wheels override."""
        raise NotImplementedError

    def get_parameter_values(
        self,
        settings: BaseSubmitterSettings,
        queue_parameters: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """The ``deadline:*`` values and the node's queue parameters. A non-empty caller value wins."""
        raise NotImplementedError

    def get_asset_references(self, settings: BaseSubmitterSettings) -> AssetReferences:
        """The ``.hip``, the scene assets, and the declared inputs of steps with no dependencies.

        A step with dependencies receives its inputs from its upstream steps'
        outputs, so attaching them would ship stale copies.
        """
        raise NotImplementedError

    def submit(self, snapshot: GraphSnapshot, bundle_dir: str) -> str:
        """Write the bundle with the shared writer, create the job, and return its ID.

        Raises :class:`~deadline.houdini_pdg.errors.QuotaExceededError` or
        :class:`~deadline.houdini_pdg.errors.JobSubmissionError`, chained to the cause.
        """
        raise NotImplementedError
