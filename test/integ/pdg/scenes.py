# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The PDG integ tests' scenes, each with the result a cook of it must give.

``scenes.yaml`` lists them. A scene is either built by a script in ``scene_builders/``
when the test runs, or is an external ``.hip`` file in the folder that
``PDG_TEST_SCENES_DIR`` names, pinned by its SHA-256.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Set, Tuple

import pytest
import yaml

#: The checked-in list of test scenes.
SCENES_FILE: Path = Path(__file__).parent / "scenes.yaml"

#: The folder of scene builder scripts.
SCENE_BUILDERS_DIR: Path = Path(__file__).parent / "scene_builders"

#: The variable naming the folder that holds the external scenes.
SCENES_DIR_VARIABLE: str = "PDG_TEST_SCENES_DIR"

# The version of scenes.yaml's format that this module reads.
_FORMAT_VERSION: int = 1

# How much of a scene file is hashed at a time, so a large scene isn't read into memory.
_HASH_CHUNK_BYTES: int = 1 << 20


class SceneKind(Enum):
    """Whether a scene must cook, or must be refused."""

    COOK = "cook"
    REFUSAL = "refusal"


class SceneSet(Enum):
    """Which run includes a scene: every run, or only the full one."""

    SMOKE = "smoke"
    FULL = "full"


@dataclass(frozen=True)
class ExpectedStep:
    """One step the cook's snapshot must have."""

    name: str
    task_count: int
    declares_outputs: bool


@dataclass(frozen=True)
class ExpectedRefusal:
    """One refusal the cook must log."""

    #: A ``RefusalScenario`` value.
    scenario: str
    #: Text the refusal's subject must contain; ``""`` matches any subject.
    subject_contains: str


@dataclass(frozen=True)
class TestScene:
    """One test scene and the result a cook of it must give."""

    # Not a test class: stops pytest collecting it for its Test prefix.
    __test__: ClassVar[bool] = False

    scene_id: str
    kind: SceneKind
    scene_set: SceneSet
    #: Exactly one of ``builder`` and ``external`` is set.
    builder: Optional[str]
    external: Optional[str]
    sha256: Optional[str]
    #: False only for a builder scene that the cook builds itself and never saves.
    saved: bool
    #: The node to cook, or None for the TOP network's display node.
    cook_node: Optional[str]
    #: What the farm's workers need for this scene, named in failure messages.
    requires: Tuple[str, ...]
    #: How long the cook may take before the test kills it and cancels its job.
    max_minutes: float
    expected_outcome: str
    expected_steps: Tuple[ExpectedStep, ...]
    expected_refusals: Tuple[ExpectedRefusal, ...]
    #: Host requirements every step of the job must hold, or None.
    expected_host_requirements: Optional[Mapping[str, Any]]


def load_scenes(path: Path) -> Tuple[TestScene, ...]:
    """Read scenes.yaml. Raises ValueError naming the scene and the rule it breaks.

    The rules: each ID is unique; a cook scene has steps and no refusals, and a refusal
    scene the reverse; a source has exactly one of builder and external; an external
    source has a SHA-256; and only a builder source may be unsaved.
    """
    document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != _FORMAT_VERSION:
        raise ValueError(f"{path.name} must be a mapping with version: {_FORMAT_VERSION}.")

    scenes: List[TestScene] = []
    seen: Set[str] = set()
    for position, entry in enumerate(document.get("scenes") or []):
        # Each entry is read by itself, and the scene's ID goes into every error about it.
        scene_id: str = str(entry.get("id", "")) if isinstance(entry, dict) else ""
        name: str = scene_id or f"scene {position}"
        try:
            scene: TestScene = _read_scene(entry)
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{path.name}: {name}: {_message(e)}") from e
        if scene.scene_id in seen:
            raise ValueError(f"{path.name}: {name}: another scene has the same id.")
        seen.add(scene.scene_id)
        scenes.append(scene)
    return tuple(scenes)


def scene_params(kind: SceneKind, path: Path = SCENES_FILE) -> List[Any]:
    """One pytest param per scene of this kind, by its ID. A full-set scene carries the
    ``pdg_full`` mark, so it runs only in a full run."""
    return [
        pytest.param(
            scene,
            id=scene.scene_id,
            marks=[pytest.mark.pdg_full] if scene.scene_set is SceneSet.FULL else [],
        )
        for scene in load_scenes(path)
        if scene.kind is kind
    ]


def prepare_scene_file(
    scene: TestScene,
    *,
    hython: Path,
    env: Mapping[str, str],
    scenes_dir: Optional[Path],
    into: Path,
    builders_dir: Path = SCENE_BUILDERS_DIR,
) -> Optional[Path]:
    """Put the scene's .hip in the empty folder ``into``, and return its path.

    A builder scene: runs the builder in a hython subprocess, which saves the .hip.
    An external scene: copies the file after checking its SHA-256. It raises
    ``FileNotFoundError``, naming ``PDG_TEST_SCENES_DIR``, when the variable is unset or the
    file is missing, and ``ValueError`` when the file's hash differs.
    A scene with saved False: returns None, and cook_scene_in_hython.py runs the builder itself.
    """
    if not scene.saved:
        return None

    # A built scene is saved under its ID by the builder, in hython with the plugin loaded.
    if scene.builder is not None:
        hip: Path = into / f"{scene.scene_id}.hip"
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [str(hython), str(builders_dir / scene.builder), str(hip)],
            env=dict(env),
            cwd=str(into),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not hip.is_file():
            raise RuntimeError(
                f"Builder {scene.builder} did not save {hip} (exit {completed.returncode}):\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
        return hip

    # An external scene is used as it was downloaded, keeping its name for scenes that
    # use $HIPNAME.
    external: str = str(scene.external)
    if scenes_dir is None:
        raise FileNotFoundError(
            f"Scene {scene.scene_id} is {external}, in the folder {SCENES_DIR_VARIABLE} names. "
            f"Set {SCENES_DIR_VARIABLE}."
        )
    source: Path = scenes_dir / external
    if not source.is_file():
        raise FileNotFoundError(
            f"Scene {scene.scene_id} needs {source}, which is missing. Save the scene in "
            f"{SCENES_DIR_VARIABLE} under the name scenes.yaml gives it."
        )
    actual: str = _sha256(source)
    if actual != scene.sha256:
        raise ValueError(
            f"Scene {scene.scene_id}: {source} has SHA-256 {actual}, but scenes.yaml pins "
            f"{scene.sha256}. It is a different file."
        )
    hip_copy: Path = into / source.name
    shutil.copyfile(source, hip_copy)
    return hip_copy


def _read_scene(entry: Any) -> TestScene:
    """One scenes.yaml entry. Raises KeyError, TypeError or ValueError for a bad one."""
    # The entry's own fields, then its source and what it expects.
    if not isinstance(entry, dict):
        raise TypeError("must be a mapping")
    source: Dict[str, Any] = dict(entry["source"])
    expect: Dict[str, Any] = dict(entry["expect"])
    scene: TestScene = TestScene(
        scene_id=str(entry["id"]),
        kind=SceneKind(entry["kind"]),
        scene_set=SceneSet(entry["set"]),
        builder=source.get("builder"),
        external=source.get("external"),
        sha256=source.get("sha256"),
        saved=bool(source.get("saved", True)),
        cook_node=entry.get("cook_node"),
        requires=tuple(str(r) for r in entry.get("requires") or ()),
        max_minutes=float(entry["max_minutes"]),
        expected_outcome=str(expect["outcome"]),
        expected_steps=tuple(
            ExpectedStep(
                name=str(s["name"]),
                task_count=int(s["task_count"]),
                declares_outputs=bool(s["declares_outputs"]),
            )
            for s in expect.get("steps") or ()
        ),
        expected_refusals=tuple(
            ExpectedRefusal(
                scenario=str(r["scenario"]), subject_contains=str(r.get("subject_contains", ""))
            )
            for r in expect.get("refusals") or ()
        ),
        expected_host_requirements=expect.get("host_requirements"),
    )
    _check_rules(scene)
    return scene


def _check_rules(scene: TestScene) -> None:
    """Raise ValueError for a scene that breaks one of the rules ``load_scenes`` lists."""
    # What the scene expects must match its kind.
    if scene.kind is SceneKind.COOK and (not scene.expected_steps or scene.expected_refusals):
        raise ValueError("a cook scene must list expected steps and no refusals")
    if scene.kind is SceneKind.REFUSAL and (scene.expected_steps or not scene.expected_refusals):
        raise ValueError("a refusal scene must list expected refusals and no steps")

    # Where the scene comes from.
    if (scene.builder is None) == (scene.external is None):
        raise ValueError("source must have exactly one of builder and external")
    if scene.external is not None and not scene.sha256:
        raise ValueError("an external source must have a sha256")
    if not scene.saved and scene.builder is None:
        raise ValueError("only a builder source may have saved: false")


def _message(error: Exception) -> str:
    """An error's text, with a missing key named as a missing field."""
    if isinstance(error, KeyError):
        return f"missing field {error.args[0]}"
    return str(error)


def _sha256(path: Path) -> str:
    """The file's SHA-256, as lowercase hex."""
    digest: Any = hashlib.sha256()
    with path.open("rb") as scene_file:
        for chunk in iter(lambda: scene_file.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return str(digest.hexdigest())
