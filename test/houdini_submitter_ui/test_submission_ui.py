# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Submit through the real GUI to the validating loopback Deadline service."""

import json

import pytest
import yaml
from deadline_test_fixtures.xa11y.controls import (
    TAB_JOB_SPECIFIC,
    set_checkbox,
    set_job_name,
    set_max_failed_tasks,
    set_max_retries,
    set_priority,
    switch_to_tab,
)


def _submitter(app):
    root = app.locator('dialog[name^="Deadline Cloud Houdini Submitter"]')
    root.wait_visible(timeout=60)
    root.descendant('button[name="Submit"]').wait_enabled(timeout=60)
    return root


def _submit(root):
    button = root.descendant('button[name="Submit"]')
    button.wait_enabled(timeout=60)
    button.press()


def _finish_and_verify(gui_session, root, *, custom=False):
    app, _, backend, work, _ = gui_session
    progress = app.locator('dialog[name="AWS Deadline Cloud submission"]')
    progress.wait_visible(timeout=90)
    progress.descendant('static_text[name="Submission complete"]').wait_visible(timeout=90)
    snapshot = backend.snapshot()
    (work / "mock-submission.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (work / "accessibility-submitted.txt").write_text(app.dump(max_depth=15), encoding="utf-8")
    assert snapshot["unmatched_requests"] == [], snapshot
    assert snapshot["call_counts"]["CreateJob"] == 1, snapshot
    assert snapshot["call_counts"]["GetJob"] >= 1, snapshot
    jobs = snapshot["resources"]["jobs"]
    assert len(jobs) == 1, jobs
    job = jobs[0]
    assert job["farmId"] == backend.farm_id
    assert job["queueId"] == backend.queue_id
    assert job["lifecycleStatus"] == "CREATE_COMPLETE"
    assert job["priority"] == (51 if custom else 50)
    assert job["maxFailedTasksCount"] == (7 if custom else 20)
    assert job["maxRetriesPerTask"] == (2 if custom else 5)
    assert not job.get("attachments"), job
    assert job["templateType"] == "YAML"
    template = yaml.safe_load(job["template"])
    assert template["name"] == ("GUI mock submission" if custom else "test")
    assert template["steps"], template
    hip_file = job["parameters"]["HipFile"]["string"]
    assert hip_file.replace("\\", "/") == (work / "scene/test.hip").as_posix()
    # The mock validates both the OpenJD template and the submitted parameter
    # values before returning CREATE_COMPLETE. Also compare with the actual
    # history bundle produced by the GUI, including its asset references.
    bundles = list((work / "history").rglob("template.yaml"))
    assert bundles, f"No submission history bundle under {work}"
    assert any(yaml.safe_load(path.read_text()) == template for path in bundles)
    assets = list((work / "history").rglob("asset_references.yaml"))
    assert assets
    assert any(
        hip_file in yaml.safe_load(path.read_text())["assetReferences"]["inputs"]["filenames"]
        for path in assets
    )
    progress.wait_until(
        lambda element: element is not None and job["jobId"] in element.dump(), timeout=60
    )
    progress.descendant('button[name="OK"]').press()
    progress.wait_hidden(timeout=30)
    root.wait_hidden(timeout=30)


@pytest.mark.parametrize("gui_session", ["submit"], indirect=True)
@pytest.mark.parametrize("custom", [False, True], ids=["defaults", "edited-settings"])
def test_submit_creates_mock_job_and_history_bundle(gui_session, custom):
    app, _, _, work, _ = gui_session
    root = _submitter(app)
    if custom:
        set_job_name(root, "GUI mock submission")
        set_priority(root, 51)
        set_max_failed_tasks(root, 7)
        set_max_retries(root, 2)
    (work / "accessibility-before-submit.txt").write_text(app.dump(max_depth=15), encoding="utf-8")
    _submit(root)
    _finish_and_verify(gui_session, root, custom=custom)


@pytest.mark.parametrize("gui_session", ["unsaved"], indirect=True)
def test_unsaved_scene_cancel_then_retry(gui_session):
    app, _, backend, work, _ = gui_session
    assert json.loads((work / "startup.json").read_text())["unsaved_changes"]
    root = _submitter(app)
    switch_to_tab(root, TAB_JOB_SPECIFIC)
    set_checkbox(root, "Automatically save scene (.hip) file", False)
    _submit(root)
    warning = app.locator('dialog[name="Warning"], window[name="Warning"]')
    warning.wait_visible(timeout=30)
    warning.wait_until(
        lambda element: element is not None and "Hip file has unsaved changes" in element.dump(),
        timeout=30,
    )
    (work / "accessibility-unsaved-warning.txt").write_text(
        app.dump(max_depth=15), encoding="utf-8"
    )
    warning.descendant('button[name="Cancel"]').press()
    warning.wait_hidden(timeout=30)
    root.wait_visible(timeout=30)
    assert backend.call_counts.get("CreateJob", 0) == 0
    assert not list((work / "history").rglob("template.yaml"))
    set_checkbox(root, "Automatically save scene (.hip) file", True)
    _submit(root)
    _finish_and_verify(gui_session, root)


@pytest.mark.parametrize("gui_session", ["missing-input"], indirect=True)
def test_missing_input_rop_blocks_submitter(gui_session):
    app, _, backend, work, _ = gui_session
    warning = app.locator(
        'dialog[name="Missing Input Render Node"], window[name="Missing Input Render Node"]'
    )
    warning.wait_visible(timeout=30)
    warning.wait_until(
        lambda element: element is not None and "must have an input ROP" in element.dump(),
        timeout=30,
    )
    (work / "accessibility-missing-input.txt").write_text(app.dump(max_depth=15), encoding="utf-8")
    assert not app.locator('dialog[name^="Deadline Cloud Houdini Submitter"]').exists()
    assert backend.call_counts.get("CreateJob", 0) == 0
    assert not list((work / "history").rglob("template.yaml"))
