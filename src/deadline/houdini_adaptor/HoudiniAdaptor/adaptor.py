# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import threading
import time
from functools import wraps
from typing import Callable

from deadline.client.api import get_deadline_cloud_library_telemetry_client, TelemetryClient
from openjd.adaptor_runtime._version import version as openjd_adaptor_version
from openjd.adaptor_runtime.adaptors import Adaptor, AdaptorDataValidators, SemanticVersion
from openjd.adaptor_runtime.adaptors.configuration import AdaptorConfiguration
from openjd.adaptor_runtime.process import LoggingSubprocess
from openjd.adaptor_runtime.app_handlers import RegexCallback, RegexHandler
from openjd.adaptor_runtime.application_ipc import ActionsQueue, AdaptorServer
from openjd.adaptor_runtime_client import Action

from .._version import version as adaptor_version

_logger = logging.getLogger(__name__)


class HoudiniNotRunningError(Exception):
    """Error that is raised when attempting to use Houdini while it is not running"""

    pass


_REQUIRED_HOUDINI_INIT_KEYS = [
    "scene_file",
    "render_node",
]
_OPTIONAL_HOUDINI_INIT_KEYS = {
    "ignore_input_nodes",
    "wedgenum",
    "wedge_node",
}

_HOUDINI_RUN_KEYS = {
    "frame",
}


def _check_for_exception(func: Callable) -> Callable:
    """
    Decorator that checks if an exception has been caught before calling the
    decorated function
    """

    @wraps(func)
    def wrapped_func(self, *args, **kwargs):
        if not self._has_exception:  # Raises if there is an exception  # pragma: no branch
            return func(self, *args, **kwargs)

    return wrapped_func


class HoudiniAdaptor(Adaptor[AdaptorConfiguration]):
    """
    Adaptor that creates a session in Houdini to Render interactively.
    """

    _SERVER_START_TIMEOUT_SECONDS = 30
    _SERVER_END_TIMEOUT_SECONDS = 30
    _HOUDINI_START_TIMEOUT_SECONDS = 300
    _HOUDINI_END_TIMEOUT_SECONDS = 30

    _server: AdaptorServer | None = None
    _server_thread: threading.Thread | None = None
    _houdini_client: LoggingSubprocess | None = None
    _action_queue = ActionsQueue()
    _is_rendering: bool = False
    # If a thread raises an exception we will update this to raise in the main thread
    _exc_info: Exception | None = None
    _performing_cleanup = False
    _regex_callbacks: list | None = None
    _validators: AdaptorDataValidators | None = None
    _telemetry_client: TelemetryClient | None = None
    _houdini_version: str = ""

    # Variables used for keeping track of produced outputs for progress reporting.
    # Will be optionally changed after the scene is set.
    _expected_outputs: int = 1  # Total number of renders to perform.
    _produced_outputs: int = 0  # Counter for tracking number of complete renders.

    @property
    def integration_data_interface_version(self) -> SemanticVersion:
        return SemanticVersion(major=0, minor=1)

    @staticmethod
    def _get_timer(timeout: int | float) -> Callable[[], bool]:
        """
        Given a timeout length, returns a lambda which returns False until the timeout occurs.

        Args:
            timeout (int): The amount of time (in seconds) to wait before timing out.
        """
        timeout_time = time.time() + timeout
        return lambda: time.time() >= timeout_time

    @property
    def _has_exception(self) -> bool:
        """Property which checks the private _exc_info property for an exception

        Raises:
            self._exc_info: An exception if there is one

        Returns:
            bool: False there is no exception waiting to be raised
        """
        if self._exc_info and not self._performing_cleanup:
            raise self._exc_info
        return False

    @property
    def _houdini_is_running(self) -> bool:
        """Property which indicates that the houdini client is running

        Returns:
            bool: True if the houdini client is running, false otherwise
        """
        return self._houdini_client is not None and self._houdini_client.is_running

    @property
    def _houdini_is_rendering(self) -> bool:
        """Property which indicates if houdini is rendering

        Returns:
            bool: True if houdini is rendering, false otherwise
        """
        return self._houdini_is_running and self._is_rendering

    @_houdini_is_rendering.setter
    def _houdini_is_rendering(self, value: bool) -> None:
        """Property setter which updates the private _is_rendering boolean.

        Args:
            value (bool): A boolean indicating if houdini is rendering.
        """
        self._is_rendering = value

    def _wait_for_socket(self) -> str:
        """
        Performs a busy wait for the socket path that the adaptor server is running on, then
        returns it.

        Raises:
            RuntimeError: If the server does not finish initializing

        Returns:
            str: The socket path the adaptor server is running on.
        """
        is_timed_out = self._get_timer(self._SERVER_START_TIMEOUT_SECONDS)
        while (self._server is None or self._server.server_path is None) and not is_timed_out():
            time.sleep(0.01)

        if self._server is not None and self._server.server_path is not None:
            return self._server.server_path

        raise RuntimeError("Could not find a socket because the server did not finish initializing")

    def _start_houdini_server(self) -> None:
        """
        Starts a server with the given ActionsQueue, attaches the server to the adaptor and serves
        forever in a blocking call.
        """
        self._server = AdaptorServer(self._action_queue, self)
        self._server.serve_forever()

    def _start_houdini_server_thread(self) -> None:
        """
        Starts the houdini adaptor server in a thread.
        Sets the environment variable "HOUDINI_ADAPTOR_SERVER_PATH" to
        the socket the server is running
        on after the server has finished starting.
        """
        self._server_thread = threading.Thread(
            target=self._start_houdini_server, name="HoudiniAdaptorServerThread"
        )
        self._server_thread.start()
        os.environ["HOUDINI_ADAPTOR_SERVER_PATH"] = self._wait_for_socket()

    @property
    def validators(self) -> AdaptorDataValidators:
        if not self._validators:
            cur_dir = os.path.dirname(__file__)
            schema_dir = os.path.join(cur_dir, "schemas")
            self._validators = AdaptorDataValidators.for_adaptor(schema_dir)
        return self._validators

    def _get_regex_callbacks(self) -> list[RegexCallback]:
        """
        Returns a list of RegexCallbacks used by the Houdini Adaptor

        Returns:
            list[RegexCallback]: List of Regex Callbacks to add
        """
        if not self._regex_callbacks:
            callback_list = []

            _houdini_license_error = "RuntimeError: Error encountered when initializing Houdini"

            completed_regexes = [re.compile(".*Finished Rendering.*")]
            progress_regexes = [re.compile(".*ALF_PROGRESS ([0-9]+)%.*")]
            error_regexes = [re.compile(".*Error: .*|.*\\[Error\\].*", re.IGNORECASE)]
            version_regexes = [
                re.compile("HoudiniClient: Houdini Version ([0-9]+.[0-9]+)(.[0-9]+)?")
            ]

            callback_list.append(RegexCallback(completed_regexes, self._handle_complete))
            callback_list.append(RegexCallback(progress_regexes, self._handle_progress))
            if self.init_data.get("strict_error_checking", False):
                callback_list.append(RegexCallback(error_regexes, self._handle_error))

            callback_list.append(
                RegexCallback(
                    [re.compile(_houdini_license_error)],
                    self._handle_license_error,
                )
            )
            callback_list.append(RegexCallback(version_regexes, self._handle_houdini_version))

            self._regex_callbacks = callback_list
        return self._regex_callbacks

    def _handle_logging(self, match: re.Match) -> None:
        print(match.group(0))

    @_check_for_exception
    def _handle_complete(self, match: re.Match) -> None:
        """
        Callback for stdout that indicate completeness of a render. Updates progress to 100
        Args:
            match (re.Match): The match object from the regex pattern that was matched in the
                              message.
        """
        self._houdini_is_rendering = False
        self.update_status(progress=100)

    @_check_for_exception
    def _handle_progress(self, match: re.Match) -> None:
        """
        Callback for stdout that indicate progress of a render.
        Args:
            match (re.Match): The match object from the regex pattern that was matched in the
                              message.
        """
        text = match.group(0)
        loc = text.index("ALF_PROGRESS ") + len("ALF_PROGRESS ")
        percent = text[loc : loc + 2]
        # check for % in case of single digit progress
        percent = percent[0] if percent.endswith("%") else percent
        progress = int(percent)
        self.update_status(progress=progress)

    def _handle_error(self, match: re.Match) -> None:
        """
        Callback for stdout that indicates an error or warning.
        Args:
            match (re.Match): The match object from the regex pattern that was matched in the
                              message

        Raises:
            RuntimeError: Always raises a runtime error to halt the adaptor.
        """
        self._exc_info = RuntimeError(f"Houdini Encountered an Error: {match.group(0)}")

    def _handle_license_error(self, match: re.Match) -> None:
        """
        Callback for stdout that indicates an license error.
        Args:
            match (re.Match): The match object from the regex pattern that was matched the message

        Raises:
            RuntimeError: Always raises a runtime error to halt the adaptor.
        """
        shutil_usage = shutil.disk_usage(os.getcwd())
        self._exc_info = RuntimeError(
            f"{match.group(0)}\n"
            "This error is typically associated with a licensing error"
            " when using Houdini. Check your licensing configuration.\n"
            f"Free disc space: {shutil_usage.free//1024//1024}M\n"
        )

    def _handle_houdini_version(self, match: re.Match) -> None:
        """
        Callback for stdout that indicates the Houdini version in use.
        Args:
            match (re.Match): The match object from the regex pattern that was matched the message
        """
        self._houdini_version = match.groups()[0]
        if len(match.groups()) > 1:
            self._houdini_version += match.groups()[1]

    def _get_houdini_client_path(self) -> str:
        """
        Obtains the houdini_client.py path by searching directories in sys.path

        Raises:
            FileNotFoundError: If the houdini_client.py file could not be found.

        Returns:
            str: The path to the houdini_client.py file.
        """
        for dir_ in sys.path:
            path = os.path.join(
                dir_, "deadline", "houdini_adaptor", "HoudiniClient", "houdini_client.py"
            )
            if os.path.isfile(path):
                return path
        raise FileNotFoundError(
            "Could not find houdini_client.py. Check that the "
            "HoudiniClient package is in one of the "
            f"following directories: {sys.path[1:]}"
        )

    def _start_houdini_client(self) -> None:
        """
        Starts the houdini client by launching Houdini with the houdini_client.py file.

        Raises:
            FileNotFoundError: If the houdini_client.py file or the scene file could not be found.
        """
        hython_exe = "hython"
        regexhandler = RegexHandler(self._get_regex_callbacks())

        # Add the openjd namespace directory to PYTHONPATH, so that adaptor_runtime_client
        # will be available directly to the houdini client.
        import openjd.adaptor_runtime_client
        import deadline.houdini_adaptor

        openjd_namespace_dir = os.path.dirname(
            os.path.dirname(openjd.adaptor_runtime_client.__file__)
        )
        deadline_namespace_dir = os.path.dirname(os.path.dirname(deadline.houdini_adaptor.__file__))
        python_path_addition = f"{openjd_namespace_dir}{os.pathsep}{deadline_namespace_dir}"
        if "PYTHONPATH" in os.environ:
            os.environ["PYTHONPATH"] = (
                f"{os.environ['PYTHONPATH']}{os.pathsep}{python_path_addition}"
            )
        else:
            os.environ["PYTHONPATH"] = python_path_addition

        # If there are path mapping rules, set the houdini environment variable to enable them
        houdini_pathmap = self._get_houdini_pathmap()
        if houdini_pathmap:
            os.environ["HOUDINI_PATHMAP"] = houdini_pathmap

        _logger.info("Setting HOUDINI_PATHMAP to: {}".format(houdini_pathmap))

        houdini_client_path = self._get_houdini_client_path()

        self._houdini_client = LoggingSubprocess(
            args=[hython_exe, houdini_client_path],
            stdout_handler=regexhandler,
            stderr_handler=regexhandler,
        )

    def _get_houdini_pathmap(self) -> str:
        """Builds a dict of source to destination strings from the path mapping rules

        The string representation of the dict can then be used to set HOUDINI_PATHMAP

        Returns:
            str: The value to set HOUDINI_PATHMAP to
        """
        path_mapping_rules: dict[str, str] = {}

        for rule in self._path_mapping_rules:
            path_mapping_rules[rule.source_path.replace("\\", "/")] = rule.destination_path.replace(
                "\\", "/"
            )

        if path_mapping_rules:
            return str(path_mapping_rules)
        return ""

    def on_start(self) -> None:
        """
        For job stickiness. Will start everything required for the Task. Will be used for all
        SubTasks.

        Raises:
            jsonschema.ValidationError: When init_data fails validation against the adaptor schema.
            jsonschema.SchemaError: When the adaptor schema itself is nonvalid.
            RuntimeError: If Houdini did not complete initialization actions due to an exception
            TimeoutError: If Houdini did not complete initialization actions due to timing out.
            FileNotFoundError: If the houdini_client.py file could not be found.
            KeyError: If a configuration for the given platform and version does not exist.
        """
        self.validators.init_data.validate(self.init_data)

        self.update_status(progress=0, status_message="Initializing Houdini")
        self._start_houdini_server_thread()
        self._populate_action_queue()

        self._start_houdini_client()

        is_timed_out = self._get_timer(self._HOUDINI_START_TIMEOUT_SECONDS)
        while self._houdini_is_running and not self._has_exception and len(self._action_queue) > 0:
            if is_timed_out():
                raise TimeoutError(
                    "Houdini did not complete initialization actions in "
                    f"{self._HOUDINI_START_TIMEOUT_SECONDS} seconds and failed to start."
                )

            time.sleep(0.1)  # busy wait for houdini to finish initialization

        self._get_deadline_telemetry_client().record_event(
            event_type="com.amazon.rum.deadline.adaptor.runtime.start", event_details={}
        )

        if len(self._action_queue) > 0:
            raise RuntimeError(
                "Houdini encountered an error and was not able to complete initialization actions."
            )

    def on_run(self, run_data: dict) -> None:
        """
        This starts a render in Houdini for the given frame, scene and layer(s) and
        performs a busy wait until the render completes.
        """

        if not self._houdini_is_running:
            raise HoudiniNotRunningError("Cannot render because Houdini is not running.")

        # frame argument is string type and will fail validation unless we
        # recast to int
        # TODO: morgan - Fix this bug
        run_data["frame"] = int(run_data["frame"])
        self.validators.run_data.validate(run_data)
        # ERROR: Entrypoint failed:
        # ERROR: openjd_fail: Error encountered while running adaptor: '1' is not of type 'number'
        #
        # Failed validating 'type' in schema['properties']['frame']:
        #     {'type': 'number'}
        #
        # On instance['frame']:
        #     '1'
        self._is_rendering = True

        for name in _HOUDINI_RUN_KEYS:
            if name in run_data:
                self._action_queue.enqueue_action(Action(name, {name: run_data[name]}))

        self._action_queue.enqueue_action(Action("start_render", {"frame": run_data["frame"]}))

        while self._houdini_is_rendering and not self._has_exception:
            time.sleep(0.1)  # busy wait so that on_cleanup is not called

        if not self._houdini_is_running and self._houdini_client:  # Client will always exist here.
            #  This is always an error case because the Houdini Client should still be running and
            #  waiting for the next command. If the thread finished, then we cannot continue
            exit_code = self._houdini_client.returncode
            self._get_deadline_telemetry_client().record_error(
                {"exit_code": exit_code, "exception_scope": "on_run"}, str(RuntimeError)
            )
            raise HoudiniNotRunningError(
                "Houdini exited early and did not render successfully, please check render logs. "
                f"Exit code {exit_code}"
            )

    def on_stop(self) -> None:
        """ """
        self._action_queue.enqueue_action(Action("close"), front=True)
        return

    def on_cleanup(self):
        """
        Cleans up the adaptor by closing the Houdini client and adaptor server.
        """
        self._performing_cleanup = True

        self._action_queue.enqueue_action(Action("close"), front=True)
        is_timed_out = self._get_timer(self._HOUDINI_END_TIMEOUT_SECONDS)
        while self._houdini_is_running and not is_timed_out():
            time.sleep(0.1)
        if self._houdini_is_running and self._houdini_client:
            _logger.error(
                "Houdini did not complete cleanup actions and failed to gracefully shutdown. "
                "Terminating."
            )
            self._houdini_client.terminate()

        if self._server:
            self._server.shutdown()

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=self._SERVER_END_TIMEOUT_SECONDS)
            if self._server_thread.is_alive():
                _logger.error("Failed to shutdown the Houdini Adaptor server.")

        self._performing_cleanup = False

    def on_cancel(self):
        """
        Cancels the current render if Houdini is rendering.
        """
        _logger.info("CANCEL REQUESTED")
        if not self._houdini_client or not self._houdini_is_running:
            _logger.info("Nothing to cancel because Houdini is not running")
            return

        self._houdini_client.terminate(grace_time_s=0)

    def _populate_action_queue(self) -> None:
        """
        Populates the adaptor server's action queue with actions from the init_data that the Houdini
        Client will request and perform. The action must be present in either the
        _REQUIRED_HOUDINI_INIT_KEYS or _OPTIONAL_HOUDINI_INIT_KEYS set to be added to the action queue.
        """
        for name in _REQUIRED_HOUDINI_INIT_KEYS:
            self._action_queue.enqueue_action(Action(name, {name: self.init_data[name]}))

        for name in _OPTIONAL_HOUDINI_INIT_KEYS:
            if name in self.init_data:
                self._action_queue.enqueue_action(Action(name, {name: self.init_data[name]}))

    def _get_deadline_telemetry_client(self):
        """
        Wrapper around the Deadline Client Library telemetry client, in order to set package-specific information
        """
        if not self._telemetry_client:
            self._telemetry_client = get_deadline_cloud_library_telemetry_client()
            self._telemetry_client.update_common_details(
                {
                    "deadline-cloud-for-houdini-adaptor-version": adaptor_version,
                    "houdini-version": self._houdini_version,
                    "open-jd-adaptor-runtime-version": openjd_adaptor_version,
                }
            )
        return self._telemetry_client
