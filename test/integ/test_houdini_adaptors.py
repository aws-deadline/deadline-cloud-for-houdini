# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import pytest

from pathlib import Path

from .helpers.test_runners import run_command, run_houdini_adaptor_test
from .helpers.image_comparison import assert_all_images_close
import os
import psutil

from deadline.houdini_adaptor.HoudiniAdaptor import HoudiniAdaptor


@pytest.mark.adaptor
class TestAdaptors:
    """
    Tests that the Houdini adaptor produces the output we expect given different job bundles and parameters.
    """

    def test_minimal_scene_adaptor(
        self,
        hython_location: Path,
        script_location: Path,
        tmp_path: Path,
    ) -> None:

        # Use the test script to create a HIP file that we can use with the pre-made job bundle.
        run_command(
            [
                str(hython_location),
                str(script_location / "minimal_test" / "_test_hip.py"),
                "--",
                str(script_location),
                str(tmp_path),
                "adaptor",
            ]
        )

        job_params = {"HipFile": f"{str(tmp_path)}/test.hip"}
        job_template_location = (
            script_location / "minimal_test" / "expected_job_bundle" / "template.yaml"
        )
        run_houdini_adaptor_test(job_template_location, job_params)

        assert_all_images_close(
            script_location / "minimal_test" / "expected_images", tmp_path / "render"
        )

    def test_wedge_node_adaptor(
        self, hython_location: Path, script_location: Path, tmp_path: Path
    ) -> None:

        run_command(
            [
                str(hython_location),
                str(script_location / "wedge_node_test" / "_test_hip.py"),
                "--",
                str(script_location),
                str(tmp_path),
                "adaptor",
            ]
        )

        job_params = {"HipFile": f"{str(tmp_path)}/test_wedge.hip"}
        job_template_location = (
            script_location / "wedge_node_test" / "expected_job_bundle" / "template.yaml"
        )
        run_houdini_adaptor_test(job_template_location, job_params)
        assert_all_images_close(
            script_location / "wedge_node_test" / "expected_images", tmp_path / "render"
        )

    def test_render_dependencies_adaptor(
        self, hython_location: Path, script_location: Path, tmp_path: Path
    ) -> None:
        scene_name = "test_render_deps.hip"

        run_command(
            [
                str(hython_location),
                str(script_location / "render_dependencies_test" / "_test_hip.py"),
                "--",
                str(script_location),
                str(tmp_path),
                scene_name,
                "adaptor",
            ]
        )
        job_params = {"HipFile": f"{str(tmp_path / scene_name)}"}
        job_template_location = (
            script_location / "render_dependencies_test" / "expected_job_bundle" / "template.yaml"
        )
        run_houdini_adaptor_test(job_template_location, job_params)
        assert_all_images_close(
            script_location / "render_dependencies_test" / "expected_images", tmp_path
        )

    def test_adaptor_open_close(
        self, hython_location: Path, script_location: Path, tmp_path: Path
    ) -> None:
        """Test that the adaptor can successfully open and close the Houdini client."""
        run_command(
            [
                str(hython_location),
                str(script_location / "minimal_test" / "_test_hip.py"),
                "--",
                str(script_location),
                str(tmp_path),
                "adaptor",
            ]
        )

        adaptor = HoudiniAdaptor(
            {
                "scene_file": f"{str(tmp_path)}/test.hip",
                "render_node": "/out/mantra1",
                "version": os.environ["HOUDINI_VERSION"],
            }
        )

        adaptor.on_start()
        assert adaptor._houdini_is_running

        # Validate process is actually running using PID
        assert adaptor._houdini_client
        pid = adaptor._houdini_client.pid
        assert psutil.pid_exists(pid)

        adaptor.on_cleanup()
        assert not adaptor._houdini_is_running
        assert not psutil.pid_exists(pid)

    def test_adaptor_cancel_closes_houdini(
        self, hython_location: Path, script_location: Path, tmp_path: Path
    ) -> None:
        """Test that on_cancel closes the Houdini client."""
        run_command(
            [
                str(hython_location),
                str(script_location / "minimal_test" / "_test_hip.py"),
                "--",
                str(script_location),
                str(tmp_path),
                "adaptor",
            ]
        )

        adaptor = HoudiniAdaptor(
            {
                "scene_file": f"{str(tmp_path)}/test.hip",
                "render_node": "/out/mantra1",
                "version": os.environ["HOUDINI_VERSION"],
            }
        )

        adaptor.on_start()
        assert adaptor._houdini_is_running

        assert adaptor._houdini_client
        pid = adaptor._houdini_client.pid
        assert psutil.pid_exists(pid)

        adaptor.on_cancel()
        assert not adaptor._houdini_is_running
        assert not psutil.pid_exists(pid)

    def test_adaptor_daemon_open_close(
        self, hython_location: Path, script_location: Path, tmp_path: Path
    ) -> None:
        """Test that the adaptor can open and close the Houdini client in daemon mode."""
        import tempfile
        import subprocess
        import json
        import time

        run_command(
            [
                str(hython_location),
                str(script_location / "minimal_test" / "_test_hip.py"),
                "--",
                str(script_location),
                str(tmp_path),
                "adaptor",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            connection_file = Path(tmpdir) / "connection.json"

            # Start daemon backend
            init_data = {
                "scene_file": f"{str(tmp_path)}/test.hip",
                "render_node": "/out/mantra1",
                "version": os.environ["HOUDINI_VERSION"],
            }

            try:
                start_process = subprocess.run(
                    [
                        "houdini-openjd",
                        "daemon",
                        "start",
                        "--connection-file",
                        str(connection_file),
                        "--init-data",
                        json.dumps(init_data),
                    ]
                )

                assert start_process.returncode == 0

                # Wait for connection file
                timeout = time.time() + 10
                while not connection_file.exists() and time.time() < timeout:
                    time.sleep(0.1)
                assert connection_file.exists()

                # Connect and verify process is running
                connection_data = json.loads(connection_file.read_text())
                assert "socket" in connection_data, "Connection file should contain socket"

            finally:
                # Stop daemon
                if connection_file.exists():
                    stop_cmd = [
                        "houdini-openjd",
                        "daemon",
                        "stop",
                        "--connection-file",
                        str(connection_file),
                    ]

                    stop_process = subprocess.run(
                        stop_cmd,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )

                    # Verify daemon stopped successfully
                    assert (
                        stop_process.returncode == 0
                    ), f"Daemon stop failed: {stop_process.stderr}"
