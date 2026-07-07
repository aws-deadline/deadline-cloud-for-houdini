# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from deadline.client.api import UpdateCheckResult, UpdateCheckStatus

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.update_utils import (
    _check_for_update,
    _session_state,
    check_and_show_update_dialog,
)

MODULE = "deadline.houdini_submitter.python.deadline_cloud_for_houdini.update_utils"


@pytest.fixture(autouse=True)
def _reset_session_state():
    """Reset session state before each test."""
    _session_state.update_dismissed = False


class TestCheckForUpdate:
    """Tests for _check_for_update()."""

    @patch(f"{MODULE}.safe_check_for_updates")
    def test_passes_correct_integration_name(self, mock_check):
        # GIVEN
        mock_check.return_value = UpdateCheckResult(
            status=UpdateCheckStatus.SUCCESS,
            current_version="0.1.0",
        )

        # WHEN
        _check_for_update()

        # THEN
        call_kwargs = mock_check.call_args[1]
        assert call_kwargs["integration_name"] == "deadline-cloud-for-houdini"

    @patch(f"{MODULE}.safe_check_for_updates")
    def test_formats_version_from_tuple(self, mock_check):
        # GIVEN
        mock_check.return_value = UpdateCheckResult(
            status=UpdateCheckStatus.SUCCESS,
            current_version="1.2.3",
        )

        # WHEN
        with patch(f"{MODULE}.adaptor_version_tuple", (1, 2, 3, "final", 0)):
            _check_for_update()

        # THEN
        call_kwargs = mock_check.call_args[1]
        assert call_kwargs["current_version"] == "1.2.3"


class TestCheckAndShowUpdateDialog:
    """Tests for check_and_show_update_dialog()."""

    @patch(f"{MODULE}._check_for_update")
    def test_returns_false_when_no_update(self, mock_check):
        # GIVEN
        mock_check.return_value = UpdateCheckResult(
            status=UpdateCheckStatus.SUCCESS,
            current_version="0.1.0",
            update_available=False,
        )

        # WHEN
        result = check_and_show_update_dialog()

        # THEN
        assert result is False

    @patch(f"{MODULE}._check_for_update")
    def test_returns_false_on_error_status(self, mock_check):
        # GIVEN
        mock_check.return_value = UpdateCheckResult(
            status=UpdateCheckStatus.NETWORK_ERROR,
            current_version="0.1.0",
            update_available=False,
        )

        # WHEN
        result = check_and_show_update_dialog()

        # THEN
        assert result is False

    @patch(f"{MODULE}.UpdateAvailableDialog")
    @patch(f"{MODULE}._check_for_update")
    def test_returns_true_when_user_downloads(self, mock_check, mock_dialog_cls):
        # GIVEN
        mock_check.return_value = UpdateCheckResult(
            status=UpdateCheckStatus.SUCCESS,
            current_version="0.1.0",
            update_available=True,
            latest_version="0.2.0",
            download_url="https://example.com/installer",
        )
        mock_dialog = MagicMock()
        mock_dialog.user_downloaded = True
        mock_dialog_cls.return_value = mock_dialog

        # WHEN
        result = check_and_show_update_dialog()

        # THEN
        assert result is True
        mock_dialog.exec_.assert_called_once()

    @patch(f"{MODULE}.UpdateAvailableDialog")
    @patch(f"{MODULE}._check_for_update")
    def test_dismiss_sets_session_state(self, mock_check, mock_dialog_cls):
        # GIVEN
        mock_check.return_value = UpdateCheckResult(
            status=UpdateCheckStatus.SUCCESS,
            current_version="0.1.0",
            update_available=True,
            latest_version="0.2.0",
            download_url="https://example.com/installer",
        )
        mock_dialog = MagicMock()
        mock_dialog.user_downloaded = False
        mock_dialog_cls.return_value = mock_dialog

        # WHEN
        result = check_and_show_update_dialog()

        # THEN
        assert result is False
        assert _session_state.update_dismissed is True

    @patch(f"{MODULE}._check_for_update")
    def test_skips_check_when_previously_dismissed(self, mock_check):
        # GIVEN
        _session_state.update_dismissed = True

        # WHEN
        result = check_and_show_update_dialog()

        # THEN
        assert result is False
        mock_check.assert_not_called()

    @patch(f"{MODULE}.UpdateAvailableDialog")
    @patch(f"{MODULE}._check_for_update")
    def test_download_does_not_set_dismissed(self, mock_check, mock_dialog_cls):
        # GIVEN
        mock_check.return_value = UpdateCheckResult(
            status=UpdateCheckStatus.SUCCESS,
            current_version="0.1.0",
            update_available=True,
            latest_version="0.2.0",
            download_url="https://example.com/installer",
        )
        mock_dialog = MagicMock()
        mock_dialog.user_downloaded = True
        mock_dialog_cls.return_value = mock_dialog

        # WHEN
        check_and_show_update_dialog()

        # THEN
        assert _session_state.update_dismissed is False

    @patch(f"{MODULE}.UpdateAvailableDialog")
    @patch(f"{MODULE}._check_for_update")
    def test_dialog_receives_correct_release_notes_url(self, mock_check, mock_dialog_cls):
        # GIVEN
        mock_check.return_value = UpdateCheckResult(
            status=UpdateCheckStatus.SUCCESS,
            current_version="0.1.0",
            update_available=True,
            latest_version="0.2.0",
            download_url="https://example.com/installer",
        )
        mock_dialog = MagicMock()
        mock_dialog.user_downloaded = False
        mock_dialog_cls.return_value = mock_dialog

        # WHEN
        check_and_show_update_dialog()

        # THEN
        call_kwargs = mock_dialog_cls.call_args[1]
        assert (
            call_kwargs["release_notes_url"]
            == "https://github.com/aws-deadline/deadline-cloud-for-houdini/releases"
        )
