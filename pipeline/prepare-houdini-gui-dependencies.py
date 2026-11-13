# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Stage dependencies for Houdini 22 without replacing its embedded Qt runtime."""

import argparse
import subprocess
from pathlib import Path

from uv import find_uv_bin


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        [
            find_uv_bin(),
            "pip",
            "install",
            "--target",
            str(args.directory),
            "--python-platform",
            "x86_64-pc-windows-msvc",
            "--python-version",
            "3.13",
            "--only-binary",
            ":all:",
            "deadline>=0.60.4,<0.61",
            "qtpy>=2.4,<3",
            "pywin32>=307,<311",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
