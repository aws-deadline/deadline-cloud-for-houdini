# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for which PDG integ tests a run collects, and when they skip, with no farm and no Houdini.

Each test runs pytest on test/integ/pdg in a new process, as ``hatch run integ:test`` or
``scripts/run_pdg_integ_tests.py`` would. GitHub runs the integ tests only after a merge,
so these are what catch a change that makes the cook and refusal tests run without a
farm, or collects the wrong scenes. The test that needs hython uses a stand-in script,
which needs a POSIX shell.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set

import pytest

_REPO_ROOT: Path = Path(__file__).resolve().parents[3]

# The scenes each set cooks, as test/integ/pdg/scenes.yaml lists them.
_SMOKE_COOK_SCENES: Set[str] = {
    "minimal_cook",
    "minimal_cook_host_requirements",
    "pyro_bonfire_small",
}
_REFUSAL_SCENES: Set[str] = {
    "dynamic_node",
    "in_process_node",
    "in_process_step",
    "runtime_file_unproduced",
    "rop_batch",
    "multiple_schedulers",
    "service_node",
    "quota_exceeded",
    "output_outside_cook",
    "local_file_unreachable",
    "attribute_as_variable",
    "cached_outputs",
    "unsaved_scene",
    "no_farm_work",
    "city_builder",
    "volume_advection",
}
_SMOKE_SCENES: Set[str] = _SMOKE_COOK_SCENES | _REFUSAL_SCENES
_ALL_SCENES: Set[str] = _SMOKE_SCENES | {"pyro_bonfire_large", "orca_guided_ocean"}

# The parametrized tests' node names, with the scene ID each one cooks.
_SCENE_TEST: re.Pattern[str] = re.compile(
    r"(?:test_scene_cooks_end_to_end|test_refused_scene_creates_no_job)\[(\w+)\]"
)

# A farm and queue that don't exist. No test here gets far enough to reach them.
_FAKE_FARM: Dict[str, str] = {
    "PDG_INTEG_FARM_ID": "farm-does-not-exist",
    "PDG_INTEG_QUEUE_ID": "queue-does-not-exist",
}


def _clean_env(**changes: str) -> Dict[str, str]:
    """This process's environment without the PDG test variables or this pytest run's own.

    pytest and its coverage plugin pass settings to child processes through the
    environment, which would change how the inner pytest runs.
    """
    env: Dict[str, str] = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PDG_INTEG_", "PDG_TEST_", "PYTEST_", "COV_CORE_"))
    }
    env.update(changes)
    return env


def _skips_of_pdg_tests(env: Mapping[str, str], tmp_path: Path) -> Dict[str, str]:
    """Run pytest on test/integ/pdg as ``hatch run integ:test`` does, and return each cook
    or refusal test that ran, by scene ID, with its skip reason, or ``""`` when it didn't skip.
    """
    # The JUnit report holds each test's outcome and skip reason in a fixed format.
    report: Path = tmp_path / "report.xml"
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-m", "pytest", "--no-cov", "-p", "no:cacheprovider", "-n0"]
        + [str(Path("test") / "integ" / "pdg"), f"--junitxml={report}"],
        cwd=_REPO_ROOT,
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    assert report.is_file(), completed.stdout + completed.stderr

    # Each test case's skip reason, or "" for one that passed, failed or errored.
    skips: Dict[str, str] = {}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        match: Optional[re.Match[str]] = _SCENE_TEST.fullmatch(case.get("name", ""))
        assert match, f"Not a cook or refusal test: {case.get('name')}"
        skipped: Optional[ElementTree.Element] = case.find("skipped")
        skips[match.group(1)] = "" if skipped is None else skipped.get("message", "")
    return skips


def _collected_by_wrapper(arguments: List[str], tmp_path: Path) -> Set[str]:
    """The scene IDs ``scripts/run_pdg_integ_tests.py`` collects with these arguments.

    It runs from another folder, as hatch may start it, and with the farm variables set.
    """
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "run_pdg_integ_tests.py"), *arguments]
        + ["--collect-only", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=_clean_env(**_FAKE_FARM),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return set(_SCENE_TEST.findall(completed.stdout))


def test_without_the_farm_variables_every_cook_test_skips_and_says_why(tmp_path: Path) -> None:
    """A plain integ run doesn't submit jobs. It skips each cook and refusal test, naming what
    to set.

    Without the skip, every test would start hython and fail in an integ run that has no farm.
    """
    skips: Dict[str, str] = _skips_of_pdg_tests(_clean_env(), tmp_path)

    assert set(skips) == _ALL_SCENES
    for scene_id, reason in skips.items():
        assert "PDG_INTEG_FARM_ID" in reason, scene_id
        assert "PDG_INTEG_QUEUE_ID" in reason, scene_id
        assert "scripts/run_pdg_integ_tests.py" in reason, scene_id


@pytest.mark.skipif(os.name == "nt", reason="the hython stand-in is a POSIX script")
def test_with_a_farm_but_a_hython_that_cannot_start_houdini_every_scene_test_skips(
    tmp_path: Path,
) -> None:
    """A workstation without a Houdini license skips the cook and refusal tests, rather than
    failing them.

    The stand-in hython exits 1, as hython does when it can't get a license.
    """
    # The stand-in prints what hython prints without a license.
    hython: Path = tmp_path / "hython"
    hython.write_text(
        "#!/bin/sh\necho 'No licenses could be found to run this application.'\nexit 1\n"
    )
    hython.chmod(hython.stat().st_mode | stat.S_IXUSR)

    skips: Dict[str, str] = _skips_of_pdg_tests(
        _clean_env(**_FAKE_FARM, HYTHON_EXECUTABLE=str(hython)), tmp_path
    )

    assert set(skips) == _ALL_SCENES
    for scene_id, reason in skips.items():
        assert "hython cannot start Houdini" in reason, scene_id
        assert "No licenses could be found" in reason, scene_id


def test_the_wrapper_collects_only_the_smoke_set_by_default(tmp_path: Path) -> None:
    """A smoke run cooks the three small scenes and every refusal scene, and none of the
    large ones.

    A large scene in the smoke run would make each run take hours.
    """
    assert _collected_by_wrapper([], tmp_path) == _SMOKE_SCENES


def test_the_wrapper_collects_every_cook_scene_with_full(tmp_path: Path) -> None:
    """--full adds the large scenes to the smoke set."""
    assert _collected_by_wrapper(["--full"], tmp_path) == _ALL_SCENES


def test_selecting_refused_collects_every_refusal_scene_and_no_cook_scene(tmp_path: Path) -> None:
    """``-k refused`` runs only the refusal tests, which make no job when they pass.

    That selection is how a run checks every refusal without cooking a scene on the farm.
    """
    assert _collected_by_wrapper(["-k", "refused"], tmp_path) == _REFUSAL_SCENES
