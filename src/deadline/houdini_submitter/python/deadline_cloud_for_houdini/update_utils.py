# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Utilities for checking and displaying update notifications."""

import logging
from dataclasses import dataclass

from deadline.client.api import safe_check_for_updates, UpdateCheckResult, UpdateCheckStatus
from deadline.client.ui.dialogs.update_available_dialog import UpdateAvailableDialog

from ._version import version_tuple as adaptor_version_tuple

logger = logging.getLogger(__name__)


@dataclass
class _SessionState:
    """Session-level state: once the user dismisses the update dialog,
    don't show it again until Houdini is restarted."""

    update_dismissed: bool = False


_session_state = _SessionState()


def _check_for_update() -> UpdateCheckResult:
    """Check if a newer version of the Houdini submitter is available.

    Returns:
        An UpdateCheckResult describing whether an update is available.
    """
    current_version = ".".join(str(v) for v in adaptor_version_tuple[:3])
    return safe_check_for_updates(
        integration_name="deadline-cloud-for-houdini",
        current_version=current_version,
    )


def check_and_show_update_dialog() -> bool:
    """Check for updates and show the update dialog if one is available.

    If the user previously dismissed the dialog in this Houdini session,
    the check is skipped entirely.

    Returns:
        True if the user clicked Download (caller should skip opening the submitter),
        False otherwise.
    """
    if _session_state.update_dismissed:
        return False

    update_result = _check_for_update()
    if update_result.status != UpdateCheckStatus.SUCCESS:
        return False

    if not update_result.update_available:
        return False

    update_dialog = UpdateAvailableDialog(
        integration_name="Houdini",
        current_version=update_result.current_version or "",
        latest_version=update_result.latest_version or "",
        download_url=update_result.download_url or "",
        release_notes_url="https://github.com/aws-deadline/deadline-cloud-for-houdini/releases",
    )
    update_dialog.exec_()
    if update_dialog.user_downloaded:
        return True
    # User dismissed — suppress for the rest of this Houdini session
    _session_state.update_dismissed = True
    return False
