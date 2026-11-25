# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Launch a test child with the runner's identity and reduced privileges.

The Vulkan loader intentionally ignores app-local driver environment variables
in elevated processes. CreateRestrictedToken and medium integrity let this
test's GUI use those variables without changing users, UAC, or the registry.
The child starts suspended so its Job Object owns it before it can spawn workers.
"""

from __future__ import annotations

import os
import subprocess


def allow_test_directory(directory):
    """Grant this same user's medium token access only to its test scratch tree."""
    from pathlib import Path

    import win32api
    import win32con
    import win32security

    directory = Path(directory).resolve()
    root = Path(os.environ["RUNNER_TEMP"]).resolve()
    if directory == root or not directory.is_relative_to(root):
        raise RuntimeError("Only an isolated directory under RUNNER_TEMP may be prepared.")
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.ConvertSidToStringSid(
            win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        )
    finally:
        token.Close()
    # Administrator-only inherited ACLs cannot authorize a deny-only admin SID.
    # Add Modify for the SAME user, never Users/Everyone, and label only this
    # disposable test tree medium. No system directories or profiles are edited.
    for options in (
        ["/grant", f"*{sid}:(OI)(CI)M", "/T"],
        ["/setintegritylevel", "(OI)(CI)M", "/T"],
    ):
        subprocess.run(
            ["icacls", str(directory), *options], capture_output=True, text=True, check=True
        )


class UnelevatedWindowsProcess:
    """The small Popen interface needed by the GUI fixture and its Job Object."""

    def __init__(self, args, *, cwd, env, stdout, stderr):
        import msvcrt

        import win32api
        import win32con
        import win32process
        import win32security

        self.args = args
        self.returncode = None
        self._handle = None
        self._thread = None
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32con.TOKEN_QUERY
            | win32con.TOKEN_DUPLICATE
            | win32con.TOKEN_ASSIGN_PRIMARY
            | win32con.TOKEN_ADJUST_DEFAULT,
        )
        restricted = None
        inherited = []
        try:
            # Administrator membership becomes deny-only. Do not use
            # SANDBOX_INERT: all existing software restriction policies apply.
            admin = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid)
            restricted = win32security.CreateRestrictedToken(token, 1, [(admin, 0)], None, None)
            win32security.SetTokenInformation(
                restricted,
                win32security.TokenIntegrityLevel,
                (win32security.ConvertStringSidToSid("S-1-16-8192"), 0x20),
            )
            startup = win32process.STARTUPINFO()
            startup.lpDesktop = None  # Inherit this job's already verified desktop.
            startup.dwFlags = win32con.STARTF_USESTDHANDLES
            with open(os.devnull, "rb") as stdin:
                for stream in (stdin, stdout, stderr):
                    inherited.append(
                        win32api.DuplicateHandle(
                            win32api.GetCurrentProcess(),
                            getattr(msvcrt, "get_osfhandle")(stream.fileno()),
                            win32api.GetCurrentProcess(),
                            0,
                            True,
                            win32con.DUPLICATE_SAME_ACCESS,
                        )
                    )
                startup.hStdInput, startup.hStdOutput, startup.hStdError = inherited
                self._handle, self._thread, self.pid, _ = win32process.CreateProcessAsUser(
                    restricted,
                    str(args[0]),
                    subprocess.list2cmdline([str(arg) for arg in args]),
                    None,
                    None,
                    True,
                    win32con.CREATE_SUSPENDED | win32con.CREATE_UNICODE_ENVIRONMENT,
                    dict(env),
                    str(cwd),
                    startup,
                )
        finally:
            for handle in inherited:
                handle.Close()
            if restricted is not None:
                restricted.Close()
            token.Close()

    def resume(self):
        import win32process

        assert self._thread is not None
        win32process.ResumeThread(self._thread)
        self._thread.Close()
        self._thread = None

    def poll(self):
        import win32event
        import win32process

        if self.returncode is None and win32event.WaitForSingleObject(self._handle, 0) == 0:
            self.returncode = win32process.GetExitCodeProcess(self._handle)
        return self.returncode

    def kill(self):
        import win32api

        win32api.TerminateProcess(self._handle, 1)

    def wait(self, timeout=None):
        import win32event

        assert self._handle is not None
        milliseconds = win32event.INFINITE if timeout is None else int(timeout * 1000)
        if win32event.WaitForSingleObject(self._handle, milliseconds) == win32event.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        result = self.poll()
        if self._thread is not None:
            self._thread.Close()
            self._thread = None
        self._handle.Close()
        self._handle = None
        return result


if __name__ == "__main__":
    import sys
    from pathlib import Path

    from _windows_process import WindowsProcessJob

    output = Path(os.environ["RUNNER_TEMP"]) / "houdini-gui-artifacts/launcher-check.log"
    scratch = output.parent / "launcher-work"
    scratch.mkdir()
    allow_test_directory(scratch)
    # This verifies only child launch/privileges. GUI compatibility is proved
    # later by the actual Houdini viewport and submitter integration tests.
    command = (
        "import ctypes,json,os,pathlib; "
        "admin=bool(ctypes.windll.shell32.IsUserAnAdmin()); "
        "print(json.dumps({'administrator_token':admin})); "
        "assert not admin, 'Child must not retain administrator membership'; "
        "pathlib.Path(os.environ['LAUNCHER_WRITE_CHECK']).write_text('medium token write passed')"
    )
    probe_environment = dict(os.environ, LAUNCHER_WRITE_CHECK=str(scratch / "write-check.txt"))
    with output.open("w", encoding="utf-8") as log:
        child = UnelevatedWindowsProcess(
            [sys.executable, "-c", command],
            cwd=scratch,
            env=probe_environment,
            stdout=log,
            stderr=log,
        )
        job = None
        try:
            job = WindowsProcessJob(child)
            child.resume()
            exit_code = child.wait(timeout=30)
        finally:
            if job is not None:
                job.close()
                if child._handle is not None:
                    child.wait(timeout=30)
            elif child.poll() is None:
                child.kill()
                child.wait(timeout=30)
    print(output.read_text())
    sys.exit(exit_code)
