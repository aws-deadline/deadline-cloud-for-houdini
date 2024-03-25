# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import os
from typing import Generator
from unittest.mock import Mock, PropertyMock, call, patch

import pytest

import deadline.houdini_adaptor.HoudiniAdaptor.adaptor as adaptor_module
from deadline.houdini_adaptor.HoudiniAdaptor import HoudiniAdaptor
from deadline.houdini_adaptor.HoudiniAdaptor.adaptor import (
    _REQUIRED_HOUDINI_INIT_KEYS,
    HoudiniNotRunningError,
)


@pytest.fixture()
def init_data() -> dict:
    """
    Pytest Fixture to return an init_data dictionary that passes validation

    Returns:
        dict: An init_data dictionary
    """
    return {
        "scene_file": "/path/to/scene/houdiniscene-19.5.hip",
        "version": "19.5.435",
        "render_node": "mantra1",
        "wedge_node": "",
        "wedgenum": "",
        "ignore_input_nodes": True,
    }


@pytest.fixture()
def run_data() -> dict:
    """
    Pytest Fixture to return a run_data dictionary that passes validation

    Returns:
        dict: A run_data dictionary
    """
    return {"frame": 1}


@pytest.fixture(autouse=True)
def mock_config() -> Generator[Mock, None, None]:
    config_mock = Mock()
    config_mock.get_executable_path.return_value = "/path/to/houdini/hython"

    with patch.object(HoudiniAdaptor, "config", new_callable=PropertyMock) as mock:
        mock.return_value = config_mock
        yield config_mock


class TestHoudiniAdaptor_on_start:
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._get_deadline_telemetry_client"
    )
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.ActionsQueue.__len__", return_value=0)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_no_error(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
        mock_telemetry_client: Mock,
        init_data: dict,
    ) -> None:
        """Tests that on_start completes without error"""
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"
        adaptor.on_start()

    @patch("time.sleep")
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._get_deadline_telemetry_client"
    )
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.ActionsQueue.__len__", return_value=0)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_waits_for_server_socket(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
        mock_telemetry_client: Mock,
        mock_sleep: Mock,
        init_data: dict,
    ) -> None:
        """Tests that the adaptor waits until the server socket is available"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        socket_mock = PropertyMock(
            side_effect=[None, None, None, "/tmp/9999", "/tmp/9999", "/tmp/9999"]
        )
        type(mock_server.return_value).server_path = socket_mock

        # WHEN
        adaptor.on_start()

        # THEN
        assert mock_sleep.call_count == 3

    @patch("threading.Thread")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_server_init_fail(self, mock_server: Mock, mock_thread: Mock, init_data: dict) -> None:
        """Tests that an error is raised if no socket becomes available"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)

        with (
            patch.object(adaptor, "_SERVER_START_TIMEOUT_SECONDS", 0.01),
            pytest.raises(RuntimeError) as exc_info,
        ):
            # WHEN
            adaptor.on_start()

        # THEN
        assert (
            str(exc_info.value)
            == "Could not find a socket because the server did not finish initializing"
        )

    @patch.object(adaptor_module.os.path, "isfile", return_value=False)
    def test_client_not_found(
        self,
        mock_isfile: Mock,
        init_data: dict,
    ) -> None:
        """Tests that the an error is raised if the houdini client file cannot be found"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        test_dir = "test_dir"

        with patch.object(adaptor_module.sys, "path", ["unreported_dir", test_dir]):
            with pytest.raises(FileNotFoundError) as exc_info:
                # WHEN
                adaptor._get_houdini_client_path()

        # THEN
        error_msg = (
            "Could not find houdini_client.py. Check that the HoudiniClient package is in "
            f"one of the following directories: {[test_dir]}"
        )
        assert str(exc_info.value) == error_msg
        mock_isfile.assert_called_with(
            os.path.join(
                test_dir, "deadline", "houdini_adaptor", "HoudiniClient", "houdini_client.py"
            )
        )

    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.ActionsQueue.__len__", return_value=1)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_houdini_init_timeout(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
        init_data: dict,
    ) -> None:
        """
        Tests that a TimeoutError is raised if the houdini client does not complete initialization
        tasks within a given time frame
        """
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"
        new_timeout = 0.01

        with (
            patch.object(adaptor, "_HOUDINI_START_TIMEOUT_SECONDS", new_timeout),
            pytest.raises(TimeoutError) as exc_info,
        ):
            # WHEN
            adaptor.on_start()

        # THEN
        error_msg = (
            f"Houdini did not complete initialization actions in {new_timeout} seconds and "
            "failed to start."
        )
        assert str(exc_info.value) == error_msg

    @patch.object(HoudiniAdaptor, "_houdini_is_running", False)
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._get_deadline_telemetry_client"
    )
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.ActionsQueue.__len__", return_value=1)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_houdini_init_fail(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
        mock_telemetry_client: Mock,
        init_data: dict,
    ) -> None:
        """
        Tests that an RuntimeError is raised if the houdini client encounters an exception
        """
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"

        with pytest.raises(RuntimeError) as exc_info:
            # WHEN
            adaptor.on_start()

        # THEN
        error_msg = (
            "Houdini encountered an error and was not able to complete initialization actions."
        )
        assert str(exc_info.value) == error_msg

    @patch.object(HoudiniAdaptor, "_action_queue")
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._get_deadline_telemetry_client"
    )
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_populate_action_queue(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_telemetry_client: Mock,
        mock_actions_queue: Mock,
        init_data: dict,
    ) -> None:
        """Tests that the action queue is populated correctly"""
        # GIVEN
        mock_actions_queue.__len__.return_value = 0
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"

        # WHEN
        adaptor.on_start()

        # THEN
        calls = mock_actions_queue.enqueue_action.call_args_list
        for _call, name in zip(
            calls[: len(_REQUIRED_HOUDINI_INIT_KEYS)], _REQUIRED_HOUDINI_INIT_KEYS
        ):
            assert _call.args[0].name == name, f"Action: {name} missing from required init actions"

    @patch.object(HoudiniAdaptor, "_action_queue")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_populate_action_queue_less_init_data(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
    ) -> None:
        """
        Tests that the action queue is populated correctly when not all keys are in the init data
        """
        # GIVEN
        init_data = {
            "version": "19.5",
            "render_node": "real_node",
        }
        missing_field = "scene_file"
        mock_actions_queue.__len__.return_value = 0
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"

        # WHEN
        try:
            adaptor.on_start()
        except Exception as ve:
            text = str(ve).partition("\n")[0]

        # THEN
        assert text == f"'{missing_field}' is a required property"


class TestHoudiniAdaptor_on_run:
    @patch("time.sleep")
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._get_deadline_telemetry_client"
    )
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.ActionsQueue.__len__", return_value=0)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_on_run(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
        mock_telemetry_client: Mock,
        mock_sleep: Mock,
        init_data: dict,
        run_data: dict,
    ) -> None:
        """Tests that on_run waits for completion"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"
        # First side_effect value consumed by setter
        is_rendering_mock = PropertyMock(side_effect=[None, True, False])
        HoudiniAdaptor._is_rendering = is_rendering_mock
        adaptor.on_start()

        # WHEN
        adaptor.on_run(run_data)

        # THEN
        mock_sleep.assert_called_once_with(0.1)

    @patch("time.sleep")
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._is_rendering",
        new_callable=PropertyMock,
    )
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._houdini_is_running",
        new_callable=PropertyMock,
    )
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._get_deadline_telemetry_client"
    )
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.ActionsQueue.__len__", return_value=0)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_on_run_render_fail(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
        mock_telemetry_client: Mock,
        mock_houdini_is_running: Mock,
        mock_is_rendering: Mock,
        mock_sleep: Mock,
        init_data: dict,
        run_data: dict,
    ) -> None:
        """Tests that on_run raises an error if the render fails"""
        # GIVEN
        mock_is_rendering.side_effect = [None, True, False]
        mock_houdini_is_running.side_effect = [True, True, True, False, False]
        mock_logging_subprocess.return_value.returncode = 1
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"
        adaptor.on_start()

        # WHEN
        with pytest.raises(HoudiniNotRunningError) as exc_info:
            adaptor.on_run(run_data)

        # THEN
        mock_sleep.assert_called_once_with(0.1)
        assert str(exc_info.value) == (
            "Houdini exited early and did not render successfully, please check render logs. "
            "Exit code 1"
        )


class TestHoudiniAdaptor_on_stop:
    @patch("time.sleep")
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._get_deadline_telemetry_client"
    )
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.ActionsQueue.__len__", return_value=0)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_on_stop(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
        mock_telemetry_client: Mock,
        mock_sleep: Mock,
        init_data: dict,
        run_data: dict,
    ) -> None:
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"
        is_rendering_mock = PropertyMock(return_value=False)
        HoudiniAdaptor._is_rendering = is_rendering_mock
        adaptor.on_start()
        adaptor.on_run(run_data)

        try:
            # WHEN
            adaptor.on_stop()
        except Exception as e:
            pytest.fail(f"Test raised an exception when it shouldn't have: {e}")
        else:
            # THEN
            pass  # on_stop ran without exception


class TestHoudiniAdaptor_on_cleanup:
    @patch("time.sleep")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor._logger")
    def test_on_cleanup_houdini_not_graceful_shutdown(
        self, mock_logger: Mock, mock_sleep: Mock, init_data: dict
    ) -> None:
        """Tests that on_cleanup reports when houdini does not gracefully shutdown"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)

        with (
            patch(
                "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._houdini_is_running",
                new_callable=lambda: True,
            ),
            patch.object(adaptor, "_HOUDINI_END_TIMEOUT_SECONDS", 0.01),
            patch.object(adaptor, "_houdini_client") as mock_client,
        ):
            # WHEN
            adaptor.on_cleanup()

        # THEN
        mock_logger.error.assert_called_once_with(
            (
                "Houdini did not complete cleanup actions and "
                "failed to gracefully shutdown. Terminating."
            )
        )
        mock_client.terminate.assert_called_once()

    @patch("time.sleep")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor._logger")
    def test_on_cleanup_server_not_graceful_shutdown(
        self, mock_logger: Mock, mock_sleep: Mock, init_data: dict
    ) -> None:
        """Tests that on_cleanup reports when the server does not shutdown"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)

        with (
            patch(
                "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._houdini_is_running",
                new_callable=lambda: False,
            ),
            patch.object(adaptor, "_SERVER_END_TIMEOUT_SECONDS", 0.01),
            patch.object(adaptor, "_server_thread") as mock_server_thread,
        ):
            mock_server_thread.is_alive.return_value = True
            # WHEN
            adaptor.on_cleanup()

        # THEN
        mock_logger.error.assert_called_once_with("Failed to shutdown the Houdini Adaptor server.")
        mock_server_thread.join.assert_called_once_with(timeout=0.01)

    @patch("time.sleep")
    @patch(
        "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._get_deadline_telemetry_client"
    )
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.ActionsQueue.__len__", return_value=0)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.LoggingSubprocess")
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.AdaptorServer")
    def test_on_cleanup(
        self,
        mock_server: Mock,
        mock_logging_subprocess: Mock,
        mock_actions_queue: Mock,
        mock_telemetry_client: Mock,
        mock_sleep: Mock,
        init_data: dict,
        run_data: dict,
    ) -> None:
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        mock_server.return_value.server_path = "/tmp/9999"
        is_rendering_mock = PropertyMock(return_value=False)
        HoudiniAdaptor._is_rendering = is_rendering_mock

        adaptor.on_start()
        adaptor.on_run(run_data)
        adaptor.on_stop()

        with patch(
            "deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor._houdini_is_running",
            new_callable=lambda: False,
        ):
            # WHEN
            try:
                adaptor.on_cleanup()
                assert not adaptor._houdini_is_rendering
            except Exception:
                assert False  # Assert no exceptions occured

        # THEN
        return  # Assert no errors occured

    def test_regex_callbacks_cache(self, init_data):
        """Test that regex callbacks are generated exactly once"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)

        # WHEN
        regex_callbacks = adaptor._get_regex_callbacks()

        # THEN
        assert regex_callbacks is adaptor._regex_callbacks

    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor.update_status")
    def test_handle_complete(self, mock_update_status: Mock, init_data: dict):
        """Tests that the _handle_complete method updates the progress correctly"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        regex_callbacks = adaptor._get_regex_callbacks()
        complete_regex = regex_callbacks[0].regex_list[0]

        # WHEN
        match = complete_regex.search("HoudiniClient: Finished Rendering Frame 1")
        assert match is not None
        adaptor._handle_complete(match)

        # THEN
        mock_update_status.assert_called_once_with(progress=100)

    handle_progess_params = [
        (
            (1, 0),
            (
                "ALF_PROGRESS 10%",
                "Finished Rendering.",
            ),
            (10.0, 100.0),
        )
    ]

    @pytest.mark.parametrize("regex_index, stdout, expected_progress", handle_progess_params)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor.update_status")
    @patch.object(HoudiniAdaptor, "_is_rendering", new_callable=PropertyMock(return_value=True))
    def test_handle_progress(
        self,
        mock_is_rendering: Mock,
        mock_update_status: Mock,
        regex_index: tuple[int, int],
        stdout: tuple[str, str],
        expected_progress: tuple[float, float],
        init_data: dict,
    ) -> None:
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)
        regex_callbacks = adaptor._get_regex_callbacks()
        progress_index, output_complete_index = regex_index
        progress_regex = regex_callbacks[progress_index].regex_list[0]
        output_complete_regex = regex_callbacks[output_complete_index].regex_list[0]

        # WHEN
        if progress_match := progress_regex.search(stdout[0]):
            adaptor._handle_progress(progress_match)
        if output_complete_match := output_complete_regex.search(stdout[1]):
            adaptor._handle_complete(output_complete_match)

        # THEN
        assert progress_match is not None
        assert output_complete_match is not None
        mock_update_status.assert_has_calls(
            [call(progress=progress) for progress in expected_progress]
        )

    handle_error_params = [
        ("ERROR: Something terrible happened", True),
        ("Error: Something terrible happened", True),
        ("Eddy[ERROR] - Something terrible happened", True),
    ]

    @pytest.mark.parametrize("stdout, regex_valid", handle_error_params)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor.update_status")
    @patch.object(HoudiniAdaptor, "_is_rendering", new_callable=PropertyMock(return_value=True))
    def test_handle_error_valid_regex(
        self,
        mock_is_rendering: Mock,
        mock_update_status: Mock,
        stdout: str,
        regex_valid: bool,
        init_data: dict,
    ) -> None:
        # GIVEN
        ERROR_CALLBACK_INDEX = 2
        init_data["strict_error_checking"] = True
        adaptor = HoudiniAdaptor(init_data)
        regex_callbacks = adaptor._get_regex_callbacks()
        error_regex = regex_callbacks[ERROR_CALLBACK_INDEX].regex_list[0]
        print(error_regex.search(stdout))
        if match := error_regex.search(stdout):
            # WHEN
            adaptor._handle_error(match)

        # THEN
        assert match
        assert isinstance(adaptor._exc_info, RuntimeError)
        assert str(adaptor._exc_info) == f"Houdini Encountered an Error: {stdout}"

    handle_error_params = [
        ("Error : Something terrible happened", False),
    ]

    @pytest.mark.parametrize("stdout, regex_valid", handle_error_params)
    @patch("deadline.houdini_adaptor.HoudiniAdaptor.adaptor.HoudiniAdaptor.update_status")
    @patch.object(HoudiniAdaptor, "_is_rendering", new_callable=PropertyMock(return_value=True))
    def test_handle_error_invalid_regex(
        self,
        mock_is_rendering: Mock,
        mock_update_status: Mock,
        stdout: str,
        regex_valid: bool,
        init_data: dict,
    ) -> None:
        # GIVEN
        ERROR_CALLBACK_INDEX = 2
        init_data["strict_error_checking"] = True
        adaptor = HoudiniAdaptor(init_data)
        regex_callbacks = adaptor._get_regex_callbacks()
        error_regex = regex_callbacks[ERROR_CALLBACK_INDEX].regex_list[0]
        print(error_regex.search(stdout))
        if match := error_regex.search(stdout):
            # WHEN
            adaptor._handle_error(match)

        # THEN
        assert not match

    def test_handle_version(self, init_data: dict):
        """Tests that the _handle_houdini_version method reports the version correctly"""
        # GIVEN
        VERSION_CALLBACK_INDEX = 3
        adaptor = HoudiniAdaptor(init_data)
        regex_callbacks = adaptor._get_regex_callbacks()
        complete_regex = regex_callbacks[VERSION_CALLBACK_INDEX].regex_list[0]

        # WHEN
        match = complete_regex.search("HoudiniClient: Houdini Version 19.5.435")
        assert match is not None
        adaptor._handle_houdini_version(match)

        # THEN
        assert adaptor._houdini_version == "19.5.435"

    @pytest.mark.parametrize("adaptor_exc_info", [RuntimeError("Something Bad Happened!")])
    def test_has_adaptor_exception(
        self, init_data: dict, adaptor_exc_info: Exception | None
    ) -> None:
        """
        Validates that the adaptor._has_exception property raises when adaptor._exc_info is not None
        and returns false when adaptor._exc_info is None
        """
        adaptor = HoudiniAdaptor(init_data)
        adaptor._exc_info = adaptor_exc_info

        with pytest.raises(RuntimeError) as exc_info:
            assert adaptor._has_exception

        assert exc_info.value == adaptor_exc_info

    @pytest.mark.parametrize("adaptor_exc_info", [None])
    def test_has_adaptor_no_exception(
        self, init_data: dict, adaptor_exc_info: Exception | None
    ) -> None:
        """
        Validates that the adaptor._has_exception property raises when adaptor._exc_info is not None
        and returns false when adaptor._exc_info is None
        """
        adaptor = HoudiniAdaptor(init_data)
        adaptor._exc_info = adaptor_exc_info

        assert not adaptor._has_exception

    @patch.object(
        HoudiniAdaptor, "_houdini_is_running", new_callable=PropertyMock(return_value=False)
    )
    def test_raises_if_houdini_not_running(
        self,
        init_data: dict,
        run_data: dict,
    ) -> None:
        """Tests that on_run raises a HoudiniNotRunningError if houdini is not running"""
        # GIVEN
        adaptor = HoudiniAdaptor(init_data)

        # WHEN
        with pytest.raises(HoudiniNotRunningError) as raised_err:
            adaptor.on_run(run_data)

        # THEN
        assert raised_err.match("Cannot render because Houdini is not running.")


class TestHoudiniAdaptor_on_cancel:
    """Tests for HoudiniAdaptor.on_cancel"""

    def test_terminates_houdini_client(self, init_data: dict, caplog: pytest.LogCaptureFixture):
        # GIVEN
        caplog.set_level(0)
        adaptor = HoudiniAdaptor(init_data)
        adaptor._houdini_client = mock_client = Mock()

        # WHEN
        adaptor.on_cancel()

        # THEN
        mock_client.terminate.assert_called_once_with(grace_time_s=0)
        assert "CANCEL REQUESTED" in caplog.text

    def test_does_nothing_if_houdini_not_running(
        self, init_data: dict, caplog: pytest.LogCaptureFixture
    ):
        """Tests that nothing happens if a cancel is requested when houdini is not running"""
        # GIVEN
        caplog.set_level(0)
        adaptor = HoudiniAdaptor(init_data)
        adaptor._houdini_client = None

        # WHEN
        adaptor.on_cancel()

        # THEN
        assert "CANCEL REQUESTED" in caplog.text
        assert "Nothing to cancel because Houdini is not running" in caplog.text
