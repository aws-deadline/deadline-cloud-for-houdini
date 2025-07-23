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

    def test_minimal_scene_submitter(
        self,
        hython_location: Path,
        script_location: Path,
        tmp_path: Path,
    ) -> None:
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

        with (
            open(
                script_location / "minimal_test" / "expected_job_bundle" / "template.yaml"
            ) as expected,
            open(job_history_dir / "template.yaml") as actual,
        ):

            # Inject the scene location and Houdini version.
            # These are the only variables that can change depending on the user's test environment.
            expected_template = yaml.safe_load(expected)
            expected_template["parameterDefinitions"][0]["default"] = scene_location_posix

            init_data_file = expected_template["steps"][0]["stepEnvironments"][0]["script"][
                "embeddedFiles"
            ][0]
            init_data_file["data"] = init_data_file["data"].replace(
                "<HOUDINI_VERSION>", os.environ["HOUDINI_VERSION"]
            )

            assert expected_template == yaml.safe_load(actual)

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

        with open(job_history_dir / "parameter_values.yaml") as actual:
            actual_params = yaml.safe_load(actual)
            assert len(actual_params["parameterValues"]) == len(expected_params["parameterValues"])
            for param_value in expected_params["parameterValues"]:
                assert param_value in actual_params["parameterValues"]

        # Check that the asset references are as expected
        expected_asset_references: dict[str, dict[str, Any]] = {
            "assetReferences": {
                "inputs": {"directories": [], "filenames": {scene_location_posix}},
                "outputs": {
                    "directories": [str(output_path)],
                },
                "referencedPaths": [],
            }
        }

        with open(job_history_dir / "asset_references.yaml") as actual:
            actual_asset_references = yaml.safe_load(actual)
            assert len(actual_asset_references["assetReferences"]["inputs"]["filenames"]) == len(
                expected_asset_references["assetReferences"]["inputs"]["filenames"]
            )
            actual_asset_references["assetReferences"]["inputs"]["filenames"] = set(
                actual_asset_references["assetReferences"]["inputs"]["filenames"]
            )
            assert actual_asset_references == expected_asset_references
