# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for scripts/run_pdg_integ_tests.py: when it runs the PDG integ tests, and where.

The terminal, the Deadline config reader, Deadline Cloud and pytest are passed in as
stand-ins that record what they were given.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

from scripts.run_pdg_integ_tests import (
    _run_pytest,
    ask_to_use_defaults,
    describe_farm_queue,
    main,
    read_default_farm_queue,
)

_DEFAULTS: Tuple[str, str] = ("farm-default", "queue-default")
_NO_FARM: str = (
    "Set PDG_INTEG_FARM_ID and PDG_INTEG_QUEUE_ID to the farm and queue the tests may submit to."
)


class _Run:
    """Records the pytest runs it is asked for, and returns an exit code."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code: int = exit_code
        self.runs: List[Tuple[List[str], Dict[str, str]]] = []

    def __call__(self, command: List[str], env: Mapping[str, str]) -> int:
        """Record one run."""
        self.runs.append((command, dict(env)))
        return self.exit_code


def _main(
    argv: List[str],
    env: Dict[str, str],
    *,
    interactive: bool = True,
    defaults: Optional[Tuple[str, str]] = _DEFAULTS,
    answer: bool = False,
    run: Optional[_Run] = None,
) -> Tuple[int, _Run, List[str]]:
    """Run main with stand-ins. Returns its exit code, the pytest runs, and what was asked."""
    # The prompt records each description it is shown, and gives the set answer.
    pytest_runs: _Run = run or _Run()
    asked: List[str] = []

    def ask(description: str) -> bool:
        asked.append(description)
        return answer

    exit_code: int = main(
        argv,
        env=env,
        is_interactive=lambda: interactive,
        read_defaults=lambda: defaults,
        describe=lambda farm, queue: f"{farm} / {queue}",
        ask=ask,
        run_pytest=pytest_runs,
    )
    return exit_code, pytest_runs, asked


_SET: Dict[str, str] = {"PDG_INTEG_FARM_ID": "farm-set", "PDG_INTEG_QUEUE_ID": "queue-set"}


def test_with_both_variables_set_it_runs_pytest_without_asking() -> None:
    """The automated case: pytest runs with the environment as given, and returns its exit code."""
    # pytest's own failure comes back as the script's exit code.
    exit_code, run, asked = _main([], dict(_SET), run=_Run(exit_code=1))
    assert exit_code == 1
    assert asked == []
    ((command, env),) = run.runs
    assert (env["PDG_INTEG_FARM_ID"], env["PDG_INTEG_QUEUE_ID"]) == ("farm-set", "queue-set")


def test_the_smoke_run_leaves_out_the_full_set_and_passes_the_other_arguments() -> None:
    """Without --full only the smoke set runs. Arguments for pytest come after the script's own."""
    _, run, _ = _main(["-k", "minimal", "-x"], dict(_SET))
    ((command, _),) = run.runs
    assert command[1:] == [
        "-m",
        "pytest",
        "--no-cov",
        str(Path("test") / "integ" / "pdg"),
        "-vvv",
        "--numprocesses=1",
        "-m",
        "pdg and not pdg_full",
        "-k",
        "minimal",
        "-x",
    ]


def test_the_full_run_includes_every_pdg_test() -> None:
    """--full is the script's own flag, so pytest gets the wider marker and not the flag."""
    # --full comes before the pytest arguments, as hatch's test_pdg_full passes it.
    _, run, _ = _main(["--full", "-k", "pyro"], dict(_SET))
    ((command, _),) = run.runs
    assert command[-4:] == ["-m", "pdg", "-k", "pyro"]
    assert "--full" not in command


def test_a_prefix_of_full_is_passed_to_pytest() -> None:
    """Only the whole flag is the script's, so a pytest flag that starts like it isn't taken."""
    _, run, _ = _main(["--fu"], dict(_SET))
    ((command, _),) = run.runs
    assert command[-3:] == ["-m", "pdg and not pdg_full", "--fu"]


def test_an_interactive_yes_runs_with_the_defaults_for_this_run_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After a yes to the defaults by name, pytest gets them in its environment."""
    # Only the farm is set, so the defaults are offered, and they replace it.
    env: Dict[str, str] = {"PDG_INTEG_FARM_ID": "farm-half-set", "OTHER": "kept"}
    exit_code, run, asked = _main([], env, answer=True)
    assert exit_code == 0
    assert asked == ["farm-default / queue-default"]
    # pytest gets the defaults. The caller's environment is unchanged.
    ((_, run_env),) = run.runs
    assert run_env["PDG_INTEG_FARM_ID"] == "farm-default"
    assert run_env["PDG_INTEG_QUEUE_ID"] == "queue-default"
    assert run_env["OTHER"] == "kept"
    assert env == {"PDG_INTEG_FARM_ID": "farm-half-set", "OTHER": "kept"}


def test_an_interactive_no_exits_2_and_runs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """Without a yes, no test job goes to the default queue."""
    # Asked once, answered no: nothing runs, and the message says what to set.
    exit_code, run, asked = _main([], {}, answer=False)
    assert (exit_code, run.runs) == (2, [])
    assert len(asked) == 1
    assert _NO_FARM in capsys.readouterr().err


def test_a_run_without_a_terminal_exits_2_without_asking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An automated run that can't reach a farm fails, rather than skipping every farm test."""
    exit_code, run, asked = _main([], {"PDG_INTEG_QUEUE_ID": "queue-only"}, interactive=False)
    assert (exit_code, run.runs, asked) == (2, [], [])
    assert _NO_FARM in capsys.readouterr().err


def test_a_workstation_without_defaults_exits_2_without_asking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no default farm and queue there is nothing to offer."""
    exit_code, run, asked = _main([], {}, defaults=None)
    assert (exit_code, run.runs, asked) == (2, [], [])
    assert _NO_FARM in capsys.readouterr().err


@pytest.mark.parametrize("answer", ["y", "yes", "Y", " YES \n"])
def test_y_and_yes_are_yes(answer: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Only y or yes, in any case and with spaces around it, agrees to the defaults."""
    assert ask_to_use_defaults("the farm", read_answer=lambda prompt: answer) is True


@pytest.mark.parametrize("answer", ["", "n", "no", "yess", "sure"])
def test_every_other_answer_is_no(answer: str, capsys: pytest.CaptureFixture[str]) -> None:
    """The default is No, so an empty or unexpected answer doesn't submit jobs."""
    assert ask_to_use_defaults("the farm", read_answer=lambda prompt: answer) is False


def test_the_question_shows_the_farm_and_queue(capsys: pytest.CaptureFixture[str]) -> None:
    """The person sees what they are agreeing to before they answer."""
    prompts: List[str] = []

    def read_answer(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    ask_to_use_defaults("  Farm:  Studio (farm-1)", read_answer=read_answer)
    printed: str = capsys.readouterr().out
    assert printed.splitlines() == [
        "PDG_INTEG_FARM_ID and PDG_INTEG_QUEUE_ID are not set.",
        "Use your default farm and queue? Test jobs will be submitted there.",
        "  Farm:  Studio (farm-1)",
    ]
    assert prompts == ["[y/N] "]


def test_end_of_input_is_no(capsys: pytest.CaptureFixture[str]) -> None:
    """A closed input, such as a pipe that ends, is a No."""

    def closed(prompt: str) -> str:
        raise EOFError

    assert ask_to_use_defaults("the farm", read_answer=closed) is False


class _FakeDeadline:
    """Answers GetFarm and GetQueue with display names, or raises the error it is given for each."""

    def __init__(
        self, farm_error: Optional[Exception] = None, queue_error: Optional[Exception] = None
    ) -> None:
        self._farm_error: Optional[Exception] = farm_error
        self._queue_error: Optional[Exception] = queue_error

    def get_farm(self, *, farmId: str) -> Dict[str, Any]:
        """The farm's name."""
        if self._farm_error:
            raise self._farm_error
        return {"displayName": f"Farm {farmId}"}

    def get_queue(self, *, farmId: str, queueId: str) -> Dict[str, Any]:
        """The queue's name."""
        if self._queue_error:
            raise self._queue_error
        return {"displayName": f"Queue {queueId} of {farmId}"}


def test_the_defaults_are_described_by_name_and_id() -> None:
    """The farm and queue are named as the person knows them, with the IDs the tests use."""
    text: str = describe_farm_queue("farm-1", "queue-2", deadline_client=_FakeDeadline())
    assert text == "  Farm:  Farm farm-1 (farm-1)\n  Queue: Queue queue-2 of farm-1 (queue-2)"


def test_names_that_cant_be_read_still_show_the_ids() -> None:
    """An expired login doesn't hide which farm and queue would get the jobs."""
    expired: PermissionError = PermissionError("expired")
    text: str = describe_farm_queue(
        "farm-1", "queue-2", deadline_client=_FakeDeadline(farm_error=expired, queue_error=expired)
    )
    assert text == (
        "  Farm:  <name not readable: PermissionError> (farm-1)\n"
        "  Queue: <name not readable: PermissionError> (queue-2)"
    )


def test_only_the_name_that_cant_be_read_is_replaced() -> None:
    """A login that can see the farm but not the queue still shows the farm's name.

    Replacing both names would hide a fact the person could have used to decide.
    """
    denied: PermissionError = PermissionError("denied")
    queue_denied: str = describe_farm_queue(
        "farm-1", "queue-2", deadline_client=_FakeDeadline(queue_error=denied)
    )
    assert queue_denied == (
        "  Farm:  Farm farm-1 (farm-1)\n  Queue: <name not readable: PermissionError> (queue-2)"
    )

    # The other way round, too.
    farm_denied: str = describe_farm_queue(
        "farm-1", "queue-2", deadline_client=_FakeDeadline(farm_error=denied)
    )
    assert farm_denied == (
        "  Farm:  <name not readable: PermissionError> (farm-1)\n"
        "  Queue: Queue queue-2 of farm-1 (queue-2)"
    )


def test_without_a_deadline_client_both_names_say_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Deadline config naming an AWS profile that doesn't exist still shows both IDs.

    The client can't be made, so neither name can be read, and the prompt must not crash.
    """
    # The workstation's Deadline config names a profile the empty AWS config doesn't have.
    deadline_config: Path = tmp_path / "deadline_config"
    deadline_config.write_text("[defaults]\naws_profile_name = no-such-profile\n", encoding="utf-8")
    aws_config: Path = tmp_path / "aws_config"
    aws_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEADLINE_CONFIG_FILE_PATH", str(deadline_config))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_config))

    assert describe_farm_queue("farm-1", "queue-2") == (
        "  Farm:  <name not readable: ProfileNotFound> (farm-1)\n"
        "  Queue: <name not readable: ProfileNotFound> (queue-2)"
    )


@pytest.mark.parametrize(
    "config_text, expected",
    [
        pytest.param(
            "[defaults]\naws_profile_name = studio\n\n[profile-studio defaults]\nfarm_id = farm-1\n\n"
            "[profile-studio farm-1 defaults]\nqueue_id = queue-2\n",
            ("farm-1", "queue-2"),
            id="both",
        ),
        pytest.param(
            "[defaults]\naws_profile_name = studio\n\n[profile-studio defaults]\nfarm_id = farm-1\n",
            None,
            id="no_queue",
        ),
        pytest.param("", None, id="neither"),
    ],
)
def test_the_defaults_are_the_workstation_deadline_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_text: str,
    expected: Optional[Tuple[str, str]],
) -> None:
    """The offer is the farm and queue the workstation submits to, and only when it has both."""
    # The client library reads the workstation's config from this variable's path.
    config: Path = tmp_path / "config"
    config.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("DEADLINE_CONFIG_FILE_PATH", str(config))
    assert read_default_farm_queue() == expected


def test_the_run_starts_from_the_repo_root_and_returns_its_exit_code(tmp_path: Path) -> None:
    """pytest's paths are relative to the repo's root, and its exit code is the script's.

    The command here stands in for pytest: it writes where it ran and exits with a set code.
    """
    # The folder it ran in goes to this file.
    where: Path = tmp_path / "cwd.txt"
    command: List[str] = [
        sys.executable,
        "-c",
        "import os, sys; open(os.environ['WHERE'], 'w').write(os.getcwd()); sys.exit(5)",
    ]
    assert _run_pytest(command, {**os.environ, "WHERE": str(where)}) == 5
    assert Path(where.read_text()) == Path(__file__).resolve().parents[3]


def test_the_script_reads_its_own_command_line() -> None:
    """Run as a script, it parses the arguments after its own name.

    ``--help`` prints the usage and runs no tests, so this needs no farm.
    """
    # The script as hatch runs it, in a new Python process.
    script: Path = Path(__file__).resolve().parents[3] / "scripts" / "run_pdg_integ_tests.py"
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0
    assert "--full" in completed.stdout
