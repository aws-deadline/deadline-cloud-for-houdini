# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from functools import wraps
from typing import Callable

from openjobio_adaptor_runtime import AdaptorDataValidators as AdaptorDataValidators
from openjobio_adaptor_runtime_client import Action
from openjobio_adaptor_runtime import (
    Adaptor,
    AdaptorConfiguration,
    LoggingSubprocess,
    RegexCallback,
    RegexHandler,
)
from openjobio_adaptor_runtime.adaptors.adaptor_ipc import ActionsQueue, AdaptorServer

_logger = logging.getLogger(__name__)


class HoudiniNotRunningError(Exception):
    """Error that is raised when attempting to use Houdini while it is not running"""

    pass


@dataclass(frozen=True)
class ActionItem:
    name: str
    # requires_path_mapping: bool = False


_FIRST_HOUDINI_ACTIONS = [
    ActionItem("scene_file"),
    ActionItem("render_node"),
    ActionItem("ignore_input_nodes"),
]
_HOUDINI_RUN_KEYS = {
    ActionItem("frame"),
}

# Only capture the major minor group (ie. 3.3)
# patch version (ie .3) is a optional non-capturing subgroup.
_MAJOR_MINOR_RE = re.compile(r"^(\d+\.\d+)(\.\d+)?$")


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

    # Variables used for keeping track of produced outputs for progress reporting.
    # Will be optionally changed after the scene is set.
    _expected_outputs: int = 0  # Total number of renders to perform.
    _produced_outputs: int = 0  # Counter for tracking number of complete renders.

    @staticmethod
    def _get_timer(timeout: int | float) -> Callable[[], bool]:
        """
        Given a timeout length, returns a lambda which returns True until the timeout occurs.

        Args:
            timeout (int): The amount of time (in seconds) to wait before timing out.
        """
        timeout_time = time.time() + timeout
        return lambda: time.time() < timeout_time

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
        is_not_timed_out = self._get_timer(self._SERVER_START_TIMEOUT_SECONDS)
        while (self._server is None or self._server.socket_path is None) and is_not_timed_out():
            time.sleep(0.01)

        if self._server is not None and self._server.socket_path is not None:
            return self._server.socket_path

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
        Sets the environment variable "HOUDINI_ADAPTOR_SOCKET_PATH" to
        the socket the server is running
        on after the server has finished starting.
        """
        self._server_thread = threading.Thread(
            target=self._start_houdini_server, name="HoudiniAdaptorServerThread"
        )
        self._server_thread.start()
        os.environ["HOUDINI_ADAPTOR_SOCKET_PATH"] = self._wait_for_socket()

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
                dir_, "deadline_adaptor_for_houdini", "HoudiniClient", "houdini_client.py"
            )
            if os.path.isfile(path):
                return path
        raise FileNotFoundError(
            "Could not find houdini_client.py. Check that the "
            "HoudiniClient package is in one of the "
            f"following directories: {sys.path[1:]}"
        )

    def _start_houdini_client(self, houdini_version: str) -> None:
        """
        Starts the houdini client by launching Houdini with the houdini_client.py file.

        Args:
            houdini_version (str): The version of Houdini that we are launching.

        Raises:
            FileNotFoundError: If the houdini_client.py file or the scene file could not be found.
        """
        exe_path = self.config.get_executable_path(platform.system(), houdini_version)
        houdini_client_path = self._get_houdini_client_path()
        regexhandler = RegexHandler(self._get_regex_callbacks())

        self._houdini_client = LoggingSubprocess(
            args=[exe_path, houdini_client_path],
            stdout_handler=regexhandler,
            stderr_handler=regexhandler,
        )

    @staticmethod
    def _get_major_minor_version(houdini_version: str) -> str:
        """Grab the major minor information from the Houdini version string.

        We may receive the whole version (ie. 3.3.4) or just the major minor
        version (ie. 3.3) from init_data. This function should handle both cases.

        Args:
            houdini_version (str): The houdini version passed with the init_data object

        Returns:
            str: The MAJOR.MINOR version of Houdini
        """
        major_minor = houdini_version
        match = _MAJOR_MINOR_RE.match(houdini_version)
        if match:
            major_minor = match.group(1)
            _logger.info(f"Using {major_minor} to find Houdini executable")
        else:
            _logger.warning(
                f"Could not find major.minor information from '{houdini_version}', "
                f"using '{houdini_version}' to find the Houdini executable"
            )

        return major_minor

    def _action_from_action_item(self, item: ActionItem, data: dict) -> Action:
        """
        Return an Action object from an ActionItem object. Applies pathmapping if necessary.

        Args:
            item (ActionItem): The ActionItem to convert to an Action
            data (dict): The data dict to provide with the action

        Returns:
            Action: An Action object
        """
        value = data.get(item.name, "")
        return Action(item.name, {item.name: value})

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

        version = str(self.init_data.get("version"))
        version = self._get_major_minor_version(version)
        self._start_houdini_client(version)

        is_not_timed_out = self._get_timer(self._HOUDINI_START_TIMEOUT_SECONDS)
        while (
            self._houdini_is_running
            and not self._has_exception
            and len(self._action_queue) > 0
            and is_not_timed_out()
        ):
            time.sleep(0.1)  # busy wait for houdini to finish initialization

        if len(self._action_queue) > 0:
            if is_not_timed_out():
                raise RuntimeError(
                    "Houdini encountered an error and was not "
                    "able to complete initialization actions."
                )
            else:
                raise TimeoutError(
                    "Houdini did not complete initialization actions in "
                    f"{self._HOUDINI_START_TIMEOUT_SECONDS} seconds and failed to start."
                )
        for action_item in _FIRST_HOUDINI_ACTIONS:
            self._action_queue.enqueue_action(
                self._action_from_action_item(
                    action_item, {action_item.name: self.init_data[action_item.name]}
                )
            )

    def on_run(self, run_data: dict) -> None:
        """
        This starts a render in Houdini for the given frame, scene and layer(s) and
        performs a busy wait until the render completes.
        """

        if not self._houdini_is_running:
            raise HoudiniNotRunningError("Cannot render because Houdini is not running.")

        self.validators.run_data.validate(run_data)
        self._produced_outputs = 0
        self._expected_outputs = 1
        self._is_rendering = True

        for action_item in _HOUDINI_RUN_KEYS:
            if action_item.name in run_data:
                self._action_queue.enqueue_action(
                    self._action_from_action_item(
                        action_item, {action_item.name: run_data[action_item.name]}
                    )
                )

        self._action_queue.enqueue_action(Action("start_render", {"frame": run_data["frame"]}))

        while self._houdini_is_rendering and not self._has_exception:
            time.sleep(0.1)  # busy wait so that on_cleanup is not called

        if not self._houdini_is_running and self._houdini_client:  # Client will always exist here.
            #  This is always an error case because the Houdini Client should still be running and
            #  waiting for the next command. If the thread finished, then we cannot continue
            exit_code = self._houdini_client.returncode
            raise HoudiniNotRunningError(
                "Houdini exited early and did not render successfully, please check render logs. "
                f"Exit code {exit_code}"
            )

    def on_end(self) -> None:
        """ """
        self._action_queue.enqueue_action(Action("close"), front=True)
        return

    def on_cleanup(self):
        """
        Cleans up the adaptor by closing the Houdini client and adaptor server.
        """
        self._performing_cleanup = True

        self._action_queue.enqueue_action(Action("close"), front=True)
        is_not_timed_out = self._get_timer(self._HOUDINI_END_TIMEOUT_SECONDS)
        while self._houdini_is_running and is_not_timed_out():
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
