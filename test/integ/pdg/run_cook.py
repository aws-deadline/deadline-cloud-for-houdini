# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Cook one test scene in a fresh hython process, and read back what the cook did.

``run_cook`` starts hython on ``cook_scene_in_hython.py``, which writes
``cook_result.json`` in the scene's folder. ``read_cook_result`` turns that file into a
``CookResult``, and ``describe_cook`` gives a failed test's message what it needs to know.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from deadline.houdini_pdg.ports import OutputFile

from .compare_cook_with_farm import PdgWorkItem
from .cook_scene_in_hython import RESULT_VERSION, find_debug_dir
from .scenes import SCENE_BUILDERS_DIR, TestScene

#: The hython script that cooks one scene.
COOK_SCRIPT: Path = Path(__file__).parent / "cook_scene_in_hython.py"

#: The file the cook script writes in the scene's folder.
RESULT_FILE_NAME: str = "cook_result.json"

#: The file hython's output goes to, in the scene's folder.
HYTHON_LOG_NAME: str = "hython.log"

# How many random bytes, written as hex, make each job's name unique to its cook.
_JOB_NAME_RANDOM_BYTES: int = 4


@dataclass(frozen=True)
class LoggedRefusal:
    """One ``Refused, ...`` line the cook logged."""

    #: A ``RefusalScenario`` value.
    scenario: str
    subject: str


@dataclass(frozen=True)
class CookResult:
    """What one hython cook did, as cook_scene_in_hython.py recorded it."""

    #: The cooked .hip, or None for a scene that was never saved.
    hip_file: Optional[Path]
    #: The name the cook gave its job, unique to this cook.
    job_name: str
    started_at: datetime
    returned_at: datetime
    #: True when hython was killed for running past the scene's ``max_minutes``.
    timed_out: bool
    #: True when PDG raised at the cook's start, which a refusal there causes.
    start_refused: bool
    debug_dir: Optional[Path]
    logged_refusals: Tuple[LoggedRefusal, ...]
    scheduler_errors: Tuple[str, ...]
    pdg_work_items: Tuple[PdgWorkItem, ...]
    #: hython's own output, for a failure message.
    hython_log: Path


def run_cook(
    scene: TestScene,
    hip: Optional[Path],
    *,
    hython: Path,
    env: Mapping[str, str],
    wheels: Optional[Path],
    folder: Path,
) -> CookResult:
    """Cook one scene in a fresh hython process and return what cook_scene_in_hython.py recorded.

    hython runs in ``folder``, which holds the scene. It is killed, with every process it
    started, after ``scene.max_minutes``, and the result then has ``timed_out`` set and only
    what the folder shows. Raises ``RuntimeError`` when hython exits without a result.
    """
    job_name: str = f"pdg-integ-{scene.scene_id}-{secrets.token_hex(_JOB_NAME_RANDOM_BYTES)}"
    result_file: Path = folder / RESULT_FILE_NAME
    log_file: Path = folder / HYTHON_LOG_NAME
    command: List[str] = [str(hython), str(COOK_SCRIPT), *_scene_arguments(scene, hip)]
    command += ["--result", str(result_file), "--job-name", job_name]
    if scene.cook_node:
        command += ["--cook-node", scene.cook_node]
    if wheels is not None:
        command += ["--wheels", str(wheels)]

    # hython's output goes to a file, so a long cook can't fill a pipe and stall. Its own
    # session lets a timeout kill the processes the cook started too.
    started_at: datetime = datetime.now(timezone.utc)
    with log_file.open("w", encoding="utf-8") as log:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            command,
            cwd=str(folder),
            env=dict(env),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            exit_code: int = process.wait(timeout=scene.max_minutes * 60)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            process.wait()
            return _timed_out_result(job_name, hip, folder, started_at, log_file)

    if exit_code != 0 or not result_file.is_file():
        raise RuntimeError(
            f"hython exited {exit_code} without writing {result_file}. Its output is in "
            f"{log_file}:\n{_tail(log_file)}"
        )
    return read_cook_result(result_file, hython_log=log_file)


def describe_cook(scene: TestScene, result: CookResult) -> str:
    """The lines a failed cook test's message ends with: the job, what the scene's workers
    need, the refusals and scheduler errors the cook logged, and where its logs are.
    """
    lines: Dict[str, str] = {
        "Job name": result.job_name,
        "Workers need": ", ".join(scene.requires) or "Houdini only",
        "Refusals": ", ".join(f"{r.subject} ({r.scenario})" for r in result.logged_refusals)
        or "none",
        "Scheduler errors": " | ".join(result.scheduler_errors) or "none",
        "Debug directory": str(result.debug_dir),
        "hython log": str(result.hython_log),
    }
    return "\n" + "\n".join(f"{name}: {value}" for name, value in lines.items())


def read_cook_result(path: Path, *, hython_log: Path) -> CookResult:
    """Read a cook_result.json. Raises ValueError for a file of another format version."""
    content: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if content.get("version") != RESULT_VERSION:
        raise ValueError(
            f"{path} has format version {content.get('version')}, not {RESULT_VERSION}."
        )
    return CookResult(
        hip_file=Path(content["hip_file"]) if content["hip_file"] else None,
        job_name=content["job_name"],
        started_at=datetime.fromisoformat(content["started_at"]),
        returned_at=datetime.fromisoformat(content["returned_at"]),
        timed_out=False,
        start_refused=bool(content["start_refused"]),
        debug_dir=Path(content["debug_dir"]) if content["debug_dir"] else None,
        logged_refusals=tuple(
            LoggedRefusal(scenario=r["scenario"], subject=r["subject"])
            for r in content["logged_refusals"]
        ),
        scheduler_errors=tuple(content["scheduler_errors"]),
        pdg_work_items=tuple(
            PdgWorkItem(
                node=w["node"],
                name=w["name"],
                index=int(w["index"]),
                state=w["state"],
                outputs=tuple(OutputFile(o["path"], o["tag"]) for o in w["outputs"]),
            )
            for w in content["pdg_work_items"]
        ),
        hython_log=hython_log,
    )


def _scene_arguments(scene: TestScene, hip: Optional[Path]) -> List[str]:
    """The cook script's arguments for the scene: the saved .hip, or the builder to run unsaved."""
    if hip is not None:
        return ["--hip", str(hip)]
    return ["--build", str(SCENE_BUILDERS_DIR / str(scene.builder))]


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill hython and every process it started."""
    # Checked on sys.platform, because mypy skips the other platform's branch only for that check.
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False
        )
    else:
        os.killpg(process.pid, signal.SIGKILL)


def _timed_out_result(
    job_name: str, hip: Optional[Path], folder: Path, started_at: datetime, log_file: Path
) -> CookResult:
    """What a killed cook leaves: its job name, and its debug directory if it made one."""
    debug_dir: Optional[str] = find_debug_dir(str(folder))
    return CookResult(
        hip_file=hip,
        job_name=job_name,
        started_at=started_at,
        returned_at=datetime.now(timezone.utc),
        timed_out=True,
        start_refused=False,
        debug_dir=Path(debug_dir) if debug_dir else None,
        logged_refusals=(),
        scheduler_errors=(),
        pdg_work_items=(),
        hython_log=log_file,
    )


def _tail(path: Path, lines: int = 40) -> str:
    """The last lines of a text file."""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
