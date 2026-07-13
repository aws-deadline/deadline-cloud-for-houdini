# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import hou

from qtpy.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QWidget,
    QGridLayout,
    QVBoxLayout,
)
from qtpy.QtCore import Qt  # type: ignore
from deadline.client.job_bundle.submission import AssetReferences
from deadline.client.ui.dialogs.submit_job_to_deadline_dialog import SubmitJobToDeadlineDialog
from deadline.client.ui.pre_gui_hooks import (
    PreGuiHookContext,
    apply_pre_gui_output,
    run_pre_gui_hooks,
)
from deadline.client.dataclasses import SubmitterInfo
from deadline_cloud_for_houdini._version import version as houdini_submitter_version
from deadline_cloud_for_houdini._assets import _get_scene_asset_references
from deadline_cloud_for_houdini.hip_settings import HoudiniSubmitterUISettings
from deadline_cloud_for_houdini.houdini_submitter_widget import SceneSettingsWidget
from deadline_cloud_for_houdini.submitter import _pre_gui_hook_confirm_callback, submit_callback


class SubmitterPanel(QWidget):
    def __init__(self, parent, deadline_cloud_widget):
        super().__init__(parent=parent)
        self.deadline_cloud_widget = deadline_cloud_widget

        self._build_ui()

    def _build_ui(self):
        parent_layout = QVBoxLayout(self)
        frame_info_layout = QGridLayout(self)

        grid_row = 0
        frame_info_layout.addWidget(QLabel("Valid Frame Range"), grid_row, 0)
        frame_info_layout.addWidget(QComboBox(self), grid_row, 1)

        grid_row += 1
        frame_info_layout.addWidget(QLabel("Start/End/Inc"), grid_row, 0)
        frame_info_layout.addWidget(QLineEdit(self), grid_row, 1)
        frame_info_layout.addWidget(QLineEdit(self), grid_row, 2)
        frame_info_layout.addWidget(QLineEdit(self), grid_row, 3)

        grid_row += 1
        frame_info_layout.addWidget(QLabel("Render with Take"), grid_row, 0)
        frame_info_layout.addWidget(QComboBox(self), grid_row, 1)

        parent_layout.addLayout(frame_info_layout)
        parent_layout.addWidget(self.deadline_cloud_widget)


def onCreateInterface():

    # Convention for how Houdini references nodes in its Python panels; we can't include **kwargs in the function signature.
    # See https://www.sidefx.com/docs/houdini/ref/windows/pythonpaneleditor.html#interfaces-tab for more information.
    # Ignore this as to not set off the linter.
    n = kwargs["paneTab"].currentNode()  # type: ignore # noqa:F821

    ui_settings = HoudiniSubmitterUISettings()
    shared_parameter_values: dict = {}

    # Run pre-GUI hooks so studios can pre-populate dialog fields before it opens. Houdini has no
    # on-disk job bundle at this point, so hooks are sourced from DEADLINE_HOOKS_DIR only
    # (bundle_dir=None), gated by settings.allow_environment_hooks. The confirmation prompt is
    # skipped when auto_accept is set; otherwise the standard dialog is shown.
    pre_gui_output = run_pre_gui_hooks(
        PreGuiHookContext(
            bundle_dir=None,
            job_name=ui_settings.name,
            submitter_name="houdini",
            parameters=dict(shared_parameter_values),
        ),
        confirm_callback=_pre_gui_hook_confirm_callback(hou.qt.mainWindow()),
    )
    apply_pre_gui_output(pre_gui_output, ui_settings, shared_parameter_values)

    widget = SubmitJobToDeadlineDialog(
        job_setup_widget_type=SceneSettingsWidget,
        initial_job_settings=ui_settings,
        initial_shared_parameter_values=shared_parameter_values,
        auto_detected_attachments=_get_scene_asset_references(n),
        attachments=AssetReferences(),
        on_create_job_bundle_callback=submit_callback,
        # submitter_info replaces submitter_name as of deadline-cloud 0.54.0:
        # https://github.com/aws-deadline/deadline-cloud/releases/tag/0.54.0
        submitter_info=SubmitterInfo(
            submitter_name="Houdini",
            submitter_package_name="deadline-cloud-for-houdini",
            submitter_package_version=houdini_submitter_version,
            host_application_name="Houdini",
            host_application_version=hou.applicationVersionString(),
        ),
        f=Qt.Tool,
        show_host_requirements_tab=True,
    )

    panel = SubmitterPanel(parent=None, deadline_cloud_widget=widget)

    return panel
