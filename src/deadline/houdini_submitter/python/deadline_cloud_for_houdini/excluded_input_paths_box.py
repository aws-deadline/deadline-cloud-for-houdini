# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""The Skip output folders, keep loaded caches checkbox and the Excluded input paths box, two
separate controls on the Job attachments tab of the ROP panel and the submit dialog."""

import logging
from typing import Any, Callable

from ._assets import (
    _EXCLUDED_INPUT_PATHS_PARM,
    _SKIP_OUTPUTS_KEEP_CACHES_PARM,
    _get_excluded_input_path_rows,
    _get_scene_asset_references,
    _is_skip_outputs_keep_caches_enabled,
    _set_excluded_input_path_rows,
)

_logger = logging.getLogger(__name__)

EXCLUDED_INPUT_PATHS_TITLE = "Excluded input paths"
EXCLUDED_INPUT_PATHS_HELP = (
    "Auto-detected inputs at or under these paths aren't uploaded. Type one path per line.\n"
    "\n"
    "Examples:\n"
    "  $HIP/cache          the cache folder and everything in it\n"
    "  **/cache            a folder named cache anywhere\n"
    "  $HIP/geo/*_tmp      anything in $HIP/geo whose name ends in _tmp\n"
    "  $HIP/render/sh01?   the shots named sh01 plus one more character, such as sh010 or sh01a\n"
    "\n"
    "Paths you add with Add... are always uploaded, unless they were already in the\n"
    "auto-detected list. The scene file and output folders are never excluded.\n"
    "\n"
    "With Skip output folders, keep loaded caches on, the cache version a File Cache node loads\n"
    "is still uploaded when you exclude its folder. To leave those files out too, name them:\n"
    "  $HIP/cache/*/v3/*.bgeo.sc\n"
    "With it off, that cache is left out with its folder."
)
SKIP_OUTPUTS_KEEP_CACHES_TITLE = "Skip output folders, keep loaded caches"
SKIP_OUTPUTS_KEEP_CACHES_HELP = (
    "When this is on:\n"
    "- output folders aren't uploaded;\n"
    "- the cache version a File Cache node loads is still uploaded when you exclude its"
    " cache folder.\n"
    "If a job reads files from an output folder, add that folder with Add... so it still"
    " uploads.\n"
    "Leave it off to upload these folders as before."
)
EXCLUDED_INPUT_PATHS_PLACEHOLDER = "One path per line, for example $HIP/cache"


def _save_and_refresh(
    node: Any, save: Callable[[], None], job_attachments: Any, absorbed: dict[str, set[str]]
) -> None:
    """Run save to change the node, then show the new auto-detected lists. Never raises.

    absorbed holds, per list, the paths the user added that an earlier refresh showed as auto.
    """
    try:
        save()
        if job_attachments is not None:
            scan = _get_scene_asset_references(node)
            # deadline-cloud's refresh drops an added path that the scan also lists, so keep it
            # here and put it back when a later scan no longer lists it.
            for list_name, paths in absorbed.items():
                added: set[str] = getattr(job_attachments.attachments, list_name)
                auto: set[str] = getattr(scan, list_name)
                added.update(paths - auto)
                paths.intersection_update(auto)
                paths.update(added & auto)
            job_attachments.refresh_ui(scan, None)
    except Exception as exc:
        _logger.warning("Couldn't update the job attachments after the change: %s", exc)


def _build_excluded_input_paths_box(node: Any, refresh: Callable[[], None]) -> tuple[Any, Any]:
    """Build the group box with a text box that saves each edit to the node and refreshes the
    lists after a pause in typing. Return the box and its pause timer."""
    from qtpy.QtCore import QTimer  # type: ignore
    from qtpy.QtWidgets import QGroupBox, QPlainTextEdit, QSizePolicy, QVBoxLayout  # type: ignore

    box = QGroupBox(EXCLUDED_INPUT_PATHS_TITLE)
    box.setToolTip(EXCLUDED_INPUT_PATHS_HELP)
    # Keep the box as tall as its text box wherever it's placed.
    box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    edit = QPlainTextEdit(box)
    edit.setPlaceholderText(EXCLUDED_INPUT_PATHS_PLACEHOLDER)
    edit.setToolTip(EXCLUDED_INPUT_PATHS_HELP)
    edit.setFixedHeight(4 * edit.fontMetrics().lineSpacing() + 12)
    edit.setPlainText("\n".join(_get_excluded_input_path_rows(node)))
    QVBoxLayout(box).addWidget(edit)

    timer = QTimer(box)
    timer.setSingleShot(True)
    timer.setInterval(700)

    def save_rows() -> None:
        # Save right away, so closing the pane mid-pause keeps the edit. Only the scan waits.
        _set_excluded_input_path_rows(node, edit.toPlainText().splitlines())
        timer.start()

    edit.textChanged.connect(save_rows)
    timer.timeout.connect(refresh)
    return box, timer


def add_to_dialog_top(dialog: Any) -> Callable[[Any], None]:
    """Fallback for the submit dialog: put the widgets above the tabs, in the order given."""
    placed: list[Any] = []

    def insert_below_placed(widget: Any) -> None:
        dialog.layout().insertWidget(len(placed), widget)
        placed.append(widget)

    return insert_below_placed


def _insert_after(
    container: Any, anchor: Any, widget: Any, anchor_label: str, fallback: Callable
) -> None:
    """Insert widget right after anchor in container's layout, or call fallback(widget) and log
    one warning when the anchor isn't in that layout."""
    layout: Any = container.layout() if container is not None else None
    index: int = layout.indexOf(anchor) if layout is not None and anchor is not None else -1
    if index >= 0:
        layout.insertWidget(index + 1, widget)
    else:
        _logger.warning(
            "Couldn't find %s in the Job attachments tab, so a control is shown in another place.",
            anchor_label,
        )
        fallback(widget)


def add_auto_input_controls(dialog: Any, node: Any, fallback: Callable[[Any], None]) -> list[Any]:
    """Add the Skip output folders, keep loaded caches checkbox under Require all input paths
    exist and the Excluded input paths box after Attach input directories, for each parm the node
    has. Never raises."""
    controls: list[Any] = []
    job_attachments = getattr(dialog, "job_attachments", None)
    absorbed: dict[str, set[str]] = {
        name: set() for name in ("input_filenames", "input_directories", "output_directories")
    }
    try:
        toggle_parm = node.parm(_SKIP_OUTPUTS_KEEP_CACHES_PARM)
        if toggle_parm is not None:
            from qtpy.QtWidgets import QCheckBox  # type: ignore

            checkbox = QCheckBox(SKIP_OUTPUTS_KEEP_CACHES_TITLE)
            checkbox.setToolTip(SKIP_OUTPUTS_KEEP_CACHES_HELP)
            checkbox.setChecked(_is_skip_outputs_keep_caches_enabled(node))
            checkbox.toggled.connect(
                lambda checked: _save_and_refresh(
                    node, lambda: toggle_parm.set(int(checked)), job_attachments, absorbed
                )
            )
            # The row under Require all input paths exist, in General submission settings.
            _insert_after(
                getattr(job_attachments, "general_group", None),
                getattr(job_attachments, "general_settings", None),
                checkbox,
                "Require all input paths exist",
                fallback,
            )
            controls.append(checkbox)
    except Exception as exc:
        _logger.warning(
            "Couldn't add the Skip output folders, keep loaded caches checkbox: %s", exc
        )
    try:
        if node.parm(_EXCLUDED_INPUT_PATHS_PARM) is not None:

            def refresh_lists() -> None:
                _save_and_refresh(node, lambda: None, job_attachments, absorbed)

            box, timer = _build_excluded_input_paths_box(node, refresh_lists)
            _insert_after(
                job_attachments,
                getattr(job_attachments, "input_directories_group", None),
                box,
                "Attach input directories",
                fallback,
            )

            def refresh_pending_edit() -> None:
                # Submitting or saving a bundle mid-pause would use the old lists.
                if timer.isActive():
                    timer.stop()
                    refresh_lists()

            for button_name in ("submit_button", "export_bundle_button"):
                button = getattr(dialog, button_name, None)
                if button is not None:
                    button.pressed.connect(refresh_pending_edit)
            controls.append(box)
    except Exception as exc:
        _logger.warning("Couldn't add the Excluded input paths box: %s", exc)
    return controls
