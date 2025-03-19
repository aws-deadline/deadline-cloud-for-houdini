import argparse
import json
import os
import subprocess
import sys


def run_test(version: tuple[str], executable: str):
    """
    Sets up and runs integration tests for a provided version of Houdini and its Hython executable.

    Arguments:
        version (tuple[str]): A tuple of the Houdini version in the format (MAJOR, MINOR, PATCH)

        executable (str): A path pointing to the Hython executable to use in the tests.

    Raises:
        CalledProcessError: If one of the subprocesses exits with a non-zero error code.
    """

    subprocess.run(
        ["hatch", "run", "install", "--houdini-version", f"{version[0]}.{version[1]}"], check=True
    )

    os.environ["HYTHON_EXECUTABLE"] = executable
    os.environ["HOUDINI_VERSION"] = f"{version[0]}.{version[1]}.{version[2]}"

    subprocess.run(["hatch", "run", "integ:test"], check=True)


if __name__ == "__main__":
    """
    Script to run integration tests against many Houdini versions consecutively. Fails early if any versions' setup or tests fail.
    """

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "config",
        type=str,
        help="A JSON string containing a dictionary of each Houdini version to test with (MAJOR.MINOR.PATCH) and the corresponding Hython executable.",
    )

    args = parser.parse_args(sys.argv[1:])
    config = json.loads(args.config)

    for version, path in config.items():
        run_test(tuple(version.split(".")), path)
