# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Cook each test scene that must be refused, and check that the scheduler refused it.

A refusal test passes when the cook logs exactly the scene's expected refusals, the
scheduler node shows the refusal as an error, and the queue has no job from the cook.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytest

# The cook, the job search and the judgment of the cook have their own unit tests, so
# this test only runs them in order.
from .farm import FarmQueue
from .run_cook import CookResult, cancel_jobs_of_refused_cook, refusal_failure, run_cook
from .scenes import SceneKind, TestScene, prepare_scene_file, scene_params


@pytest.mark.pdg
@pytest.mark.parametrize("scene", scene_params(SceneKind.REFUSAL))
def test_refused_scene_creates_no_job(
    scene: TestScene,
    farm_queue: FarmQueue,
    pdg_hython: Path,
    pdg_hython_env: Dict[str, str],
    scenes_dir: Optional[Path],
    test_started_at: datetime,
    tmp_path: Path,
) -> None:
    """The scheduler refuses the scene for exactly the expected reasons, shows the refusal
    on its node, and the cook makes no job.

    This catches a refusal check that stops refusing, or refuses for another reason, and a
    refusal that still submits a job.
    """
    # The scene's cook, in its own folder. A refused cook needs no worker wheels.
    hip: Optional[Path] = prepare_scene_file(
        scene, hython=pdg_hython, env=pdg_hython_env, scenes_dir=scenes_dir, into=tmp_path
    )
    result: CookResult = run_cook(
        scene, hip, hython=pdg_hython, env=pdg_hython_env, wheels=None, folder=tmp_path
    )

    # A job from this cook would run on a shared queue, so it is canceled before the test fails.
    canceled: List[str] = cancel_jobs_of_refused_cook(
        farm_queue, result, test_started_at=test_started_at
    )

    # The jobs, the refusals the cook logged, and the error on the scheduler node.
    failure: str = refusal_failure(scene, result, canceled_jobs=canceled)
    assert not failure, failure
