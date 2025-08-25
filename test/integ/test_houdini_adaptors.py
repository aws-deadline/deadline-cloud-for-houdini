# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import pytest

from pathlib import Path

from .helpers.test_runners import run_command, run_houdini_adaptor_test
from .helpers.image_comparison import assert_all_images_close


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
