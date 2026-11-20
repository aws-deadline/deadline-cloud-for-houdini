# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""A stand-in for hython that the tests can start on any OS, with no Houdini."""

from __future__ import annotations

import shlex
import stat
import sys
from pathlib import Path


def hython_stand_in(folder: Path, body: str) -> Path:
    """Write ``body`` to ``hython.py`` in ``folder`` and return a launcher that runs it with
    this Python: ``hython.cmd`` on Windows, and a ``hython`` shell script elsewhere.
    """
    (folder / "hython.py").write_text(body, encoding="utf-8")
    # A launcher, not a #! line, because the kernel splits #! at the first space in the
    # Python's path, and Windows ignores #! lines.
    if sys.platform == "win32":
        launcher: Path = folder / "hython.cmd"
        launcher.write_bytes(f'@"{sys.executable}" "%~dp0hython.py" %*\r\n'.encode())
        return launcher
    launcher = folder / "hython"
    launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$0.py" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher
