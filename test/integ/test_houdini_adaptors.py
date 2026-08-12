# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import os
import re

import pytest

from pathlib import Path

from .helpers.test_runners import run_command, run_houdini_adaptor_test
from .helpers.image_comparison import assert_all_images_close


def _get_houdini_version(hython_location: Path) -> str:
    """Derive the Houdini version from HOUDINI_VERSION env var or the hython path."""
    houdini_version = os.environ.get("HOUDINI_VERSION", "")
    if not houdini_version:
        match = re.search(r"[Hh]oudini[/\\]?(\d+\.\d+)", str(hython_location))
        if match:
            houdini_version = match.group(1)
    if not houdini_version:
        pytest.fail(
            "Could not determine Houdini version. Set HOUDINI_VERSION env var "
            f"or ensure hython path contains version info. Path: {hython_location}"
        )
    return houdini_version


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

    def test_adaptor_keeps_dcc_open_between_tasks(
        self,
        hython_location: Path,
        script_location: Path,
        tmp_path: Path,
    ) -> None:
        """
        Verifies that the Houdini adaptor keeps the DCC open between tasks within a single
        step session. The adaptor should call 'daemon start' once, 'daemon run' for each task,
        and 'daemon stop' once — proving Houdini was NOT restarted between tasks.

        Uses a Geometry ROP to avoid requiring a render license (e.g. mantra).
        """
        # Determine Houdini version from the hython path or env
        houdini_version = _get_houdini_version(hython_location)

        # Create a HIP file with a Geometry ROP (no render license needed)
        hip_output = run_command(
            [
                str(hython_location),
                str(script_location / "dcc_open_test" / "_test_hip.py"),
                "--",
                str(tmp_path),
            ]
        )
        assert hip_output.returncode == 0, "Failed to create test HIP file"

        job_template_location = script_location / "dcc_open_test" / "template.yaml"
        job_params = {
            "HipFile": str(tmp_path / "test_dcc_open.hip"),
            "HoudiniVersion": houdini_version,
        }

        # Run the step with all tasks in a single session (frames 1-3)
        output = run_command(
            [
                "openjd",
                "run",
                str(job_template_location),
                "--step",
                "GeoRender",
                "--job-param",
                json.dumps(job_params),
            ]
        )
        assert output.returncode == 0

        # Combine stdout and stderr for lifecycle assertion
        combined_output = output.stdout.decode("utf-8", errors="replace") + output.stderr.decode(
            "utf-8", errors="replace"
        )

        # Verify the hython process was launched exactly once. The adaptor logs
        # "Running command: <hython_path>" when it starts hython. If the adaptor
        # restarted Houdini between tasks, we'd see this repeated.
        hython_launches = combined_output.count("ADAPTOR_OUTPUT: INFO: Running command:")
        assert hython_launches == 1, (
            f"Expected hython to be launched exactly once but found {hython_launches} "
            "launches. Houdini was restarted between tasks."
        )

        # Verify all 3 tasks rendered successfully within the single session
        assert combined_output.count("Finished Rendering") == 3, (
            "Expected 3 'Finished Rendering' messages (one per task) but got "
            f"{combined_output.count('Finished Rendering')}."
        )

        # Verify the session structure: one enter, one exit
        assert combined_output.count("Entering Environment") == 1
        assert combined_output.count("Exiting Environment") == 1

    def test_lop_keyframe_pathmap_does_not_crash(
        self,
        hython_location: Path,
        script_location: Path,
        tmp_path: Path,
    ) -> None:
        """
        Guards PR #355 regression: parm.unexpandedString() raised
        hou.OperationFailed on keyframed LOP parms inside _remap_lop_file_paths,
        crashing set_scene_file on real customer scenes.

        The scene contains TWO sublayer nodes, both with filepaths under
        <tmp_path>/submitter_assets (which does not exist locally, so the path
        mapping rule is active):
        1. sublayer1: filepath1 is KEYFRAMED → must be skipped gracefully.
        2. sublayer2: filepath1 is a normal path → must still be remapped to
           <tmp_path>/worker_assets, proving one skipped parm does not cost the
           rest of the scene its path mapping.

        The path mapping rules are NON-EMPTY so _remap_lop_file_paths is
        actually exercised (it early-returns when HOUDINI_PATHMAP is empty).
        """
        houdini_version = _get_houdini_version(hython_location)

        # Create a HIP file with both a keyframed and a non-keyframed LOP sublayer
        hip_output = run_command(
            [
                str(hython_location),
                str(script_location / "lop_keyframe_pathmap_test" / "_test_hip.py"),
                "--",
                str(tmp_path),
            ]
        )
        assert hip_output.returncode == 0, "Failed to create test HIP file"

        job_template_location = script_location / "lop_keyframe_pathmap_test" / "template.yaml"
        job_params = {
            "HipFile": str(tmp_path / "test_lop_keyframe.hip"),
            "HoudiniVersion": houdini_version,
        }

        # Both sublayer parms point under <tmp_path>/submitter_assets, which
        # _test_hip.py deliberately does NOT create: Houdini deactivates a
        # HOUDINI_PATHMAP rule whose source path exists locally, reporting
        # "# Not active. Source path exists.". The destination worker_assets DOES
        # exist. This mirrors a real render - scene holds submitter paths, the
        # rule redirects them to worker paths.
        #
        # Paths are forward-slashed to match the parm values written by
        # _test_hip.py; the adaptor forward-slashes both sides of every rule when
        # building HOUDINI_PATHMAP (see _set_houdini_pathmap). source_path_format
        # must be one of openjd.sessions.PathFormat: POSIX / WINDOWS / URI.
        source_dir = (tmp_path / "submitter_assets").as_posix()
        dest_dir = (tmp_path / "worker_assets").as_posix()
        source_format = "WINDOWS" if os.name == "nt" else "POSIX"

        path_mapping_rules = json.dumps(
            {
                "version": "pathmapping-1.0",
                "path_mapping_rules": [
                    {
                        "source_path_format": source_format,
                        "source_path": source_dir,
                        "destination_path": dest_dir,
                    }
                ],
            }
        )

        output = run_command(
            [
                "openjd",
                "run",
                str(job_template_location),
                "--step",
                "GeoRender",
                "--job-param",
                json.dumps(job_params),
                "--path-mapping-rules",
                path_mapping_rules,
            ]
        )

        combined_output = output.stdout.decode("utf-8", errors="replace") + output.stderr.decode(
            "utf-8", errors="replace"
        )

        # The adaptor must not raise hou.OperationFailed (the PR #355 crash).
        assert (
            "hou.OperationFailed" not in combined_output
        ), "Adaptor crashed on keyframed LOP parm — PR #355 regression is present"

        # The fix emits a skip message when it encounters a keyframed parm.
        # Matching only the stable prefix so a reworded message does not silently
        # turn this assertion into a false positive.
        assert "Skipping LOP parm" in combined_output, (
            "Expected the adaptor to log a skip message for the keyframed parm, "
            "indicating _remap_lop_file_paths was exercised with path mapping rules"
        )

        # The non-keyframed sublayer2 parm should be successfully remapped,
        # proving the adaptor still does its job for normal parms.
        assert "Remapped LOP parm" in combined_output, (
            "Expected the adaptor to remap the non-keyframed sublayer2 parm, "
            "proving path mapping still works for normal parms"
        )

        assert output.returncode == 0, (
            f"Adaptor exited with non-zero return code.\n"
            f"Combined output:\n{combined_output[-2000:]}"
        )
