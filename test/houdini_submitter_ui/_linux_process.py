# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Own an isolated Linux session and stop every descendant at teardown."""

import os
import sys
import signal
import subprocess
import time
import contextlib
import ctypes

import psutil


if sys.platform == "linux":

    @contextlib.contextmanager
    def owned_children():
        """Adopt detached grandchildren so a crashing test cannot leak a GUI seat."""
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "Could not enable owned child supervision.")
        try:
            yield
        finally:
            children = psutil.Process().children(recursive=True)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(children, timeout=5)
            for child in alive:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(alive, timeout=5)
            if alive:
                raise RuntimeError("Owned child processes survived cleanup.")

    class LinuxProcessGroup:
        def __init__(self, process):
            self.process = process
            self.pgid = os.getpgid(process.pid)
            if self.pgid != process.pid or self.pgid == os.getpgrp():
                raise RuntimeError("Test children must start in their own session.")

        def close(self):
            try:
                os.killpg(self.pgid, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self.process.poll()
                try:
                    os.killpg(self.pgid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.1)
            try:
                os.killpg(self.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=10)

    def launch(command, **kwargs):
        return subprocess.Popen(command, start_new_session=True, **kwargs)
