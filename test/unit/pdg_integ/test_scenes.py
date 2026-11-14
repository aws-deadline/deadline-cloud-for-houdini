# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for reading scenes.yaml and preparing each scene's .hip, with no Houdini."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from test.integ.pdg.scenes import (
    SCENES_FILE,
    ExpectedRefusal,
    ExpectedStep,
    SceneKind,
    SceneSet,
    TestScene,
    load_scenes,
    prepare_scene_file,
)

# A scene file's content, and its SHA-256.
_CONTENT: bytes = b"a saved Houdini scene"
_CONTENT_SHA256: str = hashlib.sha256(_CONTENT).hexdigest()


def _cook_entry(scene_id: str = "a_cook", **changes: Any) -> Dict[str, Any]:
    """A valid cook scene entry, with any field replaced."""
    entry: Dict[str, Any] = {
        "id": scene_id,
        "kind": "cook",
        "set": "smoke",
        "source": {"builder": "a_cook.py"},
        "max_minutes": 20,
        "expect": {
            "outcome": "succeeded",
            "steps": [{"name": "render", "task_count": 3, "declares_outputs": True}],
        },
    }
    entry.update(changes)
    return entry


def _refusal_entry(scene_id: str = "a_refusal", **changes: Any) -> Dict[str, Any]:
    """A valid refusal scene entry, with any field replaced."""
    entry: Dict[str, Any] = {
        "id": scene_id,
        "kind": "refusal",
        "set": "smoke",
        "source": {"external": "refused.hip", "sha256": _CONTENT_SHA256},
        "max_minutes": 3,
        "expect": {"outcome": "refused", "refusals": [{"scenario": "rop_batch"}]},
    }
    entry.update(changes)
    return entry


def _write_scenes(tmp_path: Path, entries: List[Dict[str, Any]], version: int = 1) -> Path:
    """A scenes.yaml holding these entries."""
    path: Path = tmp_path / "scenes.yaml"
    path.write_text(yaml.safe_dump({"version": version, "scenes": entries}), encoding="utf-8")
    return path


def _scene(**changes: Any) -> TestScene:
    """An external cook scene, with any field replaced."""
    fields: Dict[str, Any] = {
        "scene_id": "pyro",
        "kind": SceneKind.COOK,
        "scene_set": SceneSet.SMOKE,
        "builder": None,
        "external": "pyro.hip",
        "sha256": _CONTENT_SHA256,
        "saved": True,
        "cook_node": None,
        "requires": (),
        "max_minutes": 45.0,
        "expected_outcome": "succeeded",
        "expected_steps": (ExpectedStep("render", 3, True),),
        "expected_refusals": (),
        "expected_host_requirements": None,
    }
    fields.update(changes)
    return TestScene(**fields)


def _prepare(scene: TestScene, scenes_dir: Optional[Path], into: Path) -> Optional[Path]:
    """prepare_scene_file with a hython that is never started, for an external scene."""
    return prepare_scene_file(
        scene, hython=Path("no-hython"), env={}, scenes_dir=scenes_dir, into=into
    )


def test_the_checked_in_scenes_load() -> None:
    """scenes.yaml itself follows every rule, so a bad entry fails here, not in a farm run."""
    scenes: Tuple[TestScene, ...] = load_scenes(SCENES_FILE)
    assert {"minimal_cook", "minimal_cook_host_requirements", "pyro_bonfire_small"} <= {
        s.scene_id for s in scenes
    }


def test_an_entry_is_read_into_its_scene(tmp_path: Path) -> None:
    """Each field lands where the tests read it, including the defaults for omitted fields."""
    # A cook scene with every optional field, and a refusal scene with a subject to match.
    host: Dict[str, Any] = {"attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux"]}]}
    cook: Dict[str, Any] = _cook_entry(
        "host", set="full", requires=["linux workers"], cook_node="/obj/topnet1/render"
    )
    cook["expect"]["host_requirements"] = host
    refusal: Dict[str, Any] = _refusal_entry()
    refusal["expect"]["refusals"] = [{"scenario": "rop_batch", "subject_contains": "rop1"}]

    cook_scene, refusal_scene = load_scenes(_write_scenes(tmp_path, [cook, refusal]))

    assert cook_scene == TestScene(
        scene_id="host",
        kind=SceneKind.COOK,
        scene_set=SceneSet.FULL,
        builder="a_cook.py",
        external=None,
        sha256=None,
        saved=True,
        cook_node="/obj/topnet1/render",
        requires=("linux workers",),
        max_minutes=20.0,
        expected_outcome="succeeded",
        expected_steps=(ExpectedStep("render", 3, True),),
        expected_refusals=(),
        expected_host_requirements=host,
    )
    assert refusal_scene.kind is SceneKind.REFUSAL
    assert refusal_scene.external == "refused.hip"
    assert refusal_scene.expected_refusals == (ExpectedRefusal("rop_batch", "rop1"),)


def test_a_refusal_with_no_subject_matches_any_subject(tmp_path: Path) -> None:
    """An omitted subject_contains is "", which every subject contains."""
    (scene,) = load_scenes(_write_scenes(tmp_path, [_refusal_entry()]))
    assert scene.expected_refusals == (ExpectedRefusal("rop_batch", ""),)


@pytest.mark.parametrize(
    "entry, rule",
    [
        pytest.param(
            _cook_entry(expect={"outcome": "succeeded"}),
            "a cook scene must list expected steps",
            id="cook_without_steps",
        ),
        pytest.param(
            _cook_entry(
                expect={
                    "outcome": "succeeded",
                    "steps": [{"name": "s", "task_count": 1, "declares_outputs": True}],
                    "refusals": [{"scenario": "rop_batch"}],
                }
            ),
            "a cook scene must list expected steps and no refusals",
            id="cook_with_refusals",
        ),
        pytest.param(
            _refusal_entry(expect={"outcome": "refused"}),
            "a refusal scene must list expected refusals",
            id="refusal_without_refusals",
        ),
        pytest.param(
            _refusal_entry(
                expect={
                    "outcome": "refused",
                    "refusals": [{"scenario": "rop_batch"}],
                    "steps": [{"name": "s", "task_count": 1, "declares_outputs": True}],
                }
            ),
            "a refusal scene must list expected refusals and no steps",
            id="refusal_with_steps",
        ),
        pytest.param(
            _cook_entry(source={"builder": "a.py", "external": "a.hip", "sha256": "0"}),
            "exactly one of builder and external",
            id="both_sources",
        ),
        pytest.param(_cook_entry(source={}), "exactly one of builder and external", id="no_source"),
        pytest.param(
            _cook_entry(source={"external": "a.hip"}),
            "an external source must have a sha256",
            id="external_without_sha256",
        ),
        pytest.param(
            _cook_entry(source={"external": "a.hip", "sha256": "0", "saved": False}),
            "only a builder source may have saved: false",
            id="unsaved_external",
        ),
        pytest.param(_cook_entry(kind="render"), "'render' is not a valid SceneKind", id="kind"),
        pytest.param(_cook_entry(set="nightly"), "'nightly' is not a valid SceneSet", id="set"),
        pytest.param(
            {k: v for k, v in _cook_entry().items() if k != "max_minutes"},
            "missing field max_minutes",
            id="missing_field",
        ),
    ],
)
def test_an_entry_that_breaks_a_rule_names_the_scene_and_the_rule(
    tmp_path: Path, entry: Dict[str, Any], rule: str
) -> None:
    """A bad entry fails when the tests are collected, with a message that says what to fix."""
    # A good entry first, so the bad one is named by its own ID.
    path: Path = _write_scenes(tmp_path, [_cook_entry("fine"), entry])
    with pytest.raises(ValueError) as raised:
        load_scenes(path)
    assert f"scenes.yaml: {entry['id']}: " in str(raised.value)
    assert rule in str(raised.value)


def test_a_builder_scene_may_be_unsaved(tmp_path: Path) -> None:
    """saved: false is allowed with a builder, for a scene the cook builds and never saves."""
    entry: Dict[str, Any] = _refusal_entry(source={"builder": "unsaved.py", "saved": False})
    (scene,) = load_scenes(_write_scenes(tmp_path, [entry]))
    assert scene.saved is False


def test_two_scenes_with_one_id_are_refused(tmp_path: Path) -> None:
    """The ID is the pytest ID, so two scenes can't share one."""
    path: Path = _write_scenes(tmp_path, [_cook_entry("same"), _refusal_entry("same")])
    with pytest.raises(ValueError, match="same: another scene has the same id"):
        load_scenes(path)


def test_an_entry_without_an_id_is_named_by_its_position(tmp_path: Path) -> None:
    """With no ID to name, the message still says which entry is wrong."""
    entry: Dict[str, Any] = {k: v for k, v in _cook_entry().items() if k != "id"}
    with pytest.raises(ValueError, match="scene 1: missing field id"):
        load_scenes(_write_scenes(tmp_path, [_cook_entry("fine"), entry]))


def test_an_entry_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    """A list item that isn't a mapping is named by its position."""
    path: Path = tmp_path / "scenes.yaml"
    path.write_text("version: 1\nscenes:\n  - just a string\n", encoding="utf-8")
    with pytest.raises(ValueError, match="scene 0: must be a mapping"):
        load_scenes(path)


def test_a_file_of_another_version_is_refused(tmp_path: Path) -> None:
    """A format this code doesn't know is refused rather than read wrongly."""
    with pytest.raises(ValueError, match="version: 1"):
        load_scenes(_write_scenes(tmp_path, [_cook_entry()], version=2))


def test_an_external_scene_is_copied_after_its_hash_matches(tmp_path: Path) -> None:
    """The copy keeps the file's name, for scenes whose paths use $HIPNAME."""
    # The pinned file in the scenes folder, and an empty folder to cook in.
    scenes_dir: Path = tmp_path / "scenes"
    scenes_dir.mkdir()
    (scenes_dir / "pyro.hip").write_bytes(_CONTENT)
    into: Path = tmp_path / "cook"
    into.mkdir()

    hip: Optional[Path] = _prepare(_scene(), scenes_dir, into)

    assert hip == into / "pyro.hip"
    assert hip.read_bytes() == _CONTENT


@settings(max_examples=25)
@given(content=st.binary(min_size=1, max_size=64))
def test_any_change_to_an_external_scene_is_refused(tmp_path_factory: Any, content: bytes) -> None:
    """For any file other than the pinned one, the test refuses to cook it."""
    # The pinned content itself is the one file that must pass.
    if content == _CONTENT:
        return
    folder: Path = tmp_path_factory.mktemp("scenes")
    (folder / "pyro.hip").write_bytes(content)
    with pytest.raises(ValueError, match="It is a different file"):
        _prepare(_scene(), folder, folder)


def test_an_external_scene_without_the_scenes_folder_names_the_variable(tmp_path: Path) -> None:
    """With the farm set, a missing folder fails the test and says which variable to set."""
    with pytest.raises(FileNotFoundError, match="Set PDG_TEST_SCENES_DIR"):
        _prepare(_scene(), None, tmp_path)


def test_a_missing_external_scene_names_the_file_and_the_variable(tmp_path: Path) -> None:
    """A folder without the scene fails the test, naming the file it needs."""
    with pytest.raises(FileNotFoundError) as raised:
        _prepare(_scene(), tmp_path, tmp_path)
    assert str(tmp_path / "pyro.hip") in str(raised.value)
    assert "PDG_TEST_SCENES_DIR" in str(raised.value)


def test_an_unsaved_scene_has_no_file(tmp_path: Path) -> None:
    """The cook builds an unsaved scene itself, so nothing is prepared and no hython runs."""
    scene: TestScene = _scene(builder="unsaved.py", external=None, sha256=None, saved=False)
    assert _prepare(scene, None, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def _builder_scene(builder: str) -> TestScene:
    """A builder scene."""
    return _scene(scene_id="built", builder=builder, external=None, sha256=None)


def test_a_builder_scene_is_saved_by_its_builder(tmp_path: Path) -> None:
    """The builder runs in hython with the given environment, and saves the scene it's told to."""
    # A stand-in for hython: Python, running a builder that writes an env var to its argument.
    builders: Path = tmp_path / "builders"
    builders.mkdir()
    (builders / "writes.py").write_text(
        "import os, sys\nopen(sys.argv[1], 'w').write(os.environ['SCENE_TEXT'])\n"
    )
    into: Path = tmp_path / "cook"
    into.mkdir()

    hip: Optional[Path] = prepare_scene_file(
        _builder_scene("writes.py"),
        hython=Path(sys.executable),
        env={**os.environ, "SCENE_TEXT": "built"},
        scenes_dir=None,
        into=into,
        builders_dir=builders,
    )

    assert hip == into / "built.hip"
    assert hip.read_text() == "built"


def test_a_builder_that_fails_reports_its_output(tmp_path: Path) -> None:
    """A builder that exits with an error, or saves nothing, fails with what it printed."""
    # One builder that fails, and one that succeeds without saving the scene.
    builders: Path = tmp_path / "builders"
    builders.mkdir()
    (builders / "fails.py").write_text("import sys\nprint('no such node')\nsys.exit(3)\n")
    (builders / "saves_nothing.py").write_text("print('done')\n")

    for builder, exit_text, printed in (
        ("fails.py", "exit 3", "no such node"),
        ("saves_nothing.py", "exit 0", "done"),
    ):
        with pytest.raises(RuntimeError) as raised:
            prepare_scene_file(
                _builder_scene(builder),
                hython=Path(sys.executable),
                env=dict(os.environ),
                scenes_dir=None,
                into=tmp_path,
                builders_dir=builders,
            )
        assert builder in str(raised.value)
        assert exit_text in str(raised.value)
        assert printed in str(raised.value)
