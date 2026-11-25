# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Process-local dependency bootstrap for GUI-exported local render tests."""

import os
import site
import sys
from pathlib import Path
from urllib.parse import urlparse

# The runner's Python and Houdini's embedded Python have different ABIs.
if sys.version_info[:2] == tuple(
    map(int, os.environ.get("HOUDINI_GUI_PYTHON_VERSION", "0.0").split("."))
):
    site.addsitedir(os.environ["HOUDINI_GUI_DEPENDENCIES"])
repo = Path(os.environ["HOUDINI_GUI_REPO"])
sys.path[:0] = [str(repo / "src"), str(repo / "src/deadline/houdini_submitter/python")]

if os.environ.get("HOUDINI_GUI_MOCK_ENDPOINT"):
    import botocore.awsrequest
    from botocore.client import BaseClient

    mock = os.environ["HOUDINI_GUI_MOCK_ENDPOINT"]
    if urlparse(mock).hostname not in ("127.0.0.1", "localhost"):
        os._exit(2)
    original_urljoin = botocore.awsrequest._urljoin
    botocore.awsrequest._urljoin = lambda endpoint, url_path, host_prefix: original_urljoin(
        endpoint, url_path, None
    )
    original_api_call = BaseClient._make_api_call

    def mock_only_call(client, operation, parameters):
        if client.meta.endpoint_url.rstrip("/") != mock.rstrip("/"):
            raise RuntimeError(f"Local test refused non-mock API operation {operation}")
        return original_api_call(client, operation, parameters)

    BaseClient._make_api_call = mock_only_call
