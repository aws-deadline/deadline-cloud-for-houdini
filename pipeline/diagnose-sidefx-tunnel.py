# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Compare HTTP transport and real SideFX protocol without checking out a license."""

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "test/houdini_submitter_ui"))
    from _windows_process import WindowsProcessJob

    port = int(os.environ["HOUDINI_GUI_LICENSE_PORT"])
    executable = Path(os.environ["HYTHON_EXECUTABLE"]).with_name("sesictrl.exe")
    result = {
        "remote_port": 1715,
        "local_port": port,
        "proxy_environment_present": {
            key: bool(os.environ.get(key))
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
        },
        "attempts": [],
    }
    for number in range(1, 4):
        attempt = {"number": number}
        # http.client goes directly to loopback, independent of proxy settings.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read(4096).lower()
            attempt.update(
                http_status=response.status,
                sidefx_signature=any(word in body for word in (b"sidefx", b"sesinetd", b"houdini")),
            )
        except (OSError, http.client.HTTPException) as exc:
            attempt["http_error"] = type(exc).__name__
        finally:
            connection.close()
        process = subprocess.Popen(
            [str(executable), "ping", "--host", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        job = None
        try:
            job = WindowsProcessJob(process)
            stdout, stderr = process.communicate(timeout=20)
            output = (stdout + stderr).lower()
            attempt.update(
                protocol_exit=process.returncode,
                protocol_success=process.returncode == 0 and "Successfully pinged" in stdout,
                protocol_error_signatures=[
                    text
                    for text in (
                        "unable to connect",
                        "could not connect",
                        "connection refused",
                        "timed out",
                        "unauthorized",
                    )
                    if text in output
                ],
            )
        except subprocess.TimeoutExpired:
            attempt.update(protocol_success=False, protocol_timeout=True)
        finally:
            if job is not None:
                job.close()
            elif process.poll() is None:
                process.kill()
            process.communicate(timeout=30)
        result["attempts"].append(attempt)
        if number < 3:
            time.sleep(5)
    directory = Path(os.environ["RUNNER_TEMP"]) / "houdini-gui-artifacts"
    directory.mkdir(exist_ok=True)
    (directory / "sidefx-transport-diagnostic.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
