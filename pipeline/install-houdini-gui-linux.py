# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Install the checksum-verified Linux Houdini GUI in an ephemeral CI directory."""

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--github-env", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "setup_runner", Path(__file__).with_name("setup-runner.py")
    )
    assert spec is not None and spec.loader is not None
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    version = "22.0.368"
    args.directory.mkdir(parents=True, exist_ok=False)
    archive = args.directory / "installer.tar.gz"
    key = f"houdini/22.0/houdini-{version}-linux_x86_64_gcc14.2.tar.gz"
    # Existing installer code logs the private S3 location; suppress that
    # informational output while retaining owner/checksum validation.
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        setup.download_from_s3(key, archive)
        setup.verify_checksum(archive, setup.HOUDINI_CHECKSUMS[version]["linux"])
    unpack = args.directory / "unpack"
    with tarfile.open(archive) as source:
        source.extractall(unpack, filter="data")
    installer = unpack / f"houdini-{version}-linux_x86_64_gcc14.2"
    runtime = args.directory / f"hfs{version}"
    with (args.directory / "installer.log").open("w") as log:
        subprocess.run(
            [
                str(installer / "houdini.install"),
                "--auto-install",
                "--install-houdini",
                "--no-install-license",
                "--no-install-menus",
                "--no-install-bin-symlink",
                "--no-install-hfs-symlink",
                "--no-install-hqueue-server",
                "--no-root-check",
                "--accept-EULA",
                "2021-10-13",
                str(runtime),
            ],
            cwd=installer,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    for name in ("houdini", "hython", "hserver", "sesictrl"):
        if not os.access(runtime / "bin" / name, os.X_OK):
            raise RuntimeError(f"Installer did not produce {name}.")
    archive.unlink()
    import shutil

    shutil.rmtree(unpack)
    with args.github_env.open("a") as env:
        env.write(
            f"HOUDINI_VERSION={version}\nHFS={runtime}\n"
            f"HOUDINI_EXECUTABLE={runtime}/bin/houdini\n"
            f"HYTHON_EXECUTABLE={runtime}/bin/hython\n"
        )


if __name__ == "__main__":
    main()
