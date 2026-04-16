# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import pytest
import yaml

from pathlib import Path
from typing import Any

from .helpers.test_runners import run_houdini_submitter_test, is_valid_template


@pytest.mark.submitter
class TestSubmitters:
    """
    Tests that the Houdini submitter generates the job bundle we expect given different scenes & parameters.
    """

    """
    Helpers
    """

    def _assert_job_template(
        self, scene_location_posix, expected_job_template_dir, job_history_dir
    ):
        with (
            open(expected_job_template_dir / "template.yaml") as expected,
            open(job_history_dir / "template.yaml") as actual,
        ):

            # Inject the scene location and Houdini version.
            # These are the only variables that can change depending on the user's test environment.
            expected_template = yaml.safe_load(expected)
            expected_template["parameterDefinitions"][0]["default"] = scene_location_posix

            for step in expected_template["steps"]:
                init_data_file = step["stepEnvironments"][0]["script"]["embeddedFiles"][0]
                init_data_file["data"] = init_data_file["data"].replace(
                    "<HOUDINI_VERSION>", os.environ["HOUDINI_VERSION"]
                )

            assert expected_template == yaml.safe_load(actual)

    def _assert_parameter_values(self, job_history_dir: Path, expected_params: dict[str, list]):
        with open(job_history_dir / "parameter_values.yaml") as actual:
            actual_params = yaml.safe_load(actual)
            assert len(actual_params["parameterValues"]) == len(expected_params["parameterValues"])
            for param_value in expected_params["parameterValues"]:
                assert param_value in actual_params["parameterValues"]

    def _assert_asset_references(
        self, job_history_dir: Path, expected_asset_references: dict[str, dict[str, Any]]
    ):
        with open(job_history_dir / "asset_references.yaml") as actual:
            actual_asset_references = yaml.safe_load(actual)
            assert len(actual_asset_references["assetReferences"]["inputs"]["filenames"]) == len(
                expected_asset_references["assetReferences"]["inputs"]["filenames"]
            )
            actual_asset_references["assetReferences"]["inputs"]["filenames"] = set(
                actual_asset_references["assetReferences"]["inputs"]["filenames"]
            )
            assert actual_asset_references == expected_asset_references

    """
    Test Cases
    """

    def test_minimal_scene_submitter(
        self, hython_location: Path, script_location: Path, tmp_path: Path
    ) -> None:
        """
        Tests a basic scene with one render node and two frames.
        This generates:
        - a job template with one step and a task parameter
        - the HIP filename as a job parameter
        - the HIP filename and output directory in asset references
        """

        job_history_dir = tmp_path / "jobhistory"
        output_path = tmp_path / "output"

        os.makedirs(job_history_dir, exist_ok=True)
        os.makedirs(output_path, exist_ok=True)

        output = run_houdini_submitter_test(
            hython_location,
            script_location / "minimal_test" / "_test_hip.py",
            str(job_history_dir),
            str(output_path),
            "submitter",
        )

        assert (
            output.returncode == 0
        ), f"Houdini submitter exited with code {output.returncode}:\n{output.stderr.decode(encoding='utf-8', errors='replace')}"

        # Check that we have a valid template
        assert is_valid_template(job_history_dir / "template.yaml")

        # Houdini will save the HIP file as an absolute path, so we have to inject it into the expected template & parameter values.
        scene_location = Path.cwd() / "test.hip"
        # Covert the path to POSIX, which is Houdini's convention on any OS
        scene_location_posix = scene_location.as_posix()

        # We need to inject submitter information into the expected job bundle before comparing
        self._assert_job_template(
            scene_location.as_posix(),
            script_location / "minimal_test" / "expected_job_bundle",
            job_history_dir,
        )

        # Check that the parameter values are as expected
        expected_params: dict[str, list] = {
            "parameterValues": [
                {"name": "HipFile", "value": scene_location_posix},
                {"name": "deadline:priority", "value": 50},
                {"name": "deadline:maxRetriesPerTask", "value": 5},
                {"name": "deadline:maxFailedTasksCount", "value": 20},
                {"name": "deadline:targetTaskRunStatus", "value": "READY"},
            ]
        }
        self._assert_parameter_values(job_history_dir, expected_params)

        # Check that the asset references are as expected
        expected_asset_references: dict[str, dict[str, Any]] = {
            "assetReferences": {
                "inputs": {"directories": [], "filenames": {scene_location_posix}},
                "outputs": {
                    "directories": [
                        str(output_path) + "/render"
                    ],  # The test scene uses a forward slash for the render directory
                },
                "referencedPaths": [],
            }
        }
        self._assert_asset_references(job_history_dir, expected_asset_references)

    def test_wedge_node_submitter(
        self, hython_location: Path, script_location: Path, tmp_path: Path
    ):
        """
        Tests a basic scene with a material wedge node and one render node.
        This generates:
        - a job template with two steps, one per wedge parameter, with two frames each
        - the HIP filename as a job parameter
        - the HIP filename and output directory as asset references
        """

        job_history_dir = tmp_path / "jobhistory"
        output_path = tmp_path / "output"

        os.makedirs(job_history_dir, exist_ok=True)
        os.makedirs(output_path, exist_ok=True)

        output = run_houdini_submitter_test(
            hython_location,
            script_location / "wedge_node_test" / "_test_hip.py",
            str(job_history_dir),
            str(output_path),
            "submitter",
        )

        assert (
            output.returncode == 0
        ), f"Houdini submitter exited with code {output.returncode}:\n{output.stderr.decode(encoding='utf-8', errors='replace')}"
        # Check that we have a valid template
        assert is_valid_template(job_history_dir / "template.yaml")

        # Houdini will save the HIP file as an absolute path, so we have to inject it into the expected template & parameter values.
        scene_location = Path.cwd() / "test_wedge.hip"
        # Covert the path to POSIX, which is Houdini's convention on any OS
        scene_location_posix = scene_location.as_posix()

        # We need to inject submitter information into the expected job bundle before comparing
        self._assert_job_template(
            scene_location.as_posix(),
            script_location / "wedge_node_test" / "expected_job_bundle",
            job_history_dir,
        )

        # Check that the parameter values are as expected
        expected_params: dict[str, list] = {
            "parameterValues": [
                {"name": "HipFile", "value": scene_location_posix},
                {"name": "deadline:priority", "value": 50},
                {"name": "deadline:maxRetriesPerTask", "value": 5},
                {"name": "deadline:maxFailedTasksCount", "value": 20},
                {"name": "deadline:targetTaskRunStatus", "value": "READY"},
            ]
        }
        self._assert_parameter_values(job_history_dir, expected_params)

        # Check that the asset references are as expected
        expected_asset_references: dict[str, dict[str, Any]] = {
            "assetReferences": {
                "inputs": {"directories": [], "filenames": {scene_location_posix}},
                "outputs": {
                    "directories": [str(output_path) + "/render"],
                },
                "referencedPaths": [],
            }
        }
        self._assert_asset_references(job_history_dir, expected_asset_references)

    def test_render_dependencies_submitter(
        self,
        hython_location: Path,
        script_location: Path,
        tmp_path: Path,
    ) -> None:
        """
        Tests a basic scene with two render nodes chained together.
        This generates:
        - a job template with two steps, one dependent on the other; each step has two tasks
        - the HIP filename as a job parameter
        - the HIP filename and output directory as asset references
        """
        scene_name = "test_render_deps.hip"

        job_history_dir = tmp_path / "jobhistory"
        output_path = tmp_path / "output"

        os.makedirs(job_history_dir, exist_ok=True)
        os.makedirs(output_path, exist_ok=True)

        output = run_houdini_submitter_test(
            hython_location,
            script_location / "render_dependencies_test" / "_test_hip.py",
            str(job_history_dir),
            str(output_path),
            scene_name,
            "submitter",
        )

        assert (
            output.returncode == 0
        ), f"Houdini submitter exited with code {output.returncode}:\n{output.stderr.decode(encoding='utf-8', errors='replace')}"
        assert is_valid_template(job_history_dir / "template.yaml")

        scene_location = Path.cwd() / scene_name
        scene_location_posix = scene_location.as_posix()

        self._assert_job_template(
            scene_location_posix,
            script_location / "render_dependencies_test" / "expected_job_bundle",
            job_history_dir,
        )

        expected_params: dict[str, list] = {
            "parameterValues": [
                {"name": "HipFile", "value": scene_location_posix},
                {"name": "deadline:priority", "value": 50},
                {"name": "deadline:maxRetriesPerTask", "value": 5},
                {"name": "deadline:maxFailedTasksCount", "value": 20},
                {"name": "deadline:targetTaskRunStatus", "value": "READY"},
            ]
        }
        self._assert_parameter_values(job_history_dir, expected_params)

        expected_asset_reference: dict[str, dict[str, Any]] = {
            "assetReferences": {
                "inputs": {
                    "directories": [],
                    "filenames": {
                        scene_location_posix,
                    },
                },
                "outputs": {
                    "directories": [str(output_path)],
                },
                "referencedPaths": [],
            }
        }
        self._assert_asset_references(job_history_dir, expected_asset_reference)

    def test_usd_scene_dependency_detection(
        self, hython_location: Path, script_location: Path, tmp_path: Path
    ) -> None:
        """
        Tests that the submitter detects all dependencies inside USD scenes,
        including sublayers, references, payloads, and texture assets.

        The test scene has this dependency graph:
            scene.usda
            ├── sublayer: lighting.usda
            │   └── sublayer: model.usda
            │       └── asset: textures/wood.exr
            └── payload: heavy_asset.usda
        """
        job_history_dir = tmp_path / "jobhistory"
        output_path = tmp_path / "output"

        os.makedirs(job_history_dir, exist_ok=True)
        os.makedirs(output_path, exist_ok=True)

        output = run_houdini_submitter_test(
            hython_location,
            script_location / "usd_scene_test" / "_test_hip.py",
            str(job_history_dir),
            str(output_path),
            "submitter",
        )

        assert (
            output.returncode == 0
        ), f"Houdini submitter exited with code {output.returncode}:\n{output.stderr.decode(encoding='utf-8', errors='replace')}"

        assert is_valid_template(job_history_dir / "template.yaml")

        scene_location_posix = (Path.cwd() / "test_usd.hip").as_posix()
        usd_dir = output_path / "usd_scene"

        with open(job_history_dir / "asset_references.yaml") as f:
            actual_refs = yaml.safe_load(f)

        actual_input_files: set[str] = {
            os.path.normcase(f) for f in actual_refs["assetReferences"]["inputs"]["filenames"]
        }

        # All USD layers and assets must be detected
        expected_files = {
            os.path.normcase(str(usd_dir / "scene.usda")),
            os.path.normcase(str(usd_dir / "lighting.usda")),
            os.path.normcase(str(usd_dir / "model.usda")),
            os.path.normcase(str(usd_dir / "heavy_asset.usda")),
            os.path.normcase(str(usd_dir / "textures" / "wood.exr")),
        }

        for expected in expected_files:
            assert (
                expected in actual_input_files
            ), f"Missing USD dependency: {expected}\nActual files: {actual_input_files}"

        # Hip file must also be present
        assert os.path.normcase(scene_location_posix) in actual_input_files

        # Output directory from RenderProduct must be detected (resolved from relative path)
        actual_output_dirs: set[str] = {
            os.path.normcase(d) for d in actual_refs["assetReferences"]["outputs"]["directories"]
        }
        expected_output_dir: str = os.path.normcase(str(usd_dir / "renders"))
        assert (
            expected_output_dir in actual_output_dirs
        ), f"Missing output directory: {expected_output_dir}\nActual dirs: {actual_output_dirs}"
