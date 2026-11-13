# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Fixtures for real Windows Houdini GUI tests against a loopback Deadline mock."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from deadline_test_fixtures.deadline_mock import (
    MockDeadlineScenario,
    MockDeadlineServerProcess,
    build_mock_environment,
    write_deadline_config,
)
from deadline_test_fixtures.xa11y import find_accessibility_app

sys.path.insert(0, str(Path(__file__).parent))
from _windows_process import WindowsProcessJob  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(config, items):
    if items and config.getoption("numprocesses", 0):
        raise pytest.UsageError("Houdini GUI tests require --numprocesses=0.")


@pytest.fixture(scope="session")
def mock_server():
    server = MockDeadlineServerProcess(MockDeadlineScenario(response_delay_s=0.3)).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def gui_session(mock_server, tmp_path):
    if sys.platform != "win32":
        pytest.fail("This GUI baseline requires Windows.")
    exe = Path(os.environ["HOUDINI_EXECUTABLE"])
    dependencies = Path(os.environ["HOUDINI_GUI_DEPENDENCIES"])
    assert exe.is_file(), exe
    assert dependencies.is_dir(), dependencies
    reference = REPO / "test/integ/test_scripts/minimal_test/expected_images/test.mantra1.0001.png"
    assert reference.read_bytes().startswith(
        b"\x89PNG\r\n\x1a\n"
    ), "Reference image is missing or is a Git LFS pointer. Run git lfs pull --include='test/integ/**'."
    mock_server.backend.reset()
    config = tmp_path / "deadline-config"
    export = tmp_path / "export"
    export.mkdir()
    write_deadline_config(
        config,
        farm_id=mock_server.scenario.farm_id,
        queue_id=mock_server.scenario.queue_id,
        job_history_dir=tmp_path / "history",
        job_bundle_default_directory=export,
    )
    # Never propagate real AWS/OIDC credentials to the DCC or its children.
    base = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "ACTIONS_ID_TOKEN_"))}
    env = build_mock_environment(
        base,
        deadline_endpoint_url=mock_server.base_url,
        config_path=config,
        home_dir=tmp_path / "home",
    )
    env.update(
        HFS=str(exe.parent.parent),
        PATH=(
            str(Path(sys.executable).parent)
            + os.pathsep
            + str(exe.parent)
            + os.pathsep
            + env.get("PATH", "")
        ),
        HOUDINI_PATH=(
            f"{Path(__file__).parent / '_opener'};" f"{REPO / 'src/deadline/houdini_submitter'};&"
        ),
        HOUDINI_USER_PREF_DIR=str(tmp_path / "prefs-__HVER__"),
        HOUDINI_NO_ENV_FILE="1",
        HOUDINI_NO_SPLASH="1",
        HOUDINI_GUI_REPO=str(REPO),
        HOUDINI_GUI_DEPENDENCIES=str(dependencies),
        HOUDINI_GUI_OUTPUT=str(tmp_path / "scene"),
        HOUDINI_GUI_STATUS=str(tmp_path / "startup.json"),
        HOUDINI_GUI_MOCK_ENDPOINT=mock_server.base_url,
        HYTHON_EXECUTABLE=str(exe.parent / "hython.exe"),
        HOUDINI_GUI_PYTHON_VERSION="3.13",
        QT_ACCESSIBILITY="1",
    )
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    stdout = (tmp_path / "native.stdout.log").open("w", encoding="utf-8")
    stderr = (tmp_path / "native.stderr.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(exe)], cwd=tmp_path, env=env, stdout=stdout, stderr=stderr)
    job = None
    app = None
    try:
        job = WindowsProcessJob(process)
        deadline = time.monotonic() + 180
        status = tmp_path / "startup.json"
        while not status.is_file():
            assert process.poll() is None, f"Houdini exited with {process.returncode}"
            if time.monotonic() > deadline:
                pytest.fail(f"Real Houdini GUI startup timed out; diagnostics: {tmp_path}")
            time.sleep(0.5)
        startup = json.loads(status.read_text())
        assert "error" not in startup, startup
        assert startup["houdini_version"] == "22.0.368", startup
        assert startup["ui_available"] and startup["main_window_visible"], startup
        assert startup["scene_viewer"] and startup["viewport"], startup
        from PIL import Image

        with Image.open(startup["viewport_capture"]) as viewport:
            assert viewport.size == (256, 256), startup
            assert any(high > low for low, high in viewport.convert("RGB").getextrema()), startup
        print(f"Real GUI startup: {startup}")
        app = find_accessibility_app(process.pid, timeout=60)
        env["PYTHONPATH"] = str(Path(__file__).parent / "_python")
        yield app, export, mock_server.backend, tmp_path, env
    finally:
        if app is not None:
            try:
                (tmp_path / "accessibility.txt").write_text(
                    app.dump(max_depth=15), encoding="utf-8"
                )
            except Exception as exc:
                print(f"Final accessibility capture failed: {exc}")
        # Capture the actual interactive desktop before closing the owned job.
        try:
            from PIL import ImageGrab

            ImageGrab.grab(all_screens=True).save(tmp_path / "desktop.png")
        except Exception as exc:
            print(f"Screenshot capture failed: {exc}")
        if job is not None:
            job.close()
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=30)
        stdout.close()
        stderr.close()
