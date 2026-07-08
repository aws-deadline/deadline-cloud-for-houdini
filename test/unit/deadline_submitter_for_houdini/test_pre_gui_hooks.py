# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Unit tests for the Houdini submitter's pre-GUI hook integration.

``onCreateInterface`` (in the submitter panel) calls deadline-cloud's ``run_pre_gui_hooks``
(env-only, since Houdini has no on-disk bundle) and then applies the merged output onto its own
``HoudiniSubmitterUISettings`` + the dialog's shared parameter values via deadline-cloud's generic
``apply_pre_gui_output``. The mapping logic lives in (and is tested by) deadline-cloud; what is
DCC-owned -- and what these tests guard -- is that ``HoudiniSubmitterUISettings`` satisfies that
function's contract: assignable ``name`` / ``description`` and no ``.parameters`` list, so every
hook parameter flows to the shared values the dialog is seeded with.

The panel itself needs the real Qt dialog and a running Houdini, so it is exercised in the
integration suite; here we drive the real ``apply_pre_gui_output`` against a real settings object
headless (Qt is stubbed by ``test/unit/deadline_submitter_for_houdini/__init__``).
"""

from deadline.client.ui.pre_gui_hooks import apply_pre_gui_output

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.hip_settings import (
    HoudiniSubmitterUISettings,
)


def _settings() -> HoudiniSubmitterUISettings:
    s = HoudiniSubmitterUISettings()
    s.name = "Original"
    s.description = ""
    return s


def test_name_and_description_applied_to_settings():
    """A hook's name/description overwrite the settings fields."""
    settings = _settings()
    shared: dict = {}

    apply_pre_gui_output({"name": "PREGUI RAN", "description": "from pipeline"}, settings, shared)

    assert settings.name == "PREGUI RAN"
    assert settings.description == "from pipeline"


def test_hook_parameters_flow_to_shared_values():
    """HoudiniSubmitterUISettings has no .parameters list, so every hook parameter (queue params,
    deadline: properties) lands in the shared values the dialog is seeded with."""
    settings = _settings()
    shared = {"RezPackages": "houdini-20 deadline_cloud_for_houdini"}

    apply_pre_gui_output(
        {
            "parameters": {
                "deadline:priority": 88,
                "RezPackages": "houdini-20 custom_pkg",  # overrides the default
            }
        },
        settings,
        shared,
    )

    assert shared["deadline:priority"] == 88
    assert shared["RezPackages"] == "houdini-20 custom_pkg"


def test_empty_output_is_a_noop():
    """No pre-GUI hook output leaves the settings and shared values unchanged."""
    settings = _settings()
    shared = {"RezPackages": "pkg"}

    apply_pre_gui_output({}, settings, shared)

    assert settings.name == "Original"
    assert settings.description == ""
    assert shared == {"RezPackages": "pkg"}


def test_partial_output_only_touches_present_keys():
    """Only the keys present in the output are applied; others keep their prior values."""
    settings = _settings()
    settings.description = "keep me"
    shared: dict = {}

    apply_pre_gui_output({"name": "NewName"}, settings, shared)

    assert settings.name == "NewName"
    assert settings.description == "keep me"  # not overwritten
    assert shared == {}  # no parameters in output
