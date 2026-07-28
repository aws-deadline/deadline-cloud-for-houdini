# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from unittest.mock import Mock, patch

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.hip_settings import (
    HoudiniSubmitterUISettings,
)
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    write_ui_settings_to_node,
    _make_create_bundle_callback,
)


def _mock_node_with_parms():
    """Return a node whose .parm(name) yields a distinct, cached Mock per parameter name."""
    node = Mock()
    parms: dict = {}

    def get_parm(name):
        if name not in parms:
            parms[name] = Mock()
        return parms[name]

    node.parm = get_parm
    return node, parms


def test_write_ui_settings_to_node_writes_expected_parms():
    node, parms = _mock_node_with_parms()
    settings = HoudiniSubmitterUISettings(
        name="MyJob",
        description="a description",
        priority=75,
        initial_status="SUSPENDED",
        max_failed_tasks_count=3,
        max_retries_per_task=2,
        separate_steps=True,
        include_adaptor_wheels=True,
        adaptor_wheels_dir="/wheels",
        auto_unlock_rops=True,
        auto_parse_hip=False,
        auto_save_hip=True,
    )

    write_ui_settings_to_node(node, settings)

    parms["name"].set.assert_called_once_with("MyJob")
    parms["description"].set.assert_called_once_with("a description")
    parms["priority"].set.assert_called_once_with(75)
    # max_failed_tasks_count / max_retries_per_task map to the node's differently-named parms.
    parms["initial_status"].set.assert_called_once_with("SUSPENDED")
    parms["failed_tasks_limit"].set.assert_called_once_with(3)
    parms["task_retry_limit"].set.assert_called_once_with(2)
    # Toggles are written as ints.
    parms["separate_steps"].set.assert_called_once_with(1)
    parms["auto_unlock_rops"].set.assert_called_once_with(1)
    parms["auto_parse_hip"].set.assert_called_once_with(0)
    parms["auto_save_hip"].set.assert_called_once_with(1)
    parms["include_adaptor_wheels"].set.assert_called_once_with(1)
    parms["adaptor_wheels"].set.assert_called_once_with("/wheels")


def test_write_ui_settings_to_node_does_not_write_frame_or_take_parms():
    """Frame range/take are owned by the panel's frame controls and must not be clobbered."""
    node, parms = _mock_node_with_parms()

    write_ui_settings_to_node(node, HoudiniSubmitterUISettings())

    assert "trange" not in parms
    assert "f1" not in parms
    assert "f2" not in parms
    assert "f3" not in parms
    assert "take" not in parms


def test_write_ui_settings_to_node_defaults_empty_adaptor_wheels_dir():
    node, parms = _mock_node_with_parms()

    write_ui_settings_to_node(
        node, HoudiniSubmitterUISettings(include_adaptor_wheels=False, adaptor_wheels_dir=None)
    )

    parms["adaptor_wheels"].set.assert_called_once_with("")


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._create_job_bundle")
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.set_queue_parameter_values_from_openjd"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.write_ui_settings_to_node"
)
def test_create_bundle_callback_persists_before_bundling(
    mock_write, mock_set_queue_params, mock_create_bundle
):
    node = Mock()
    callback = _make_create_bundle_callback(node)

    settings = Mock()
    queue_parameters = [{"name": "CondaPackages", "value": "houdini=21.0.*"}]
    asset_references = Mock()

    result = callback(
        Mock(),  # widget
        "/tmp/bundle",
        settings,
        queue_parameters,
        asset_references,
        None,  # host_requirements
        purpose=Mock(),
    )

    mock_write.assert_called_once_with(node, settings)
    mock_set_queue_params.assert_called_once_with(node, queue_parameters)
    mock_create_bundle.assert_called_once_with(node, "/tmp/bundle", asset_references, None)
    assert result is None


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._create_job_bundle")
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.set_queue_parameter_values_from_openjd"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.write_ui_settings_to_node"
)
def test_create_bundle_callback_forwards_host_requirements(
    mock_write, mock_set_queue_params, mock_create_bundle
):
    node = Mock()
    callback = _make_create_bundle_callback(node)

    settings = Mock()
    queue_parameters = [{"name": "CondaPackages", "value": "houdini=21.0.*"}]
    asset_references = Mock()
    host_requirements = {"amounts": [{"name": "amount.worker.vcpu", "min": 8}]}

    result = callback(
        Mock(),  # widget
        "/tmp/bundle",
        settings,
        queue_parameters,
        asset_references,
        host_requirements,
        purpose=Mock(),
    )

    # Host requirements from the dialog must be forwarded to the bundle builder so they
    # end up in the generated template rather than being silently dropped.
    mock_create_bundle.assert_called_once_with(
        node, "/tmp/bundle", asset_references, host_requirements
    )
    assert result is None


@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._run_pre_submission_checks"
)
def test_pre_submission_gate_runs_checks_before_on_submit(mock_checks):
    """The gate must run the checks (with the dialog's current toggles) then call on_submit."""
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
        _install_pre_submission_gate,
    )

    order = []
    mock_checks.side_effect = lambda node, **kwargs: order.append("checks")

    dialog = Mock()
    dialog.on_submit.side_effect = lambda: order.append("submit")
    # The Scene Settings tab's current (unwritten) checkbox state.
    dialog.job_settings.auto_unlock_rops_check.isChecked.return_value = False
    dialog.job_settings.auto_parse_hip_check.isChecked.return_value = True
    dialog.job_settings.auto_save_hip_check.isChecked.return_value = False
    node = Mock()

    _install_pre_submission_gate(dialog, node)

    # The default Submit->on_submit wiring is removed and replaced with the guarded wrapper.
    dialog.submit_button.clicked.disconnect.assert_called_once_with(dialog.on_submit)
    guarded_submit = dialog.submit_button.clicked.connect.call_args.args[0]

    # Simulate the user clicking Submit.
    guarded_submit()

    # Checks must be run with the dialog's CURRENT toggle values (not read from the node).
    mock_checks.assert_called_once_with(
        node, auto_unlock_rops=False, auto_parse_hip=True, auto_save_hip=False
    )
    dialog.on_submit.assert_called_once_with()
    # Checks must happen strictly before the submission proceeds.
    assert order == ["checks", "submit"]


@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._run_pre_submission_checks"
)
def test_pre_submission_gate_aborts_submit_on_cancel(mock_checks):
    """If the checks raise UserInitiatedCancel, on_submit must NOT be called."""
    from deadline.client.exceptions import UserInitiatedCancel
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
        _install_pre_submission_gate,
    )

    mock_checks.side_effect = UserInitiatedCancel("Submission canceled.")

    dialog = Mock()
    dialog.job_settings.auto_unlock_rops_check.isChecked.return_value = False
    dialog.job_settings.auto_parse_hip_check.isChecked.return_value = False
    dialog.job_settings.auto_save_hip_check.isChecked.return_value = False
    node = Mock()

    _install_pre_submission_gate(dialog, node)
    guarded_submit = dialog.submit_button.clicked.connect.call_args.args[0]

    # Clicking Submit runs the checks; the cancel must abort without submitting or raising.
    guarded_submit()

    mock_checks.assert_called_once()
    dialog.on_submit.assert_not_called()


def test_pre_submission_checks_honor_auto_save_override_over_node_parm():
    """The unsaved-changes prompt must respect the dialog's current auto_save choice.

    Regression test: the node's ``auto_save_hip`` parm is 1 (would save silently), but the user
    unchecked "Automatically save scene" in the dialog. Passing ``auto_save_hip=False`` must make
    the check prompt instead of silently saving.
    """
    from .mock_hou import hou_module as hou
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter

    node = Mock()
    node.inputAncestors.return_value = [Mock()]
    # Node parm still says "auto save on" (stale).
    node.parm.return_value.eval.return_value = 1

    hou.ui.displayMessage.reset_mock()
    hou.ui.displayMessage.return_value = 2  # "Ignore"
    hou.hipFile.hasUnsavedChanges.return_value = True

    asset_refs = Mock()
    asset_refs.input_filenames = {"/scene.hip"}

    with (
        patch.object(submitter, "_get_evaluated_asset_references", return_value=asset_refs),
        patch.object(submitter, "_is_node_locked", return_value=False),
        patch.object(submitter, "_follow_fetch_nodes", side_effect=lambda n: n),
        patch.object(submitter, "_get_hip_file", return_value="/scene.hip"),
    ):
        submitter._run_pre_submission_checks(node, auto_save_hip=False)

    messages = [c.args[0] for c in hou.ui.displayMessage.call_args_list]
    assert any("unsaved changes" in str(m) for m in messages), messages
