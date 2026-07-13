# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Unit tests for the Houdini submitter's pre-GUI hook integration.

``onCreateInterface`` (in the submitter panel) calls deadline-cloud's ``run_pre_gui_hooks``
(env-only, since Houdini has no on-disk bundle) and then applies the merged output onto its own
``HoudiniSubmitterUISettings`` + the dialog's shared parameter values via deadline-cloud's generic
``apply_pre_gui_output``. The panel itself needs the real Qt dialog and a running Houdini, so it is
exercised in the integration suite; here we verify the DCC-owned pieces headless:

* ``apply_pre_gui_output`` routes hook output correctly against Houdini's own
  ``HoudiniSubmitterUISettings`` -- which has no ``.parameters`` list, so every hook parameter must
  land in the shared parameter values. This guards against a regression where
  ``HoudiniSubmitterUISettings`` gains a ``parameters`` attribute that would misroute hook params.
* ``_pre_gui_hook_confirm_callback`` honours the ``settings.auto_accept`` setting.

The hou / qtpy modules are stubbed by ``test/unit/deadline_submitter_for_houdini/__init__`` so
imports resolve.
"""

import sys
from typing import Optional, Tuple
from unittest.mock import MagicMock, patch

from deadline.client.ui.pre_gui_hooks import apply_pre_gui_output

from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter
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


def test_falsy_output_is_a_noop():
    """The panel passes ``pre_gui_output or {}`` into apply_pre_gui_output, so the values the
    no-hooks / declined-confirmation path can produce -- ``{}`` today, or ``None`` if the
    contract ever changed -- must both be safe no-ops that leave settings/shared untouched."""
    falsy_values: Tuple[Optional[dict], ...] = ({}, None)
    for falsy in falsy_values:
        settings = _settings()
        shared = {"RezPackages": "houdini-20 deadline_cloud_for_houdini"}

        # Mirror the panel call site: `pre_gui_output or {}`.
        apply_pre_gui_output(falsy or {}, settings, shared)

        assert settings.name == "Original"
        assert settings.description == ""
        assert shared == {"RezPackages": "houdini-20 deadline_cloud_for_houdini"}


@patch.object(submitter, "get_setting", return_value="true")
def test_confirm_callback_none_when_auto_accept_enabled(mock_get_setting):
    """With settings.auto_accept enabled, hooks run without a confirmation prompt."""
    assert submitter._pre_gui_hook_confirm_callback(parent=None) is None
    mock_get_setting.assert_called_once_with("settings.auto_accept")


@patch.object(submitter, "get_setting", return_value="false")
def test_confirmation_dialog_fires_when_auto_accept_disabled(mock_get_setting):
    """With settings.auto_accept disabled, invoking the returned callback actually shows the
    confirmation dialog (QMessageBox.question), parented to the passed-in window.

    This exercises the real ``qt_hook_confirmation`` callback rather than mocking it out, so it
    verifies the prompt fires -- not merely that a non-None callback was selected.
    ``run_pre_gui_hooks`` invokes ``confirm_callback(sources)`` with the hook sources; an empty
    list is enough to reach the dialog. The user's answer is mapped from the QMessageBox reply.

    ``qt_hook_confirmation`` runs ``from qtpy.QtWidgets import QMessageBox`` when it is *called*
    (the import is in its body, before the returned closure), binding ``QMessageBox`` as a
    closure local. So we install a controlled ``qtpy.QtWidgets`` in ``sys.modules`` around the
    ``_pre_gui_hook_confirm_callback`` call itself -- patching the module entry, rather than a
    ``qtpy.QtWidgets.QMessageBox`` attribute, makes the binding deterministic regardless of
    whether a real qtpy is also installed or which xdist worker runs this test.
    """
    fake_qtwidgets = MagicMock()
    fake_qtwidgets.QMessageBox.question.return_value = fake_qtwidgets.QMessageBox.Yes

    with patch.dict(sys.modules, {"qtpy.QtWidgets": fake_qtwidgets}):
        callback = submitter._pre_gui_hook_confirm_callback(parent="mainwin")
        assert callback is not None
        result = callback([])  # no hook sources needed to reach the dialog

    question = fake_qtwidgets.QMessageBox.question
    assert question.call_count == 1
    # The dialog is parented to the window passed into the submitter.
    assert question.call_args[0][0] == "mainwin"
    # "Yes" reply -> proceed.
    assert result is True
