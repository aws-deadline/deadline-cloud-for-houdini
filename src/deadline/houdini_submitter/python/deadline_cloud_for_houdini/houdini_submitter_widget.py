# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import hou

from qtpy.QtCore import Qt  # type: ignore
from qtpy.QtWidgets import QCheckBox, QWidget, QGridLayout

from .hip_settings import HoudiniSubmitterUISettings

from deadline.client.ui.dialogs.submit_job_to_deadline_dialog import SubmitJobToDeadlineDialog
from deadline.client.ui.widgets.path_widgets import DirectoryPickerWidget


class SceneSettingsWidget(QWidget):
    def __init__(self, initial_settings: HoudiniSubmitterUISettings, parent=None):
        super().__init__(parent=parent)

        self.dev_options = os.environ.get("DEADLINE_ENABLE_DEVELOPER_OPTIONS", "").upper() == "TRUE"

        self._build_ui()
        self._load(initial_settings)

    def _build_ui(self) -> None:
        """Set up the UI."""
        layout = QGridLayout(self)

        qt_pos_index = 0

        self.separate_step_check = QCheckBox("Submit Dependencies as Separate Steps", self)
        layout.addWidget(self.separate_step_check, qt_pos_index, 0)

        qt_pos_index += 1
        self.include_adaptor_wheels_check = QCheckBox("Include Adaptor Wheels", self)
        layout.addWidget(self.include_adaptor_wheels_check, qt_pos_index, 0)

        qt_pos_index += 1
        self.adaptor_wheels_directory_picker = DirectoryPickerWidget(
            initial_directory=hou.getEnvConfigValue("HIP"),
            directory_label="Adaptor Wheels",
            parent=self,
        )

        layout.addWidget(self.adaptor_wheels_directory_picker, qt_pos_index, 0)

        self.include_adaptor_wheels_check.stateChanged.connect(self._include_adaptor_wheels_changed)

        qt_pos_index += 1
        self.auto_unlock_rops_check = QCheckBox("Automatically unlock ROPs", self)
        layout.addWidget(self.auto_unlock_rops_check, qt_pos_index, 0)

        qt_pos_index += 1
        self.auto_parse_hip_check = QCheckBox("Automatically parse scene (.hip) references", self)
        layout.addWidget(self.auto_parse_hip_check, qt_pos_index, 0)

        qt_pos_index += 1
        self.auto_save_hip_check = QCheckBox("Automatically save scene (.hip) file", self)
        layout.addWidget(self.auto_save_hip_check, qt_pos_index, 0)

    def _load(self, settings: HoudiniSubmitterUISettings) -> None:
        """
        Populates the UI elements with the contents of `settings`.

        Args:
            settings: The Houdini-specific job settings to populate the UI with
        """

        self.separate_step_check.setChecked(settings.separate_steps)
        self.include_adaptor_wheels_check.setChecked(settings.include_adaptor_wheels)
        self.adaptor_wheels_directory_picker.setEnabled(settings.include_adaptor_wheels)

    def _include_adaptor_wheels_changed(self, state):
        self.adaptor_wheels_directory_picker.setEnabled(Qt.CheckState(state) == Qt.Checked)

    def update_settings(self, settings: HoudiniSubmitterUISettings) -> None:
        """
        Updates the Houdini submitter settings according to values set in the job settings panel.

        Args:
            settings: A dataclass with Houdini-specific job settings to update
        """

        settings.separate_steps = self.separate_step_check.isChecked()
        settings.include_adaptor_wheels = self.include_adaptor_wheels_check.isChecked()
        if settings.include_adaptor_wheels:
            settings.adaptor_wheels_dir = self.adaptor_wheels_directory_picker.getText()
        settings.auto_unlock_rops = self.auto_unlock_rops_check.isChecked()
        settings.auto_parse_hip = self.auto_parse_hip_check.isChecked()
        self.auto_save_hip_check = self.auto_save_hip_check.isChecked()


def create_dialog(
    job_setup_widget_type,
    initial_job_settings,
    initial_shared_parameter_values,
    auto_detected_attachments,
    attachments,
    on_create_job_bundle_callback,
    submitter_name,
    show_host_requirements_tab,
) -> SubmitJobToDeadlineDialog:

    dialog = SubmitJobToDeadlineDialog(
        job_setup_widget_type=job_setup_widget_type,
        initial_job_settings=initial_job_settings,
        initial_shared_parameter_values=initial_shared_parameter_values,
        auto_detected_attachments=auto_detected_attachments,
        attachments=attachments,
        on_create_job_bundle_callback=on_create_job_bundle_callback,
        submitter_name=submitter_name,
        f=Qt.Tool,
        show_host_requirements_tab=show_host_requirements_tab,
    )
    return dialog
