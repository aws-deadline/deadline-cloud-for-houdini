# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Run the PDG integ tests, which cook scenes on a Deadline Cloud farm.

    python scripts/run_pdg_integ_tests.py [--full] [pytest args ...]

The tests submit real jobs, to the farm and queue that ``PDG_INTEG_FARM_ID`` and
``PDG_INTEG_QUEUE_ID`` name. When either is unset, an interactive run offers the
workstation's default farm and queue, by name and ID, and uses them for this run only if
you answer yes. Any other run exits with status 2 and runs no tests, because a run that
calls this script expects to reach a farm.

It runs the smoke set, or with ``--full`` every scene, and passes the other arguments to
pytest. It sits outside pytest because the tests run in a pytest-xdist worker, which has
no keyboard input.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from deadline.client import api

from deadline.houdini_pdg.submitter import get_workstation_setting

# The variables naming the farm and queue the tests submit to.
_FARM_ID_VARIABLE: str = "PDG_INTEG_FARM_ID"
_QUEUE_ID_VARIABLE: str = "PDG_INTEG_QUEUE_ID"

# What a run without a farm and queue says before it exits.
_NO_FARM_MESSAGE: str = (
    f"Set {_FARM_ID_VARIABLE} and {_QUEUE_ID_VARIABLE} to the farm and queue the tests may "
    "submit to."
)

# The exit status of a run that has no farm and queue.
_NO_FARM_EXIT: int = 2

# Only these answers accept the default farm and queue.
_YES: Tuple[str, ...] = ("y", "yes")

# The repo's root, where pytest runs.
_REPO_ROOT: Path = Path(__file__).resolve().parents[1]


def read_default_farm_queue() -> Optional[Tuple[str, str]]:
    """The workstation's defaults.farm_id and defaults.queue_id, or None when either is empty."""
    farm_id: str = get_workstation_setting("defaults.farm_id")
    queue_id: str = get_workstation_setting("defaults.queue_id")
    return (farm_id, queue_id) if farm_id and queue_id else None


def describe_farm_queue(farm_id: str, queue_id: str, *, deadline_client: Any = None) -> str:
    """The farm's and queue's display names and IDs, from GetFarm and GetQueue.

    A name that can't be read says why in its place. The IDs are always shown.
    """
    farm_name, queue_name = _display_names(farm_id, queue_id, deadline_client)
    return f"  Farm:  {farm_name} ({farm_id})\n  Queue: {queue_name} ({queue_id})"


def ask_to_use_defaults(description: str, *, read_answer: Callable[[str], str] = input) -> bool:
    """Ask once on the terminal. Only "y" or "yes" is a yes."""
    # The question names the farm and queue before the answer is read.
    print(f"{_FARM_ID_VARIABLE} and {_QUEUE_ID_VARIABLE} are not set.")
    print("Use your default farm and queue? Test jobs will be submitted there.")
    print(description)
    # A closed input, such as the end of a pipe, is a No.
    try:
        answer: str = read_answer("[y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in _YES


def _display_names(farm_id: str, queue_id: str, deadline_client: Any) -> Tuple[str, str]:
    """The farm's and queue's display names. Each one that can't be read says why instead."""
    # Without a client neither name can be read.
    try:
        deadline: Any = deadline_client or api.get_boto3_client("deadline")
    except Exception as e:
        return _unreadable(e), _unreadable(e)

    # Each name is read on its own, so a queue the login can't see doesn't hide the farm's name.
    farm_name: str = _display_name(lambda: deadline.get_farm(farmId=farm_id))
    queue_name: str = _display_name(lambda: deadline.get_queue(farmId=farm_id, queueId=queue_id))
    return farm_name, queue_name


def _display_name(read: Callable[[], Mapping[str, Any]]) -> str:
    """The display name in what ``read`` returns, or why it can't be read."""
    try:
        return str(read()["displayName"])
    except Exception as e:
        return _unreadable(e)


def _unreadable(error: Exception) -> str:
    """What the prompt shows in place of a name that can't be read."""
    return f"<name not readable: {type(error).__name__}>"


def _pytest_command(full: bool, pytest_args: Sequence[str]) -> List[str]:
    """pytest on the PDG integ tests: the smoke set, or every scene with ``full``."""
    marker: str = "pdg" if full else "pdg and not pdg_full"
    return [
        sys.executable,
        "-m",
        "pytest",
        "--no-cov",
        str(Path("test") / "integ" / "pdg"),
        "-vvv",
        "--numprocesses=1",
        "-m",
        marker,
        *pytest_args,
    ]


def _run_pytest(command: List[str], env: Mapping[str, str]) -> int:
    """Run pytest from the repo's root, and return its exit code."""
    return subprocess.run(command, cwd=_REPO_ROOT, env=dict(env), check=False).returncode


def main(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] = os.environ,
    is_interactive: Callable[[], bool] = sys.stdin.isatty,
    read_defaults: Callable[[], Optional[Tuple[str, str]]] = read_default_farm_queue,
    describe: Callable[[str, str], str] = describe_farm_queue,
    ask: Callable[[str], bool] = ask_to_use_defaults,
    run_pytest: Callable[[List[str], Mapping[str, str]], int] = _run_pytest,
) -> int:
    """Run the PDG integ tests with pytest, and return pytest's exit code, or 2 when no farm
    and queue are set.

    The keyword arguments are the terminal, the Deadline config, Deadline Cloud and pytest.
    Tests pass their own.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="run_pdg_integ_tests.py", allow_abbrev=False
    )
    parser.add_argument("--full", action="store_true", help="also cook the full set's scenes")
    args, pytest_args = parser.parse_known_args(list(argv))
    pytest_command: List[str] = _pytest_command(args.full, pytest_args)

    # Both variables set: the automated case, with nothing to ask.
    if env.get(_FARM_ID_VARIABLE) and env.get(_QUEUE_ID_VARIABLE):
        return run_pytest(pytest_command, env)

    # Otherwise only a person at a terminal can agree to the default farm and queue.
    defaults: Optional[Tuple[str, str]] = read_defaults() if is_interactive() else None
    if defaults is None:
        print(_NO_FARM_MESSAGE, file=sys.stderr)
        return _NO_FARM_EXIT
    farm_id, queue_id = defaults
    if not ask(describe(farm_id, queue_id)):
        print(_NO_FARM_MESSAGE, file=sys.stderr)
        return _NO_FARM_EXIT

    # The defaults go to this run only. The Deadline config is not changed.
    run_env: Dict[str, str] = dict(env)
    run_env[_FARM_ID_VARIABLE] = farm_id
    run_env[_QUEUE_ID_VARIABLE] = queue_id
    return run_pytest(pytest_command, run_env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
