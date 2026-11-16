# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Cook one test scene in a fresh hython process, and read back what the cook did.

``run_cook`` starts hython on ``cook_scene_in_hython.py``, which writes
``cook_result.json`` in the scene's folder. ``read_cook_result`` turns that file into a
``CookResult``, and ``describe_cook`` gives a failed test's message what it needs to know.
For a scene that must be refused, ``cancel_jobs_of_refused_cook`` finds and cancels any
job the cook made, ``refusal_problems`` judges the cook, and ``refusal_failure`` gives the
refusal test's failure message.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from deadline.houdini_pdg.ports import OutputFile

from .compare_cook_with_farm import PdgWorkItem
from .cook_scene_in_hython import RESULT_VERSION, find_debug_dir
from .farm import FarmQueue, cancel_job, jobs_created_since
from .scenes import SCENE_BUILDERS_DIR, ExpectedRefusal, TestScene

#: The hython script that cooks one scene.
COOK_SCRIPT: Path = Path(__file__).parent / "cook_scene_in_hython.py"

#: The file the cook script writes in the scene's folder.
RESULT_FILE_NAME: str = "cook_result.json"

#: The file hython's output goes to, in the scene's folder.
HYTHON_LOG_NAME: str = "hython.log"

#: The start of the error a refusal puts on the scheduler node, after the node's label.
REFUSAL_HEADER: str = "The graph was refused ("

# How many random bytes, written as hex, make each job's name unique to its cook.
_JOB_NAME_RANDOM_BYTES: int = 4

# How long before the test's start the job search begins. A workstation clock ahead of
# the service's would otherwise hide a job. A longer window only searches more jobs.
_CLOCK_SKEW_MARGIN: timedelta = timedelta(minutes=5)


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


def cancel_jobs_of_refused_cook(
    farm_queue: FarmQueue,
    result: CookResult,
    *,
    test_started_at: datetime,
    deadline_client: Optional[Any] = None,
) -> List[str]:
    """Cancel every job on the queue made by a cook that should have been refused, and
    return their IDs. A refused cook makes none, so the list is normally empty.

    A job found would run on a shared queue. The search covers jobs created since the
    test started, less a margin for clock skew, that carry the cook's job name or its
    cook ID. The client defaults to the workstation's Deadline Cloud login. Tests pass their own.
    """
    jobs: List[str] = jobs_created_since(
        farm_queue,
        test_started_at - _CLOCK_SKEW_MARGIN,
        cook_id=read_cook_id(result.debug_dir),
        job_name=result.job_name,
        deadline_client=deadline_client,
    )
    for job_id in jobs:
        cancel_job(farm_queue, job_id, deadline_client=deadline_client)
    return jobs


def refusal_failure(scene: TestScene, result: CookResult, canceled_jobs: Sequence[str]) -> str:
    """Why the refusal test of this cook fails, followed by ``describe_cook``'s lines, or
    ``""`` when the test passes.

    The test fails when the cook made a job, which the caller has canceled, or when
    ``refusal_problems`` finds anything wrong with the cook.
    """
    # A job the cook made goes first, since it is the worst way a refusal can go wrong.
    problems: List[str] = refusal_problems(scene, result)
    if canceled_jobs:
        problems.insert(0, f"The cook made job(s) {', '.join(canceled_jobs)}, now canceled.")
    if not problems:
        return ""
    return "\n".join(problems) + describe_cook(scene, result)


def refusal_problems(scene: TestScene, result: CookResult) -> List[str]:
    """What is wrong with the cook of a scene that must be refused, one line each, or nothing.

    The cook must end within the scene's time limit. Its logged refusals and the scene's
    expected ones must pair one to one: each expected refusal takes its own logged refusal
    with the same scenario and a subject that contains ``subject_contains``, and no logged
    refusal is left over. One of the scheduler node's errors must hold the refusal header,
    which only a refusal puts there.
    """
    problems: List[str] = []
    if result.timed_out:
        problems.append(f"The cook ran past {scene.max_minutes:g} min.")

    # Every refusal that found no pair, on either side.
    missing, extra = match_refusals(scene.expected_refusals, result.logged_refusals)
    problems += [
        f"Not logged: a {r.scenario} refusal whose subject contains '{r.subject_contains}'."
        for r in missing
    ]
    problems += [f"Logged but not expected: {r.subject} ({r.scenario})." for r in extra]

    if not any(REFUSAL_HEADER in error for error in result.scheduler_errors):
        problems.append(f"No error on the scheduler node holds '{REFUSAL_HEADER}'.")
    return problems


def match_refusals(
    expected: Sequence[ExpectedRefusal], logged: Sequence[LoggedRefusal]
) -> Tuple[Tuple[ExpectedRefusal, ...], Tuple[LoggedRefusal, ...]]:
    """Pair as many expected refusals as possible each with its own logged refusal, and
    return the expected and the logged refusals left without a pair.

    A logged refusal fits an expected one when the scenarios are equal and its subject
    contains ``subject_contains``. Pairing in order isn't enough: an expected refusal that
    fits any subject could take the only logged refusal a narrower one fits.
    """
    # The logged refusals each expected refusal fits, by position.
    fits: List[List[int]] = [
        [
            index
            for index, refusal in enumerate(logged)
            if refusal.scenario == want.scenario and want.subject_contains in refusal.subject
        ]
        for want in expected
    ]
    # Which expected refusal holds each logged refusal that has been paired.
    holder: Dict[int, int] = {}

    def pair(want: int, tried: Set[int]) -> bool:
        """Give expected refusal ``want`` a logged refusal: a free one, or one whose holder
        can move to another. Each logged refusal is tried once per search."""
        for index in fits[want]:
            if index in tried:
                continue
            tried.add(index)
            if index not in holder or pair(holder[index], tried):
                holder[index] = want
                return True
        return False

    paired: Set[int] = {want for want in range(len(expected)) if pair(want, set())}
    return (
        tuple(want for position, want in enumerate(expected) if position not in paired),
        tuple(refusal for index, refusal in enumerate(logged) if index not in holder),
    )


def read_cook_id(debug_dir: Optional[Path]) -> Optional[str]:
    """The cook's ID from the cook.json in its debug directory, or None when there is none.

    A refusal at the cook's start writes no debug directory, so the cook has no ID.
    """
    # The scheduler writes cook.json, with the cook's ID, when the cook starts.
    cook_json: Optional[Path] = debug_dir / "cook.json" if debug_dir is not None else None
    if cook_json is None or not cook_json.is_file():
        return None
    cook_id: Optional[str] = json.loads(cook_json.read_text(encoding="utf-8")).get("cook_id")
    return cook_id


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
