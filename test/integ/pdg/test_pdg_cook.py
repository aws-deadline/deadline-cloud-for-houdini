# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Cook each test scene that must cook on a Deadline Cloud farm, and check it end to end.

A cook passes when it ends within its time limit with the expected outcome and steps,
and the farm agrees with the cook, and with PDG, about every task.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

from .compare_cook_with_farm import CookFarmComparison, CookRecord, compare_cook_with_farm
from .compare_cook_with_farm import format_comparison, read_cook_record
from .farm import FarmQueue, cancel_job_of_cook, read_farm_view
from .run_cook import CookResult, describe_cook, run_cook
from .scenes import ExpectedStep, SceneKind, TestScene, prepare_scene_file, scene_params


@pytest.mark.pdg
@pytest.mark.parametrize("scene", scene_params(SceneKind.COOK))
def test_scene_cooks_end_to_end(
    scene: TestScene,
    farm_queue: FarmQueue,
    pdg_hython: Path,
    pdg_hython_env: Dict[str, str],
    worker_wheels: Path,
    scenes_dir: Optional[Path],
    tmp_path: Path,
) -> None:
    """The scene cooks as one job, and the cook, PDG and the farm agree on every task.

    This catches a cook that refuses or fails a scene it should run, a job whose shape
    differs from the graph, and a task whose result PDG got wrong or never got.
    """
    # The scene's cook, in its own folder, so its debug directory is the only one there.
    hip: Optional[Path] = prepare_scene_file(
        scene, hython=pdg_hython, env=pdg_hython_env, scenes_dir=scenes_dir, into=tmp_path
    )
    result: CookResult = run_cook(
        scene, hip, hython=pdg_hython, env=pdg_hython_env, wheels=worker_wheels, folder=tmp_path
    )
    context: str = describe_cook(scene, result)

    # A cook that ran out of time leaves its job running, so the job is canceled.
    if result.timed_out:
        cancel_job_of_cook(farm_queue, result.debug_dir)
        pytest.fail(f"The cook ran past {scene.max_minutes:g} min. {context}")
    assert result.debug_dir is not None, f"The cook wrote no debug directory. {context}"

    # What the cook says happened.
    cook: CookRecord = read_cook_record(result.debug_dir)
    assert cook.cook_json.get("outcome") == scene.expected_outcome, context
    actual_steps: Tuple[ExpectedStep, ...] = tuple(
        ExpectedStep(s["name"], s["task_count"], s["declares_outputs"])
        for s in cook.snapshot["steps"]
    )
    assert actual_steps == scene.expected_steps, context

    # What the farm says happened, task by task, against the cook and PDG.
    comparison: CookFarmComparison = compare_cook_with_farm(
        cook,
        read_farm_view(farm_queue, cook.cook_json["job_id"]),
        pdg_work_items=result.pdg_work_items,
        expected_host_requirements=scene.expected_host_requirements,
    )
    assert comparison.passed, f"{format_comparison(comparison)}\n{context}"
