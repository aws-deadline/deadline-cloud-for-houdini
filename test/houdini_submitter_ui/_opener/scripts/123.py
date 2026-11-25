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
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.IsUserAnAdmin.argtypes = []
        shell32.IsUserAnAdmin.restype = ctypes.c_int
        startup["administrator_token"] = bool(shell32.IsUserAnAdmin())
        startup["vulkan_environment"] = {
            name: os.environ.get(name)
            for name in ("VK_DRIVER_FILES", "VK_ICD_FILENAMES", "VK_LOADER_DEBUG", "DZN_DEBUG")
        }
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel32.GetModuleFileNameW.restype = ctypes.c_uint
        graphics_modules = {}
        for name in (
            "opengl32.dll",
            "libgallium_wgl.dll",
            "vulkan-1.dll",
            "vulkan_lvp.dll",
            "vulkan_dzn.dll",
            "nvoglv64.dll",
        ):
            module = kernel32.GetModuleHandleW(name)
            if module:
                filename = ctypes.create_unicode_buffer(32768)
                if not kernel32.GetModuleFileNameW(module, filename, len(filename)):
                    raise ctypes.WinError(ctypes.get_last_error())
                graphics_modules[name] = filename.value
        startup["graphics_modules"] = graphics_modules
        if startup["graphics_mode"] == "mesa-software":
            bin_dir = Path(os.environ["HOUDINI_EXECUTABLE"]).parent
            if not all(
                name in graphics_modules and Path(graphics_modules[name]).parent == bin_dir
                for name in ("opengl32.dll", "libgallium_wgl.dll")
            ):
                raise RuntimeError(
                    "The actual Houdini GUI did not load the application-local Mesa DLLs."
                )
            icd_dir = Path(os.environ["HOUDINI_GUI_VULKAN_ICD"]).parent
            driver = os.environ["HOUDINI_GUI_VULKAN_DRIVER"]
            if driver not in graphics_modules or Path(graphics_modules[driver]).parent != icd_dir:
                raise RuntimeError(
                    "The actual Houdini GUI did not load the isolated Vulkan CPU driver."
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
        prompts = []
        original_display_message = hou.ui.displayMessage
        prompt_log = status.with_name("native-prompts.json")

        def record_prompts():
            temporary = prompt_log.with_suffix(".tmp")
            temporary.write_text(json.dumps(prompts, indent=2), encoding="utf-8")
            temporary.replace(prompt_log)

        def observed_display_message(text, *args, **kwargs):
            prompt = {"text": text, "title": kwargs.get("title"), "buttons": kwargs.get("buttons")}
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
    except Exception:
        startup["error"] = traceback.format_exc()
        status.write_text(json.dumps(startup, indent=2), encoding="utf-8")
        raise


if not hou.isUIAvailable():
    raise RuntimeError("GUI integration tests must launch houdini.exe, not hython.")
QTimer.singleShot(15000, open_submitter)
