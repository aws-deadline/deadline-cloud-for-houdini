# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Real Houdini GUI startup hook for the isolated mock-backed test session."""

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
        scene = runpy.run_path(str(repo / "test/integ/test_scripts/minimal_test/_test_hip.py"))
        rop = hou.node("/out").createNode("deadline_cloud")
        render = scene["_create_scene_and_rop"](output.as_posix())
        rop.setFirstInput(render)
        scene["_set_parameters"](rop)
        hou.hipFile.save(str(output / "test.hip"))
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
