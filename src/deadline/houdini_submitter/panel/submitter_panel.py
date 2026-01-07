# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from qtpy.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QWidget,
    QGridLayout,
    QVBoxLayout,
)
from deadline.client.job_bundle.submission import AssetReferences
from deadline_cloud_for_houdini._assets import _get_scene_asset_references
from deadline_cloud_for_houdini.hip_settings import HoudiniSubmitterUISettings
from deadline_cloud_for_houdini.houdini_submitter_widget import SceneSettingsWidget, create_dialog
from deadline_cloud_for_houdini.submitter import submit_callback


class SubmitterPanel(QWidget):
    def __init__(self, parent, deadline_cloud_widget):
        super().__init__(parent=parent)
        self.deadline_cloud_widget = deadline_cloud_widget

        self._build_ui()

    def _build_ui(self):
        parent_layout = QVBoxLayout(self)
        frame_info_layout = QGridLayout(self)

        qt_pos_index = 0
        frame_info_layout.addWidget(QLabel("Valid Frame Range"), qt_pos_index, 0)
        frame_info_layout.addWidget(QComboBox(self), qt_pos_index, 1)

        qt_pos_index += 1
        frame_info_layout.addWidget(QLabel("Start/End/Inc"), qt_pos_index, 0)
        frame_info_layout.addWidget(QLineEdit(self), qt_pos_index, 1)
        frame_info_layout.addWidget(QLineEdit(self), qt_pos_index, 2)
        frame_info_layout.addWidget(QLineEdit(self), qt_pos_index, 3)

        qt_pos_index += 1
        frame_info_layout.addWidget(QLabel("Render with Take"), qt_pos_index, 0)
        frame_info_layout.addWidget(QComboBox(self), qt_pos_index, 1)

        parent_layout.addLayout(frame_info_layout)
        parent_layout.addWidget(self.deadline_cloud_widget)


def onCreateInterface():

    # Convention for how Houdini references nodes in its Python panels; we can't include **kwargs in the function signature.
    # Ignore this as to not set off the linter.
    n = kwargs["paneTab"].currentNode()  # type: ignore # noqa:F821

    widget = create_dialog(
        job_setup_widget_type=SceneSettingsWidget,
        initial_job_settings=HoudiniSubmitterUISettings(),
        initial_shared_parameter_values={},
        auto_detected_attachments=_get_scene_asset_references(n),
        attachments=AssetReferences(),
        on_create_job_bundle_callback=submit_callback,
        submitter_name="Houdini Submitter",
        show_host_requirements_tab=True,
    )

    panel = SubmitterPanel(parent=None, deadline_cloud_widget=widget)

    return panel
