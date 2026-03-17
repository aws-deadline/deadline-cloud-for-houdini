# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import hou

from qtpy.QtCore import Qt  # type: ignore
from qtpy.QtWidgets import QCheckBox, QWidget, QGridLayout, QLabel

from .hip_settings import HoudiniSubmitterUISettings

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
            initial_directory=hou.getenv("HIP"),
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

        # Arnold Export Settings
        qt_pos_index += 1
        arnold_label = QLabel("Arnold Export Settings")
        arnold_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        layout.addWidget(arnold_label, qt_pos_index, 0)

        qt_pos_index += 1
        self.arnold_auto_configure_check = QCheckBox(
            "Auto-configure Arnold ROPs for export", self
        )
        self.arnold_auto_configure_check.setToolTip(
            "Automatically configure Arnold ROP parameters before submission "
            "to ensure proper .ass file export and prevent known issues."
        )
        layout.addWidget(self.arnold_auto_configure_check, qt_pos_index, 0)

        qt_pos_index += 1
        self.arnold_ass_export_check = QCheckBox("Enable .ass file export", self)
        self.arnold_ass_export_check.setToolTip(
            "Set ar_ass_export_enable = 1 on Arnold ROPs so .ass files are written."
        )
        layout.addWidget(self.arnold_ass_export_check, qt_pos_index, 0)

        qt_pos_index += 1
        self.arnold_disable_image_check = QCheckBox(
            "Disable image render (prevent hang)", self
        )
        self.arnold_disable_image_check.setToolTip(
            "Clear ar_picture to prevent hython from hanging when set to 'ip' "
            "(interactive render mode)."
        )
        layout.addWidget(self.arnold_disable_image_check, qt_pos_index, 0)

        # Wire Arnold auto-configure to enable/disable sub-options
        self.arnold_auto_configure_check.stateChanged.connect(
            self._arnold_auto_configure_changed
        )

    def _load(self, settings: HoudiniSubmitterUISettings) -> None:
        """
        Populates the UI elements with the contents of `settings`.

        Args:
            settings: The Houdini-specific job settings to populate the UI with
        """

        self.separate_step_check.setChecked(settings.separate_steps)
        self.include_adaptor_wheels_check.setChecked(settings.include_adaptor_wheels)
        self.adaptor_wheels_directory_picker.setEnabled(settings.include_adaptor_wheels)

        # Arnold settings
        self.arnold_auto_configure_check.setChecked(settings.arnold_auto_configure)
        self.arnold_ass_export_check.setChecked(settings.arnold_settings.enable_ass_export)
        self.arnold_disable_image_check.setChecked(settings.arnold_settings.disable_image_render)
        self._arnold_auto_configure_changed(
            Qt.Checked if settings.arnold_auto_configure else Qt.Unchecked
        )

    def _include_adaptor_wheels_changed(self, state):
        self.adaptor_wheels_directory_picker.setEnabled(Qt.CheckState(state) == Qt.Checked)

    def _arnold_auto_configure_changed(self, state):
        enabled = Qt.CheckState(state) == Qt.Checked
        self.arnold_ass_export_check.setEnabled(enabled)
        self.arnold_disable_image_check.setEnabled(enabled)

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
        settings.auto_save_hip = self.auto_save_hip_check.isChecked()

        # Arnold settings
        settings.arnold_auto_configure = self.arnold_auto_configure_check.isChecked()
        settings.arnold_settings.enable_ass_export = self.arnold_ass_export_check.isChecked()
        settings.arnold_settings.disable_image_render = self.arnold_disable_image_check.isChecked()
