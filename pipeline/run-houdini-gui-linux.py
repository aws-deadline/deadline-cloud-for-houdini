# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Own the X11 desktop, SSM tunnel and licensed, mock-only Linux test session."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

import psutil

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "test/houdini_submitter_ui"))
from _linux_process import LinuxProcessGroup, launch, owned_children  # noqa: E402


def wait_for_listener(process, port, timeout=45):
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("Owned license tunnel did not open its listener.")


@contextlib.contextmanager
def license_proxy(artifacts, environment):
    # Houdini otherwise starts a detached hserver that holds seats after exit.
    # Refuse an existing proxy and own the foreground process instead.
    if any(process.info["name"] == "hserver" for process in psutil.process_iter(["name"])):
        raise RuntimeError("An existing hserver is running; cannot own the license proxy.")
    executable = Path(environment["HOUDINI_EXECUTABLE"]).with_name("hserver")
    # An unlinked file also stays private if the runner is killed abruptly.
    with tempfile.TemporaryFile(mode="w+") as log:
        process = launch(
            [str(executable), "--run-in-foreground"],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        group = LinuxProcessGroup(process)
        try:
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                configured = subprocess.run(
                    [str(executable), "-S", environment["HOUDINI_GUI_LICENSE_SERVER"]],
                    env=environment,
                    capture_output=True,
                    timeout=5,
                )
                if configured.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("Could not set this owned hserver's loopback route.")
            yield
        finally:
            group.close()
            (artifacts / "hserver-cleanup.txt").write_text("Owned foreground hserver stopped.\n")


@contextlib.contextmanager
def license_tunnel(artifacts):
    port_number = os.environ["LICENSE_PORT"]
    document = os.environ["LICENSE_SESSION_DOCUMENT"]
    if not port_number.isdigit() or not 1 <= int(port_number) <= 65535:
        raise RuntimeError("LICENSE_PORT must be a valid port number.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,128}", document):
        raise RuntimeError("LICENSE_SESSION_DOCUMENT must be a valid document name.")
    environment = dict(os.environ, AWS_PAGER="")
    result = {}
    with tempfile.TemporaryDirectory(prefix="houdini-license-") as directory:
        root = Path(directory)
        config = root / "config"
        config.write_text(
            "[profile license]\n"
            f"role_arn = {os.environ['LICENSE_ROLE_ARN']}\n"
            "credential_source = Environment\n"
            f"region = {os.environ['AWS_REGION']}\n"
        )
        environment["AWS_CONFIG_FILE"] = str(config)

        def aws(*arguments):
            return subprocess.run(
                ["aws", *arguments, "--profile", "license"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

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
            raise RuntimeError("License bastion discovery failed; private output withheld.")
        targets = json.loads(lookup.stdout)
        if len(targets) != 1 or not re.fullmatch(r"i-[0-9a-f]{8,17}", targets[0]):
            raise RuntimeError("Expected exactly one running license bastion.")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log = root / "private-session.log"
        with log.open("w") as output:
            process = launch(
                [
                    "aws",
                    "ssm",
                    "start-session",
                    "--target",
                    targets[0],
                    "--document-name",
                    document,
                    "--parameters",
                    json.dumps({"portNumber": [port_number], "localPortNumber": [str(port)]}),
                    "--profile",
                    "license",
                ],
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            group = LinuxProcessGroup(process)
            try:
                wait_for_listener(process, port)
                result["listener_verified"] = True
                yield f"http://127.0.0.1:{port}"
            finally:
                text = log.read_text(errors="replace")
                session = re.search(r"SessionId:\s*([A-Za-z0-9._-]+)", text)
                try:
                    if session:
                        result["remote_session_terminated"] = (
                            aws("ssm", "terminate-session", "--session-id", session[1]).returncode
                            == 0
                        )
                finally:
                    group.close()
                    result["owned_process_stopped"] = process.poll() is not None
                    (artifacts / "license-cleanup.json").write_text(json.dumps(result, indent=2))
                if not result.get("remote_session_terminated"):
                    raise RuntimeError("Could not verify remote SSM session cleanup.")


@contextlib.contextmanager
def desktop(artifacts, environment, graphics):
    display = ":99"
    if Path("/tmp/.X99-lock").exists():
        raise RuntimeError("The requested X11 display is already owned.")
    config = artifacts / "xorg.conf"
    if graphics == "gpu":
        subprocess.run(
            [
                "nvidia-xconfig",
                "--output-xconfig",
                str(config),
                "--allow-empty-initial-configuration",
                "--virtual=1920x1080",
            ],
            check=True,
            capture_output=True,
        )
        command = [
            "sudo",
            "Xorg",
            display,
            "-config",
            str(config),
            "-logfile",
            str(artifacts / "xorg-server.log"),
            "-noreset",
            "-nolisten",
            "tcp",
        ]
    else:
        environment["LIBGL_ALWAYS_SOFTWARE"] = "1"
        command = ["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"]
    with (artifacts / "xorg.log").open("w") as log:
        process = launch(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        group = LinuxProcessGroup(process)
        window_manager = None
        manager_group = None
        try:
            environment.update(
                DISPLAY=display,
                QT_QPA_PLATFORM="xcb",
                QT_ACCESSIBILITY="1",
                QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1",
            )
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                check = subprocess.run(
                    ["xdpyinfo"],
                    env=environment,
                    capture_output=True,
                    timeout=5,
                )
                if check.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Owned X11 desktop failed to start; inspect xorg.log.")
            with (artifacts / "glxinfo.txt").open("w") as output:
                subprocess.run(["glxinfo", "-B"], env=environment, stdout=output, check=True)
            with (artifacts / "vulkaninfo.txt").open("w") as output:
                subprocess.run(
                    ["vulkaninfo", "--summary"],
                    env=environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            window_manager = launch(
                ["openbox"],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            manager_group = LinuxProcessGroup(window_manager)
            yield
        finally:
            if manager_group:
                manager_group.close()
            # Xorg runs as root, so terminate only the group created above.
            subprocess.run(["sudo", "kill", "-TERM", "--", f"-{group.pgid}"], check=False)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(["sudo", "kill", "-KILL", "--", f"-{group.pgid}"], check=False)
                process.wait(timeout=10)
            (artifacts / "desktop-cleanup.txt").write_text(
                "Owned Xorg and window manager stopped.\n"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--graphics", choices=("gpu", "software"), default="gpu")
    parser.add_argument(
        "--graphics-only",
        action="store_true",
        help="Validate the owned desktop without licensing or launching Houdini.",
    )
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Runner received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "source-sha.txt").write_text(
        os.environ.get("HOUDINI_GUI_TESTED_SHA", os.environ["GITHUB_SHA"]) + "\n"
    )
    (artifacts / "harness-sha.txt").write_text(os.environ["GITHUB_SHA"] + "\n")
    # Keep actual licensing credentials solely in this orchestrator.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AWS_", "ACTIONS_ID_TOKEN_"))
        and key
        not in (
            "LICENSE_ROLE_ARN",
            "LICENSE_PORT",
            "LICENSE_SESSION_DOCUMENT",
            "BASTION_INSTANCE_TAG",
            "INSTALLER_BUCKET",
            "INSTALLER_BUCKET_EXPECTED_OWNER",
        )
    }
    if args.graphics_only:
        with desktop(artifacts, environment, args.graphics):
            subprocess.run(["xdpyinfo"], env=environment, capture_output=True, check=True)
        return 0
    environment.update(
        HOUDINI_GUI_REPO=str(REPO),
        HOUDINI_GUI_PYTHON_VERSION="3.13",
        HOUDINI_GUI_GRAPHICS_MODE="nvidia" if args.graphics == "gpu" else "software",
        PYTHONPATH=str(REPO / "test/houdini_submitter_ui/_python"),
        HOUDINI_PATH=f"{REPO / 'src/deadline/houdini_submitter'}:&",
        HOUDINI_USER_PREF_DIR=str(artifacts / "baseline-prefs-__HVER__"),
        HOUDINI_NO_ENV_FILE="1",
        PATH=f"{Path(sys.executable).parent}:{Path(os.environ['HOUDINI_EXECUTABLE']).parent}:"
        + os.environ.get("PATH", ""),
    )
    with license_tunnel(artifacts) as server:
        environment.update(SESI_LMHOST=server, HOUDINI_GUI_LICENSE_SERVER=server)
        with desktop(artifacts, environment, args.graphics), license_proxy(artifacts, environment):
            with (artifacts / "pytest.log").open("w") as log:
                process = launch(
                    [
                        sys.executable,
                        str(REPO / "pipeline/run-houdini-gui-baseline.py"),
                        "--artifacts",
                        str(artifacts),
                    ],
                    env=environment,
                    cwd=REPO,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                group = LinuxProcessGroup(process)
                try:
                    returncode = process.wait(timeout=2400)
                finally:
                    group.close()
                    (artifacts / "baseline-cleanup.txt").write_text(
                        "Owned baseline group stopped.\n"
                    )
                (artifacts / "exit.txt").write_text(f"baseline_exit={returncode}\n")
                print((artifacts / "pytest.log").read_text())
                return returncode


if __name__ == "__main__":
    with owned_children():
        raise SystemExit(main())
