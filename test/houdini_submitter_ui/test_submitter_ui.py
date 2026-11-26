# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Real submitter export smoke; selectors must be verified on the live desktop."""

import json
import subprocess
import time

import pytest
import yaml
from deadline_test_fixtures.xa11y import SharedSubmitterDialog
from deadline_test_fixtures.xa11y.controls import (
    TAB_JOB_SPECIFIC,
    set_checkbox,
    set_job_name,
    set_max_failed_tasks,
    set_max_retries,
    set_priority,
    switch_to_tab,
)
from deadline_test_fixtures.job_bundle import find_complete_job_bundle

from test.integ.helpers.image_comparison import assert_all_images_close


@pytest.mark.parametrize("custom", [False, True], ids=["defaults", "edited-settings"])
def test_submitter_exports_and_renders_minimal_scene(gui_session, custom):
    app, export, backend, work, env = gui_session
    # Preserve the unmodified tree before selecting a production UI control.
    (work / "accessibility-before.txt").write_text(app.dump(max_depth=15), encoding="utf-8")
    root = app.locator('dialog[name^="Deadline Cloud Houdini Submitter"]')
    root.wait_visible(timeout=60)
    if custom:
        set_job_name(root, "GUI edited scene")
        set_priority(root, 51)
        set_max_failed_tasks(root, 7)
        set_max_retries(root, 2)
        switch_to_tab(root, TAB_JOB_SPECIFIC)
        for name, checked in (
            ("Submit Dependencies as Separate Steps", False),
            ("Automatically unlock ROPs", True),
            ("Automatically parse scene (.hip) references", True),
            ("Automatically save scene (.hip) file", True),
        ):
            set_checkbox(root, name, checked)
        (work / "accessibility-edited.txt").write_text(app.dump(max_depth=15), encoding="utf-8")
    dialog = SharedSubmitterDialog(app)
    dialog.save_bundle_locally(timeout=90)
    deadline = time.monotonic() + 60
    bundle = None
    while time.monotonic() < deadline:
        bundle = find_complete_job_bundle(export)
        if bundle is not None:
            break
        time.sleep(0.5)
    assert bundle is not None, f"No complete exported bundle under {export}"
    for name in ("template.yaml", "parameter_values.yaml", "asset_references.yaml"):
        assert (bundle / name).is_file(), name
    parameters = {
        item["name"]: item["value"]
        for item in yaml.safe_load((bundle / "parameter_values.yaml").read_text())[
            "parameterValues"
        ]
    }
    assert parameters["deadline:priority"] == (51 if custom else 50)
    assert parameters["deadline:maxFailedTasksCount"] == (7 if custom else 20)
    assert parameters["deadline:maxRetriesPerTask"] == (2 if custom else 5)
    assert parameters["HipFile"].replace("\\", "/") == (work / "scene/test.hip").as_posix()
    template = yaml.safe_load((bundle / "template.yaml").read_text())
    assert template["name"] == ("GUI edited scene" if custom else "test")
    assets = yaml.safe_load((bundle / "asset_references.yaml").read_text())["assetReferences"]
    assert parameters["HipFile"] in assets["inputs"]["filenames"]
    counts = dict(backend.call_counts)
    (work / "mock-traffic.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
    assert backend.unmatched_requests == [], backend.unmatched_requests
    assert counts.get("ListFarms", 0) >= 1, counts
    assert counts.get("ListQueueEnvironments", 0) >= 1, counts
    assert any(counts.get(operation, 0) for operation in ("ListQueues", "GetQueue")), counts
    assert counts.get("CreateJob", 0) == 0, counts
    # Render precisely the exported GUI bundle, using its values and every step.
    # This is local OpenJD execution; it does not create a Deadline job.
    job_parameters = {
        key: value for key, value in parameters.items() if not key.startswith("deadline:")
    }
    for index, step in enumerate(template["steps"]):
        result = subprocess.run(
            [
                "openjd",
                "run",
                str(bundle / "template.yaml"),
                "--step",
                step["name"],
                "--job-param",
                json.dumps(job_parameters),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        (work / f"local-render-{index}.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        assert result.returncode == 0, result.stdout + result.stderr
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    assert_all_images_close(
        repo / "test/integ/test_scripts/minimal_test/expected_images", work / "scene/render"
    )
