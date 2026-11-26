# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Install a repository-pinned Houdini build into fresh GUI-test storage.

Reuse the scanned installer owner/digest checks from setup-runner.py without
running its global submitter, embedded-Python, or licensing setup.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path


def stage_shared_resources(runtime: Path, root: Path) -> Path | None:
    """Stage Houdini 22 SHFS files after verifying the scanned installer manifest."""
    manifest = runtime / "shfs/file_list.json"
    if not manifest.is_file():
        return None
    source = Path(os.environ["ProgramFiles"]) / "Side Effects Software/shfs"
    target = root / "shared"
    target.mkdir()
    for relative, metadata in json.loads(manifest.read_text())["files"].items():
        if not metadata["required"]:
            continue
        filename = Path(relative)
        if filename.is_absolute() or filename.drive or ".." in filename.parts:
            raise RuntimeError(f"Unsafe shared-resource path: {relative}")
        original = source / filename
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        if digest != metadata["checksum"]:
            raise RuntimeError(f"Shared Houdini resource checksum mismatch: {relative}")
        destination = target / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="22.0.368")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This installer requires Windows.")

    spec = importlib.util.spec_from_file_location(
        "houdini_runner_setup", Path(__file__).with_name("setup-runner.py")
    )
    assert spec is not None and spec.loader is not None
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    if args.version not in setup.HOUDINI_VERSION_CONFIG:
        parser.error("Select a version pinned in pipeline/setup-runner.py.")

    root = args.directory.resolve()
    # Never reuse or modify an existing installed runtime.
    root.mkdir(parents=True, exist_ok=False)
    runtime = root / f"houdini-{args.version}"
    archive = root / "installer.exe"
    config = setup.HOUDINI_VERSION_CONFIG[args.version]
    major_minor = ".".join(args.version.split(".")[:2])
    setup.download_from_s3(
        f"houdini/{major_minor}/houdini-{args.version}-win64-{config['vc']}.exe",
        archive,
    )
    setup.verify_checksum(archive, setup.HOUDINI_CHECKSUMS[args.version]["windows"])
    with (root / "installer.log").open("w", encoding="utf-8") as log:
        subprocess.run(
            [
                str(archive),
                "/S",
                "/AcceptEULA=2021-10-13",
                f"/InstallDir={runtime}",
                "/InstallHoudini=Yes",
                "/InstallLicense=No",
                "/InstallMenus=No",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1800,
        )
    for executable in ("houdini.exe", "hython.exe", "hserver.exe"):
        if not (runtime / "bin" / executable).is_file():
            raise RuntimeError(f"Installation missing {executable}")
    shared = stage_shared_resources(runtime, root)
    archive.unlink()
    print(f"Installed verified Houdini {args.version}.")
    if args.github_env:
        with args.github_env.open("a", encoding="utf-8") as env:
            env.write(f"HOUDINI_EXECUTABLE={runtime / 'bin' / 'houdini.exe'}\n")
            env.write(f"HYTHON_EXECUTABLE={runtime / 'bin' / 'hython.exe'}\n")
            env.write(f"HOUDINI_VERSION={args.version}\n")
            env.write(f"HFS={runtime}\n")
            if shared is not None:
                env.write(f"SHFS={shared}\n")


if __name__ == "__main__":
    main()
