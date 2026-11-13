# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Own Houdini's Windows descendants, including workers that outlive its GUI.

A Job Object keeps ownership after the root exits; PID-based sweeps cannot
reliably do that. Close the job after the existing root-process teardown to
terminate its remaining workers. Adapted from the Nuke Windows GUI harness. This module uses only the standard library.
"""

from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes


def _last_windows_error() -> OSError:
    # These exports exist only on Windows, including in ctypes' type stubs.
    return getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsProcessJob:
    """Attach the already launched Houdini process before its slow GUI startup.

    The Popen process handle identifies our process even if its PID is reused.
    Descendants inherit this job automatically, without querying other sessions.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise _last_windows_error()
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise _last_windows_error()
            # Popen owns this handle for the lifetime of the process object.
            if not kernel32.AssignProcessToJobObject(self._handle, getattr(process, "_handle")):
                raise _last_windows_error()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release the owned job exactly once, stopping any surviving workers."""
        handle, self._handle = self._handle, None
        if handle and not self._kernel32.CloseHandle(handle):
            raise _last_windows_error()
