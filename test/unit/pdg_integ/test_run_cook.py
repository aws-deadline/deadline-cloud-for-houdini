# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for starting a cook's hython process, reading back what it recorded, and describing
it in a failure message, with no Houdini.

The tests that start a process use a stand-in for hython: a small executable script that
writes a result file, fails, or hangs. They need a POSIX shell to run it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from deadline.houdini_pdg.ports import OutputFile
from test.integ.pdg.compare_cook_with_farm import PdgWorkItem
from test.integ.pdg.run_cook import CookResult, LoggedRefusal, describe_cook, read_cook_result
from test.integ.pdg.run_cook import run_cook
from test.integ.pdg.scenes import ExpectedStep, SceneKind, SceneSet, TestScene

_POSIX_ONLY: pytest.MarkDecorator = pytest.mark.skipif(
    os.name == "nt", reason="the hython stand-in is a POSIX script"
)

# What a cook script writes for a cook that ran: one work item with one tagged output.
_RESULT: Dict[str, Any] = {
    "version": 1,
    "hip_file": "/cooks/pyro/pyro.hip",
    "job_name": "pdg-integ-pyro-0a1b2c3d",
    "cook_node": "/obj/topnet1/render",
    "started_at": "2026-11-10T17:09:46+00:00",
    "returned_at": "2026-11-10T17:25:32+00:00",
    "start_refused": False,
    "debug_dir": "/cooks/pyro/pdg_debug/2026-11-10-17-09-52-1ad65a51",
    "logged_refusals": [{"scenario": "rop_batch", "subject": "rop (batch)"}],
    "scheduler_errors": ["The graph was refused"],
    "pdg_work_items": [
        {
            "node": "render",
            "name": "render_3",
            "index": 0,
            "state": "CookedSuccess",
            "outputs": [{"path": "/cooks/pyro/render/a.png", "tag": "file/image"}],
        }
    ],
}


def _scene(**changes: Any) -> TestScene:
    """A saved builder cook scene, with any field replaced."""
    scene: TestScene = TestScene(
        scene_id="pyro",
        kind=SceneKind.COOK,
        scene_set=SceneSet.SMOKE,
        builder="pyro.py",
        external=None,
        sha256=None,
        saved=True,
        cook_node=None,
        requires=(),
        max_minutes=1.0,
        expected_outcome="succeeded",
        expected_steps=(ExpectedStep("render", 1, True),),
        expected_refusals=(),
        expected_host_requirements=None,
    )
    return replace(scene, **changes)


def _fake_hython(tmp_path: Path, body: str) -> Path:
    """An executable that stands in for hython: Python running ``body`` with the cook's arguments.

    The arguments it gets are saved to ``arguments.json`` next to it.
    """
    # It records its arguments first, so a test can check them even when ``body`` fails.
    script: Path = tmp_path / "hython"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "here = os.path.dirname(os.path.abspath(sys.argv[0]))\n"
        "json.dump(sys.argv[1:], open(os.path.join(here, 'arguments.json'), 'w'))\n"
        f"{body}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


# Writes the canned result, with the job name the cook was given.
_WRITES_RESULT: str = (
    "args = sys.argv[1:]\n"
    "result = json.loads(os.environ['CANNED_RESULT'])\n"
    "result['job_name'] = args[args.index('--job-name') + 1]\n"
    "open(args[args.index('--result') + 1], 'w').write(json.dumps(result))\n"
)


def test_a_result_file_is_read_into_a_cook_result(tmp_path: Path) -> None:
    """Every field the tests read comes from the file, with PDG's outputs as OutputFiles."""
    # The canned file of a cook that ran, read back.
    path: Path = tmp_path / "cook_result.json"
    path.write_text(json.dumps(_RESULT), encoding="utf-8")

    result: CookResult = read_cook_result(path, hython_log=tmp_path / "hython.log")

    assert result == CookResult(
        hip_file=Path("/cooks/pyro/pyro.hip"),
        job_name="pdg-integ-pyro-0a1b2c3d",
        started_at=datetime(2026, 11, 10, 17, 9, 46, tzinfo=timezone.utc),
        returned_at=datetime(2026, 11, 10, 17, 25, 32, tzinfo=timezone.utc),
        timed_out=False,
        start_refused=False,
        debug_dir=Path("/cooks/pyro/pdg_debug/2026-11-10-17-09-52-1ad65a51"),
        logged_refusals=(LoggedRefusal("rop_batch", "rop (batch)"),),
        scheduler_errors=("The graph was refused",),
        pdg_work_items=(
            PdgWorkItem(
                node="render",
                name="render_3",
                index=0,
                state="CookedSuccess",
                outputs=(OutputFile("/cooks/pyro/render/a.png", "file/image"),),
            ),
        ),
        hython_log=tmp_path / "hython.log",
    )


def test_a_cook_refused_at_its_start_has_no_scene_file_or_debug_directory(tmp_path: Path) -> None:
    """An unsaved scene records "" as its file, and a refusal at the start writes no debug directory."""
    # What the cook script writes for an unsaved scene refused at the cook's start.
    path: Path = tmp_path / "cook_result.json"
    path.write_text(
        json.dumps({**_RESULT, "hip_file": "", "debug_dir": None, "start_refused": True}),
        encoding="utf-8",
    )
    result: CookResult = read_cook_result(path, hython_log=tmp_path / "hython.log")
    assert (result.hip_file, result.debug_dir, result.start_refused) == (None, None, True)


def test_a_result_of_another_format_version_is_refused(tmp_path: Path) -> None:
    """A file written by another version of the cook script is not read wrongly."""
    path: Path = tmp_path / "cook_result.json"
    path.write_text(json.dumps({**_RESULT, "version": 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="format version 2"):
        read_cook_result(path, hython_log=tmp_path / "hython.log")


def test_a_failure_message_names_the_job_what_the_workers_need_and_what_the_cook_logged(
    tmp_path: Path,
) -> None:
    """A failed cook test says which job to look at, what the farm's workers must have, and
    which refusals and scheduler errors the cook logged, so the reader can tell a farm
    without ImageMagick from a scheduler bug without opening the logs.
    """
    result: CookResult = read_cook_result(
        _write_result(tmp_path, {**_RESULT, "scheduler_errors": ["first", "second"]}),
        hython_log=tmp_path / "hython.log",
    )
    text: str = describe_cook(_scene(requires=("imagemagick", "hffmpeg")), result)
    # The message prints paths with this platform's separators, so the expected paths do too.
    assert text.splitlines() == [
        "",
        "Job name: pdg-integ-pyro-0a1b2c3d",
        "Workers need: imagemagick, hffmpeg",
        "Refusals: rop (batch) (rop_batch)",
        "Scheduler errors: first | second",
        f"Debug directory: {Path(_RESULT['debug_dir'])}",
        f"hython log: {tmp_path / 'hython.log'}",
    ]


def test_a_failure_message_of_a_scene_that_needs_nothing_more_says_so(tmp_path: Path) -> None:
    """A scene with no requirements, no refusals and no errors says so, rather than blank lines."""
    result: CookResult = read_cook_result(
        _write_result(
            tmp_path, {**_RESULT, "logged_refusals": [], "scheduler_errors": [], "debug_dir": None}
        ),
        hython_log=tmp_path / "hython.log",
    )
    lines: List[str] = describe_cook(_scene(), result).splitlines()
    assert lines[2:6] == [
        "Workers need: Houdini only",
        "Refusals: none",
        "Scheduler errors: none",
        "Debug directory: None",
    ]


def _write_result(folder: Path, content: Dict[str, Any]) -> Path:
    """A cook_result.json with this content, in the folder."""
    path: Path = folder / "cook_result.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


@_POSIX_ONLY
def test_a_cook_runs_in_the_scene_folder_and_returns_what_hython_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hython gets the scene, the cook node, the wheels and a job name unique to the scene.

    The job name is what a refusal test searches the queue for, so two cooks never share one.
    """
    # Two cooks of one scene, the first with a cook node and wheels.
    monkeypatch.setenv("CANNED_RESULT", json.dumps(_RESULT))
    hython: Path = _fake_hython(tmp_path, _WRITES_RESULT)
    scene: TestScene = _scene(cook_node="/obj/topnet1/render")
    folder: Path = tmp_path / "cook"
    folder.mkdir()

    first: CookResult = run_cook(
        scene,
        folder / "pyro.hip",
        hython=hython,
        env=dict(os.environ),
        wheels=tmp_path / "wheels",
        folder=folder,
    )
    arguments: list = json.loads((tmp_path / "arguments.json").read_text())
    second: CookResult = run_cook(
        scene, folder / "pyro.hip", hython=hython, env=dict(os.environ), wheels=None, folder=folder
    )

    # The result is hython's, under a job name of the scene ID and 8 random hex digits.
    assert first.debug_dir == Path(_RESULT["debug_dir"])
    assert first.job_name.startswith("pdg-integ-pyro-")
    assert len(first.job_name) == len("pdg-integ-pyro-") + 8
    assert first.job_name != second.job_name

    # hython ran the cook script with the scene, the result path and the cook's settings.
    assert arguments[1:] == [
        "--hip",
        str(folder / "pyro.hip"),
        "--result",
        str(folder / "cook_result.json"),
        "--job-name",
        first.job_name,
        "--cook-node",
        "/obj/topnet1/render",
        "--wheels",
        str(tmp_path / "wheels"),
    ]
    assert arguments[0].endswith("cook_scene_in_hython.py")


@_POSIX_ONLY
def test_an_unsaved_scene_is_built_by_the_cook_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no saved file, hython is told to run the scene's builder, and no wheels are passed."""
    # A cook of an unsaved builder scene, with no cook node and no wheels.
    monkeypatch.setenv("CANNED_RESULT", json.dumps(_RESULT))
    hython: Path = _fake_hython(tmp_path, _WRITES_RESULT)

    run_cook(
        _scene(builder="unsaved.py", saved=False),
        None,
        hython=hython,
        env=dict(os.environ),
        wheels=None,
        folder=tmp_path,
    )

    # The builder's path in place of a scene file, and neither optional argument.
    arguments: list = json.loads((tmp_path / "arguments.json").read_text())
    assert arguments[1] == "--build"
    assert Path(arguments[2]).parts[-2:] == ("scene_builders", "unsaved.py")
    assert "--wheels" not in arguments and "--cook-node" not in arguments


@_POSIX_ONLY
def test_hython_that_exits_without_a_result_fails_with_its_output(tmp_path: Path) -> None:
    """A crash in hython fails the test with the end of hython's output, not a missing file."""
    # A hython that prints its error and exits, as one that can't load a scene does.
    hython: Path = _fake_hython(tmp_path, "print('cannot load the scene')\nsys.exit(4)")
    with pytest.raises(RuntimeError) as raised:
        run_cook(
            _scene(),
            tmp_path / "a.hip",
            hython=hython,
            env=dict(os.environ),
            wheels=None,
            folder=tmp_path,
        )
    assert "hython exited 4" in str(raised.value)
    assert "cannot load the scene" in str(raised.value)


@_POSIX_ONLY
def test_a_cook_past_its_time_is_killed_with_every_process_it_started(tmp_path: Path) -> None:
    """A cook that runs too long is stopped, children too, and the result names its debug directory.

    The cook test reads the job ID from that directory to cancel the job, so a hung cook can't
    hold the queue.
    """
    # The stand-in starts a child that would outlive it, records the child's ID, makes a
    # debug directory as a cook does, then hangs.
    body: str = (
        "import subprocess, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "open(os.path.join(here, 'child.pid'), 'w').write(str(child.pid))\n"
        "os.makedirs(os.path.join(os.getcwd(), 'pdg_debug', '2026-11-10-17-09-52-1ad65a51'))\n"
        "time.sleep(600)\n"
    )
    hython: Path = _fake_hython(tmp_path, body)
    folder: Path = tmp_path / "cook"
    folder.mkdir()

    # A limit of 3 s, so the test doesn't wait minutes.
    started: float = time.monotonic()
    result: CookResult = run_cook(
        _scene(max_minutes=0.05),
        folder / "a.hip",
        hython=hython,
        env=dict(os.environ),
        wheels=None,
        folder=folder,
    )

    # hython was stopped near the limit, and its child with it.
    assert time.monotonic() - started < 30
    assert result.timed_out is True
    assert (result.start_refused, result.pdg_work_items) == (False, ())
    assert result.debug_dir == folder / "pdg_debug" / "2026-11-10-17-09-52-1ad65a51"
    assert result.job_name.startswith("pdg-integ-pyro-")
    child_pid: int = int((tmp_path / "child.pid").read_text())
    assert not _is_running(child_pid)


def _is_running(pid: int) -> bool:
    """Whether a process is still alive, after giving a killed one a moment to be reaped."""
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # A killed child of an exited parent is a zombie until init reaps it.
        if _is_zombie(pid):
            return False
        time.sleep(0.1)
    return True


def _is_zombie(pid: int) -> bool:
    """Whether /proc says the process has exited and waits to be reaped.

    False when /proc has no entry for it: either the system has no /proc, or the process
    was reaped while this ran, and then the caller's next `os.kill` finds it gone.
    """
    # Reading the file can fail with either error if init reaps the process mid-read.
    try:
        return "State:\tZ" in Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False


@_POSIX_ONLY
def test_hython_that_exits_cleanly_without_a_result_still_fails(tmp_path: Path) -> None:
    """A cook script that returns without writing its file is a failure, not an empty result."""
    hython: Path = _fake_hython(tmp_path, "print('wrote nothing')")
    with pytest.raises(RuntimeError, match="hython exited 0 without writing"):
        run_cook(
            _scene(),
            tmp_path / "a.hip",
            hython=hython,
            env=dict(os.environ),
            wheels=None,
            folder=tmp_path,
        )
