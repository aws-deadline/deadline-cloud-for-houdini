# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Submit through the real GUI to the validating loopback Deadline service."""

import json
import time

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


def _request_gui_action(work, action):
    result = work / "gui-action-result.json"
    result.unlink(missing_ok=True)
    temporary = work / "gui-action.tmp"
    temporary.write_text(json.dumps({"action": action}), encoding="utf-8")
    temporary.replace(work / "gui-action.json")
    return result


def _wait_gui_action(result):
    deadline = time.monotonic() + 45
    while not result.is_file():
        assert time.monotonic() < deadline, f"GUI action timed out: {result}"
        time.sleep(0.1)
    response = json.loads(result.read_text())
    assert "error" not in response, response
    return response


@pytest.mark.parametrize("gui_session", ["lifecycle"], indirect=True)
def test_reopening_and_deleting_rop_releases_dialog(gui_session):
    app, _, backend, work, _ = gui_session
    root = _submitter(app)
    reused = _wait_gui_action(_request_gui_action(work, "reuse"))
    assert reused == {"same_dialog": True, "dialog_count": 1}, reused
    root.wait_visible(timeout=30)
    deleted = _wait_gui_action(_request_gui_action(work, "delete"))
    assert deleted["dialog_released"], deleted
    root.wait_hidden(timeout=30)
    assert backend.call_counts.get("CreateJob", 0) == 0


@pytest.mark.parametrize("gui_session", ["login"], indirect=True)
def test_modeless_rop_login_delivers_background_success(gui_session):
    app, _, backend, work, _ = gui_session
    root = _submitter(app)
    result = _request_gui_action(work, "login")
    login = app.locator(
        'dialog[name="Log in to AWS Deadline Cloud"], window[name="Log in to AWS Deadline Cloud"]'
    )
    login.wait_visible(timeout=30)
    (work / "complete-mock-login").touch()
    completed = _wait_gui_action(result)
    assert completed["login_returned"] and completed["worker_finished"], completed
    assert completed["settings_refresh"] >= 1 and completed["auth_refresh"] >= 1, completed
    login.wait_hidden(timeout=30)
    root.wait_visible(timeout=30)
    assert backend.call_counts.get("CreateJob", 0) == 0


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
    hip_file = job["parameters"]["HipFile"]["path"]
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
    prompts = json.loads((work / "native-prompts.json").read_text())
    assert prompts[-1]["text"] == "Hip file has unsaved changes", prompts
    assert prompts[-1]["buttons"] == ["Save", "Always save", "Ignore", "Cancel"], prompts
    assert prompts[-1]["active_qt_modal_title"] is None, prompts
    (work / "accessibility-unsaved-warning.txt").write_text(
        app.dump(max_depth=15), encoding="utf-8"
    )
    # UIA exposes only the window title for these Houdini-native controls.
    # The live 22.0.368 screenshot verified Cancel as the rightmost button in
    # the bottom row. Click relative to this owned dialog, then require the
    # real displayMessage return value to prove Cancel was chosen.
    from PIL import ImageGrab

    ImageGrab.grab(all_screens=True).save(work / "unsaved-warning.png")
    import win32api
    import win32con
    import win32gui
    import win32process

    windows = []

    def owned_warning(hwnd, _):
        if (
            win32gui.IsWindowVisible(hwnd)
            and win32gui.GetWindowText(hwnd) == "Warning"
            and win32process.GetWindowThreadProcessId(hwnd)[1] == app.pid
        ):
            windows.append(hwnd)

    win32gui.EnumWindows(owned_warning, None)
    assert len(windows) == 1, windows
    hwnd = windows[0]
    win32gui.SetForegroundWindow(hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    point = (int(left + (right - left) * 0.85), int(top + (bottom - top) * 0.84))
    # Use native virtual-desktop coordinates for the NVIDIA/Hyper-V display
    # arrangement; the accessibility input bridge missed this native window.
    win32api.SetCursorPos(point)
    time.sleep(0.25)
    cursor = win32gui.GetCursorPos()
    target = win32gui.GetAncestor(win32gui.WindowFromPoint(cursor), win32con.GA_ROOT)
    (work / "native-warning-click.json").write_text(
        json.dumps(
            {
                "bounds": [left, top, right, bottom],
                "point": point,
                "cursor": cursor,
                "target_is_owned_warning": target == hwnd,
                "foreground_is_owned_warning": win32gui.GetForegroundWindow() == hwnd,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert cursor == point, (cursor, point)
    assert target == hwnd, "Cursor did not reach the owned warning window"
    assert win32gui.GetForegroundWindow() == hwnd, "Qt stole focus from the native warning"
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    try:
        # Allow the native event loop to observe the held button. Back-to-back
        # down/up did not select the visible button in the live runner trial.
        time.sleep(0.5)
    finally:
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
    warning.wait_hidden(timeout=30)
    deadline = time.monotonic() + 10
    while "choice" not in (prompts := json.loads((work / "native-prompts.json").read_text()))[-1]:
        assert time.monotonic() < deadline, prompts
        time.sleep(0.1)
    assert prompts[-1]["choice"] == 3, prompts
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
    prompts = json.loads((work / "native-prompts.json").read_text())
    assert prompts[-1]["title"] == "Missing Input Render Node", prompts
    assert "must have an input ROP" in prompts[-1]["text"], prompts
    (work / "accessibility-missing-input.txt").write_text(app.dump(max_depth=15), encoding="utf-8")
    assert not app.locator('dialog[name^="Deadline Cloud Houdini Submitter"]').exists()
    assert backend.call_counts.get("CreateJob", 0) == 0
    assert not list((work / "history").rglob("template.yaml"))
