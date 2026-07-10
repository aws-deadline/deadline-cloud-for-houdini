# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
from unittest.mock import MagicMock

# we must mock UI code
mock_modules = [
    # deadline-cloud >= 0.60 eagerly imports submit_job_to_deadline_dialog from
    # deadline.client.ui.dialogs, which runs DeadlineAuthenticationStatus.getInstance() at module
    # import time. Under the mocked qtpy below that call raises, so mock the whole module out (the
    # submitter only needs the dialogs package to import). Matches the Nuke submitter's bootstrap.
    "deadline.client.ui.deadline_authentication_status",
    "PySide2",
    "PySide2.QtCore",
    "PySide2.QtGui",
    "PySide2.QtWidgets",
    "pxr",
    "pxr.Usd",
    "pxr.UsdUtils",
    "qtpy",
    "qtpy.QtCore",
    "qtpy.QtWidgets",
    "qtpy.QtGui",
]

for module in mock_modules:
    sys.modules[module] = MagicMock()
