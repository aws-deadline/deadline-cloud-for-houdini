# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Real Houdini GUI startup hook for the isolated mock-backed test session."""

import ctypes
import json
import os
import runpy
import site
import sys
import traceback
from pathlib import Path
from urllib.parse import urlparse

import hou

# Keep Houdini's own Qt/PySide runtime. Process .pth files for external pywin32.
site.addsitedir(os.environ["HOUDINI_GUI_DEPENDENCIES"])
sys.path.insert(0, os.environ["HOUDINI_GUI_REPO"] + "/src")
sys.path.insert(0, os.environ["HOUDINI_GUI_REPO"] + "/src/deadline/houdini_submitter/python")

from PySide6.QtCore import QTimer, qVersion  # noqa: E402


def open_submitter():
    status = Path(os.environ["HOUDINI_GUI_STATUS"])
    startup = {}
    try:
        if hou.qt.mainWindow() is None:
            QTimer.singleShot(1000, open_submitter)
            return
        viewer = hou.ui.curDesktop().paneTabOfType(hou.paneTabType.SceneViewer)
        startup = {
            "houdini_version": hou.applicationVersionString(),
            "python_version": sys.version,
            "qt_version": qVersion(),
            "license_category": str(hou.licenseCategory()),
            "ui_available": hou.isUIAvailable(),
            "main_window_visible": hou.qt.mainWindow().isVisible(),
            "scene_viewer": viewer is not None,
            "viewport": viewer.curViewport().name() if viewer else None,
        }
        # Record graphics from the actual licensed GUI, not a separate probe.
        startup["graphics_mode"] = os.environ.get("HOUDINI_GUI_GRAPHICS_MODE", "native")
        if sys.platform == "win32":
            shell32 = getattr(ctypes, "WinDLL")("shell32", use_last_error=True)
            shell32.IsUserAnAdmin.argtypes = []
            shell32.IsUserAnAdmin.restype = ctypes.c_int
            startup["administrator_token"] = bool(shell32.IsUserAnAdmin())
            kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
            kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            kernel32.GetModuleFileNameW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_uint,
            ]
            kernel32.GetModuleFileNameW.restype = ctypes.c_uint
            graphics_modules = {}
            for name in (
                "opengl32.dll",
                "vulkan-1.dll",
                "nvoglv64.dll",
            ):
                module = kernel32.GetModuleHandleW(name)
                if module:
                    filename = ctypes.create_unicode_buffer(32768)
                    if not kernel32.GetModuleFileNameW(module, filename, len(filename)):
                        raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
                    graphics_modules[name] = filename.value
            startup["graphics_modules"] = graphics_modules
        else:
            startup["graphics_modules"] = sorted(
                {
                    line.split()[-1]
                    for line in Path("/proc/self/maps").read_text().splitlines()
                    if any(name in line for name in ("libGL", "libnvidia", "libvulkan"))
                }
            )
        # The mock uses root-level operation paths. Match the existing Nuke
        # test-local endpoint adaptation; do not alter installed client code.
        import botocore.awsrequest

        original_urljoin = botocore.awsrequest._urljoin
        botocore.awsrequest._urljoin = lambda endpoint, url_path, host_prefix: original_urljoin(
            endpoint, url_path, None
        )
        from botocore.client import BaseClient

        original_api_call = BaseClient._make_api_call
        mock = os.environ["HOUDINI_GUI_MOCK_ENDPOINT"]
        assert urlparse(mock).hostname in ("127.0.0.1", "localhost")

        def mock_only_call(client, operation, parameters):
            if client.meta.endpoint_url.rstrip("/") != mock.rstrip("/"):
                raise RuntimeError(f"GUI test refused non-mock API operation {operation}")
            return original_api_call(client, operation, parameters)

        BaseClient._make_api_call = mock_only_call
        repo = Path(os.environ["HOUDINI_GUI_REPO"])
        output = Path(os.environ["HOUDINI_GUI_OUTPUT"])
        output.mkdir(parents=True, exist_ok=True)
        # Houdini's native message bodies/buttons are not exposed through UIA.
        # Observe the real prompt and response without changing its behavior.
        prompts: list[dict] = []
        original_display_message = hou.ui.displayMessage
        prompt_log = status.with_name("native-prompts.json")

        def record_prompts():
            temporary = prompt_log.with_suffix(".tmp")
            temporary.write_text(json.dumps(prompts, indent=2), encoding="utf-8")
            temporary.replace(prompt_log)

        def observed_display_message(text, *args, **kwargs):
            from PySide6.QtWidgets import QApplication

            modal = QApplication.activeModalWidget()
            prompt = {
                "text": text,
                "title": kwargs.get("title"),
                "buttons": kwargs.get("buttons"),
                "active_qt_modal_title": modal.windowTitle() if modal else None,
            }
            prompts.append(prompt)
            record_prompts()
            choice = original_display_message(text, *args, **kwargs)
            prompt["choice"] = choice
            record_prompts()
            return choice

        hou.ui.displayMessage = observed_display_message
        scene = runpy.run_path(str(repo / "test/integ/test_scripts/minimal_test/_test_hip.py"))
        rop = hou.node("/out").createNode("deadline_cloud")
        render = scene["_create_scene_and_rop"](output.as_posix())
        rop.setFirstInput(render)
        scene["_set_parameters"](rop)
        hou.hipFile.save(str(output / "test.hip"))
        scenario = os.environ["HOUDINI_GUI_SCENARIO"]
        if scenario != "export":
            from deadline_cloud_for_houdini._assets import _parse_files

            _parse_files(rop)
            # Start with automatic saving enabled on the node. The unsaved
            # case disables it in the dialog to exercise the current-UI gate.
            rop.parm("auto_save_hip").set(1)
            if scenario == "missing-input":
                rop.setInput(0, None)
            hou.hipFile.save()
        # Capture the native viewport in this actual licensed GUI session.
        # This establishes viewport rendering, unlike an independent GL probe.
        viewport = viewer.curViewport()
        viewport.frameAll()
        settings = viewer.flipbookSettings().stash()
        settings.frameRange((1, 1))
        settings.outputToMPlay(False)
        settings.useResolution(True)
        settings.resolution((256, 256))
        settings.output(str(output / "viewport.$F4.png").replace("\\", "/"))
        viewer.flipbook(viewport, settings)
        startup["viewport_capture"] = str(output / "viewport.0001.png")
        if scenario == "unsaved":
            hou.node("/obj").createNode("null", "unsaved_gui_test")
            assert hou.hipFile.hasUnsavedChanges()
        startup["scenario"] = scenario
        startup["unsaved_changes"] = hou.hipFile.hasUnsavedChanges()
        status.write_text(
            json.dumps(startup, indent=2),
            encoding="utf-8",
        )
        from deadline_cloud_for_houdini.submitter import submit_callback

        submit_callback({"node": rop})
        if scenario in ("lifecycle", "login"):
            from deadline_cloud_for_houdini.submitter import _ROP_SUBMITTER_DIALOGS

            dialog = _ROP_SUBMITTER_DIALOGS[rop.sessionId()]
            request = status.with_name("gui-action.json")
            result_file = status.with_name("gui-action-result.json")

            def run_test_action():
                if not request.is_file():
                    return
                action = json.loads(request.read_text())["action"]
                request.unlink()
                result: dict = {}
                try:
                    if scenario == "lifecycle" and action == "reuse":
                        rop.setName("renamed_deadline_gui_test")
                        submit_callback({"node": rop})
                        submit_callback({"node": rop})
                        result = {
                            "same_dialog": _ROP_SUBMITTER_DIALOGS[rop.sessionId()] is dialog,
                            "dialog_count": len(_ROP_SUBMITTER_DIALOGS),
                        }
                    elif scenario == "lifecycle" and action == "delete":
                        node_id = rop.sessionId()
                        rop.destroy()
                        result = {"dialog_released": node_id not in _ROP_SUBMITTER_DIALOGS}
                    elif scenario == "login" and action == "login":
                        import threading
                        import time
                        from unittest.mock import patch

                        from deadline.client.ui.dialogs.deadline_login_dialog import (
                            DeadlineLoginDialog,
                        )

                        original_refresh = dialog.refresh_deadline_settings
                        authentication = dialog.deadline_authentication_status
                        original_auth_refresh = authentication.refresh_status
                        worker = None
                        result = {"settings_refresh": 0, "auth_refresh": 0}

                        def refresh_settings():
                            result["settings_refresh"] += 1
                            original_refresh()

                        def refresh_authentication():
                            result["auth_refresh"] += 1
                            original_auth_refresh()

                        def fake_login_start(login_dialog):
                            nonlocal worker

                            def finish_login():
                                deadline = time.monotonic() + 30
                                completed = status.with_name("complete-mock-login")
                                while not completed.is_file():
                                    if time.monotonic() >= deadline:
                                        login_dialog.login_thread_exception.emit(
                                            RuntimeError("Mock login completion timed out.")
                                        )
                                        return
                                    time.sleep(0.05)
                                login_dialog.login_thread_succeeded.emit("Mock login completed.")

                            worker = threading.Thread(target=finish_login)
                            worker.start()

                        # Keep the actual client dialog and Qt signals. Replace only
                        # authentication so this test never opens a real AWS login.
                        with (
                            patch.object(DeadlineLoginDialog, "_start_login", fake_login_start),
                            patch.object(dialog, "refresh_deadline_settings", refresh_settings),
                            patch.object(authentication, "refresh_status", refresh_authentication),
                        ):
                            dialog.on_login()
                            result["login_returned"] = True
                            if worker is not None:
                                worker.join(timeout=5)
                                result["worker_finished"] = not worker.is_alive()
                    else:
                        raise RuntimeError("Unexpected GUI regression test action.")
                except Exception:
                    result["error"] = traceback.format_exc()
                temporary = result_file.with_suffix(".tmp")
                temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
                temporary.replace(result_file)

            timer = QTimer(hou.qt.mainWindow())
            timer.timeout.connect(run_test_action)
            timer.start(100)
            dialog._deadline_test_action_timer = timer
    except Exception:
        startup["error"] = traceback.format_exc()
        status.write_text(json.dumps(startup, indent=2), encoding="utf-8")
        raise


if not hou.isUIAvailable():
    raise RuntimeError("GUI integration tests must launch the Houdini GUI, not hython.")
QTimer.singleShot(15000, open_submitter)
