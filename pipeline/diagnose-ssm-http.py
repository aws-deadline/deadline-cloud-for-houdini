# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Diagnose the CI role/document forwarding path without DCC installation or checkout."""

import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import time


def main() -> int:
    artifacts = Path(os.environ["RUNNER_TEMP"]) / "houdini-license-transport"
    artifacts.mkdir(exist_ok=True)
    result = {"document": "DccInteg-PortForwardToLicenseServer", "remote_port": 1715}
    environment = dict(os.environ, AWS_PAGER="")

    def aws(*arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["aws", *arguments, "--profile", "license"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    with tempfile.TemporaryDirectory(prefix="houdini-license-transport-") as directory:
        root = Path(directory)
        config = root / "config"
        config.write_text(
            "[profile license]\n"
            f"role_arn = {os.environ['LICENSE_ROLE_ARN']}\n"
            "credential_source = Environment\n"
            f"region = {os.environ['AWS_REGION']}\n",
            encoding="utf-8",
        )
        environment["AWS_CONFIG_FILE"] = str(config)
        process = None
        log = root / "session-private.log"
        try:
            lookup = aws(
                "ec2",
                "describe-instances",
                "--filters",
                f"Name=tag:Name,Values={os.environ['BASTION_INSTANCE_TAG']}",
                "Name=instance-state-name,Values=running",
                "--query",
                "Reservations[].Instances[].InstanceId",
                "--output",
                "json",
            )
            if lookup.returncode:
                raise RuntimeError("Bastion lookup failed")
            targets = json.loads(lookup.stdout)
            if len(targets) != 1 or not re.fullmatch(r"i-[0-9a-f]{8,17}", targets[0]):
                raise RuntimeError("Expected exactly one running bastion")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            result["local_port"] = port
            with log.open("w", encoding="utf-8") as output:
                process = subprocess.Popen(
                    [
                        "aws",
                        "ssm",
                        "start-session",
                        "--target",
                        targets[0],
                        "--document-name",
                        result["document"],
                        "--parameters",
                        json.dumps({"portNumber": ["1715"], "localPortNumber": [str(port)]}),
                        "--profile",
                        "license",
                    ],
                    env=environment,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 45
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            result["local_listener"] = True
                        break
                    except OSError:
                        time.sleep(0.25)
                result["attempts"] = []
                if result.get("local_listener"):
                    for number in range(1, 4):
                        attempt = {"number": number}
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                        try:
                            connection.request("GET", "/")
                            response = connection.getresponse()
                            body = response.read(4096).lower()
                            attempt.update(
                                http_status=response.status,
                                sidefx_signature=any(
                                    word in body for word in (b"sidefx", b"sesinetd", b"houdini")
                                ),
                            )
                        except (OSError, http.client.HTTPException) as exc:
                            attempt["http_error"] = type(exc).__name__
                        finally:
                            connection.close()
                        result["attempts"].append(attempt)
                        if number < 3:
                            time.sleep(2)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            # Exception text may contain private connection data.
            result["diagnostic_error"] = type(exc).__name__
        finally:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            result["transport_error_counts"] = {
                signature: len(re.findall(re.escape(signature), text, re.IGNORECASE))
                for signature in (
                    "Connection to destination port failed",
                    "Connection refused",
                    "AccessDenied",
                    "TargetNotConnected",
                    "InvalidDocument",
                    "Waiting for connections",
                    "Connection accepted",
                )
            }
            session = re.search(r"SessionId:\s*([A-Za-z0-9._-]+)", text)
            if session:
                try:
                    result["remote_session_terminated"] = (
                        aws("ssm", "terminate-session", "--session-id", session.group(1)).returncode
                        == 0
                    )
                except subprocess.TimeoutExpired:
                    result["remote_session_terminated"] = False
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
            result["owned_process_stopped"] = process is None or process.poll() is not None
    (artifacts / "ssm-http-diagnostic.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result))
    return 0 if result.get("remote_session_terminated") and result["owned_process_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
