# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import subprocess
from pathlib import Path
from typing import Any


def run_command(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    output = subprocess.run(args, capture_output=True, check=False)

    print(f"Ran the following: {' '.join(output.args)}")
    print(f"\nstdout:\n\n{output.stdout.decode('utf-8', errors='replace')}")
    print(f"\nstderr:\n\n{output.stderr.decode('utf-8', errors='replace')}")

    return output


def run_houdini_submitter_test(
    hython_location: Path, test_script_location: Path, *additional_args
) -> subprocess.CompletedProcess[bytes]:
    args = [str(hython_location), str(test_script_location)]

    if additional_args:
        args.extend(["--", *additional_args])

    return run_command(args)


def run_houdini_adaptor_test(template_location: Path, job_params: dict[str, Any]) -> None:
    import yaml

    # Parse template to get step names
    with open(template_location) as f:
        template = yaml.safe_load(f)

    steps = [step["name"] for step in template.get("steps", [])]

    # Run each step separately to avoid connection file race condition
    for step_name in steps:
        output = run_command(
            [
                "openjd",
                "run",
                str(template_location),
                "--step",
                step_name,
                "--job-param",
                json.dumps(job_params),
            ]
        )
        assert output.returncode == 0


def is_valid_template(template_location: Path) -> bool:
    output = run_command(["openjd", "check", str(template_location), "--output", "json"])
    output_json = json.loads(output.stdout)
    return output_json["status"] == "success"
