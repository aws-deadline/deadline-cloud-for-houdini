import json
import subprocess

from pathlib import Path


def run_command(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    output = subprocess.run(args, capture_output=True)

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


def is_valid_template(template_location: Path) -> bool:
    output = run_command(["openjd", "check", str(template_location), "--output", "json"])
    output_json = json.loads(output.stdout)
    return output_json["status"] == "success"
