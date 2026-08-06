# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

#!/usr/bin/env python3
"""Setup runner for Houdini integration tests in CodeBuild."""
import argparse
import getpass
import hashlib
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config

HOUDINI_VERSION_CONFIG = {
    "19.5.805": {
        "gcc": "gcc9.3",
        "vc": "vc142",
        "clang": "clang14.0_13",
        "python": "3.9",
    },
    "20.0.896": {
        "gcc": "gcc11.2",
        "vc": "vc143",
        "clang": "clang14.0_13",
        "python": "3.10",
    },
    "20.5.613": {
        "gcc": "gcc11.2",
        "vc": "vc143",
        "clang": "clang15.0_14",
        "python": "3.11",
    },
    "21.0.440": {
        "gcc": "gcc11.2",
        "vc": "vc143",
        "clang": "clang15.0_14",
        "python": "3.11",
    },
    "22.0.368": {
        "gcc": "gcc14.2",
        "vc": "vc143",
        "clang": "clang17.0_15",
        "python": "3.13",
    },
}

HOUDINI_CHECKSUMS = {
    "19.5.805": {
        "linux": "85b04a6b250a5bdb3f5f229d00f95f84308a3f2a40486799a5dc081f586f8632",
        "windows": "3244510bcc3caefb509d94f79d61fc22ee05190fa5bb123750c100c31e694530",
        "macos": "8f4b6d3674f2677bcb93115cfee83076e7d978b35ceb743e31c8cbb1d84104b7",
    },
    "20.0.896": {
        "linux": "a68d3b2788db73774febe4e4cd3b5f5c31dca9e902b60b01c0c14b26d2886cc7",
        "windows": "e21c1b7ec2e80f83a66fe3c9b00dd5579112414efcf647dfaf407f5a94a392ef",
        "macos": "054c8457529a95475c6cc5d9377ba153746ecebd85a718c8d95c7fb0f4b1a192",
    },
    "20.5.613": {
        "linux": "5b38cf94fb7a87c9398614caef0b6e4357a0fb656e008b84e715bafb28e18fdf",
        "windows": "f721fbce0202c21c9bb47fae05aa91559818903c45e703db67da11bb494af88e",
        "macos": "4243eb9442c8c46d7e1a463d8d5b68163bbe09f34d4c811d3391e5141254b983",
    },
    "21.0.440": {
        "linux": "a87451f9146d52051a9ba142d535936638351526a48cba0c6156a221f3be58e6",
        "windows": "a78e468e99d1be3476b46062e3a043ecc435751b9a9f92e39bee414ace8ce59e",
        "macos": "3fc918428b22b3c32704d1163a6b1b08723ece71e5e0172e941167549d4d5b25",
    },
    "22.0.368": {
        "linux": "8765335f090a8329768b415b64bc9fb80a0d9963b13f63455ad042e32d353616",
        "windows": "b72c4ff9fff20e8cc9449ff1dd57dfab4c52421f77b267412fb5c9288b2a3c49",
        "macos": "a51dedfb764475e1da600432f40d213cd08d628e7ba2f5e3ccf7f8b9ecda6955",
    },
}


def run(cmd, check=True, cwd=None, stdin=None):
    print(f"Running: {cmd if isinstance(cmd, str) else shlex.join(cmd)}")
    result = subprocess.run(cmd, check=False, cwd=cwd, stdin=stdin)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result


def download_from_s3(s3_path, local_path):
    bucket = os.environ.get("INSTALLER_BUCKET")
    if not bucket:
        print("ERROR: INSTALLER_BUCKET not set")
        sys.exit(1)

    expected_bucket_owner = os.environ.get("INSTALLER_BUCKET_EXPECTED_OWNER")
    if not expected_bucket_owner:
        raise ValueError("INSTALLER_BUCKET_EXPECTED_OWNER environment variable is required")
    if not (expected_bucket_owner.isdigit() and len(expected_bucket_owner) == 12):
        raise ValueError("INSTALLER_BUCKET_EXPECTED_OWNER must be a 12-digit AWS Account ID")

    config = Config(read_timeout=300, connect_timeout=60, retries={"max_attempts": 2})

    s3 = boto3.client("s3", config=config)

    print(f"Downloading s3://{bucket}/{s3_path} to {local_path}")

    s3.download_file(
        bucket, s3_path, str(local_path), ExtraArgs={"ExpectedBucketOwner": expected_bucket_owner}
    )


def verify_checksum(file_path, expected_checksum):
    """Verify SHA256 checksum of downloaded file."""
    print(f"Verifying checksum for {file_path}...")
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)

    actual = sha256.hexdigest()
    if actual != expected_checksum:
        print("ERROR: Checksum mismatch!")
        print(f"  Expected: {expected_checksum}")
        print(f"  Actual:   {actual}")
        sys.exit(1)

    print("OK Checksum verified")
    return True


def setup_linux(houdini_versions):
    # Install bc (required by Houdini installer)
    pkg_mgr = (
        "dnf"
        if subprocess.run(["command", "-v", "dnf"], capture_output=True, check=False).returncode
        == 0
        else "yum"
    )
    # bc is required by the Houdini installer; libatomic provides
    # libatomic.so.1, which Houdini 22.0's hython links against.
    run([pkg_mgr, "install", "-y", "bc", "libatomic"])

    for version in houdini_versions:
        major_minor = ".".join(version.split(".")[:2])
        houdini_dir = Path(f"/opt/hfs{version}")
        houdini_marker = houdini_dir / ".installed"

        if houdini_marker.exists():
            print(f"Houdini {version} already installed")
            continue

        lock_file = Path(f"/tmp/houdini-{version}.lock")
        if lock_file.exists():
            print(f"Waiting for concurrent Houdini {version} install...")
            for _ in range(120):
                time.sleep(1)
                if houdini_marker.exists():
                    break
            continue

        lock_file.touch()
        try:
            print(f"Installing Houdini {version}...")
            gcc_version = HOUDINI_VERSION_CONFIG[version]["gcc"]
            houdini_archive = Path(f"/tmp/houdini-{version}.tar.gz")
            download_from_s3(
                f"houdini/{major_minor}/houdini-{version}-linux_x86_64_{gcc_version}.tar.gz",
                houdini_archive,
            )
            verify_checksum(houdini_archive, HOUDINI_CHECKSUMS[version]["linux"])

            run(["tar", "-xf", str(houdini_archive), "-C", "/tmp"])
            install_dir = Path(f"/tmp/houdini-{version}-linux_x86_64_{gcc_version}")
            run(["chmod", "-R", "777", str(install_dir)])

            # Create target directory with proper permissions
            houdini_dir.mkdir(parents=True, exist_ok=True)
            run(["chmod", "777", str(houdini_dir)])

            run(
                [
                    "./houdini.install",
                    "--auto-install",
                    "--install-houdini",
                    "--no-install-license",
                    "--no-install-menus",
                    "--no-install-bin-symlink",
                    "--install-hfs-symlink",
                    "--no-install-hqueue-server",
                    "--no-root-check",
                    "--accept-EULA",
                    "2021-10-13",
                    str(houdini_dir),
                ],
                cwd=install_dir,
            )

            # Verify installation
            hython_exe = houdini_dir / "bin" / "hython"
            if hython_exe.exists():
                print(f"SUCCESS: hython found at {hython_exe}")
                houdini_marker.touch()
            else:
                print(f"ERROR: hython NOT found at {hython_exe}")
                sys.exit(1)

            houdini_archive.unlink(missing_ok=True)
            run(["rm", "-rf", str(install_dir)], check=False)
        finally:
            lock_file.unlink(missing_ok=True)

    print("Installing Houdini submitter...")
    for version in houdini_versions:
        major_minor = ".".join(version.split(".")[:2])
        run(["hatch", "run", "install", "--houdini-version", major_minor])


def setup_windows(houdini_versions):
    for version in houdini_versions:
        major_minor = ".".join(version.split(".")[:2])
        houdini_dir = Path(f"C:/Tools/houdini-{version}")
        houdini_marker = houdini_dir / ".installed"

        if houdini_marker.exists():
            print(f"Houdini {version} already installed")
            continue

        print(f"Installing Houdini {version}...")
        vc_version = HOUDINI_VERSION_CONFIG[version]["vc"]
        houdini_installer = Path(f"C:/Tools/houdini-{version}-installer.exe")
        houdini_installer.parent.mkdir(parents=True, exist_ok=True)

        download_from_s3(
            f"houdini/{major_minor}/houdini-{version}-win64-{vc_version}.exe", houdini_installer
        )
        verify_checksum(houdini_installer, HOUDINI_CHECKSUMS[version]["windows"])

        print("Starting installation...")
        result = subprocess.run(
            [
                houdini_installer,
                "/S",
                "/AcceptEULA=2021-10-13",
                f"/InstallDir={houdini_dir}",
                "/InstallHoudini=Yes",
                "/InstallLicense=No",
                "/InstallMenus=No",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        print(f"Installation exit code: {result.returncode}")
        if result.stdout:
            print(f"Installation output: {result.stdout}")
        if result.stderr:
            print(f"Installation errors: {result.stderr}")

        # Verify installation
        print(f"Checking installation at {houdini_dir}")
        if houdini_dir.exists():
            print("Directory exists, contents:")
            run(["cmd", "/c", "dir", str(houdini_dir)], check=False)
            hython_exe = houdini_dir / "bin" / "hython.exe"
            if hython_exe.exists():
                print(f"SUCCESS: hython.exe found at {hython_exe}")
                houdini_marker.touch()  # Create marker on success
            else:
                print(f"ERROR: hython.exe NOT found at {hython_exe}")
        else:
            print(f"ERROR: Installation directory {houdini_dir} does not exist")

        houdini_installer.unlink(missing_ok=True)

    print("Installing Houdini submitter...")
    for version in houdini_versions:
        major_minor = ".".join(version.split(".")[:2])
        print(f"Installing submitter for Houdini {version} (major_minor: {major_minor})")
        result = subprocess.run(
            ["hatch", "run", "install", "--houdini-version", major_minor],
            capture_output=True,
            text=True,
            check=False,
        )
        print(f"Hatch install exit code: {result.returncode}")
        if result.stdout:
            print(f"Hatch install stdout: {result.stdout}")
        if result.stderr:
            print(f"Hatch install stderr: {result.stderr}")
        if result.returncode != 0:
            print(f"ERROR: Hatch install failed for Houdini {version}")
            sys.exit(result.returncode)

        # Install dependencies from requirements file into Houdini's Python
        houdini_dir = Path(f"C:/Tools/houdini-{version}")
        python_version = HOUDINI_VERSION_CONFIG[version]["python"]

        site_packages = (
            houdini_dir / f"python{python_version.replace('.', '')}" / "lib" / "site-packages"
        )

        # Find git root to locate requirements file
        git_root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        )
        git_root = Path(git_root_result.stdout.strip())
        requirements_file = git_root / "requirements-integ-dcc-env.txt"

        print(
            f"Installing dependencies into Houdini {version} Python (version {python_version})..."
        )
        run(
            [
                "pip",
                "install",
                "-r",
                str(requirements_file),
                f"--python-version={python_version}",
                "--only-binary=:all:",
                f"--target={site_packages}",
                "--no-cache-dir",
                "--force-reinstall",
            ]
        )


def setup_macos(houdini_versions):
    for version in houdini_versions:
        major_minor = ".".join(version.split(".")[:2])
        # Marker file stored outside app directory to prevent corruption
        houdini_marker = Path(
            f"~/Library/Application Support/.houdini-{version}-installed"
        ).expanduser()
        houdini_marker.parent.mkdir(parents=True, exist_ok=True)

        if houdini_marker.exists():
            print(f"Houdini {version} already installed")
            continue

        print(f"Installing Houdini {version}...")
        clang_version = HOUDINI_VERSION_CONFIG[version]["clang"]
        houdini_dmg = Path(f"/tmp/houdini-{version}.dmg")

        download_from_s3(
            f"houdini/{major_minor}/houdini-{version}-macosx_arm64_{clang_version}.dmg", houdini_dmg
        )
        verify_checksum(houdini_dmg, HOUDINI_CHECKSUMS[version]["macos"])

        run(["hdiutil", "attach", str(houdini_dmg)])
        run(["sudo", "installer", "-target", "/", "-pkg", "/Volumes/Houdini/Houdini.pkg"])
        run(["hdiutil", "detach", "/Volumes/Houdini"], check=False)

        # Verify installation
        hython_exe = Path(
            f"/Applications/Houdini/Houdini{version}/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython"
        )
        if hython_exe.exists():
            print(f"SUCCESS: hython found at {hython_exe}")
            houdini_marker.touch()
        else:
            print(f"ERROR: hython NOT found at {hython_exe}")
            sys.exit(1)

        houdini_dmg.unlink(missing_ok=True)

    # The sudo Houdini .pkg installer leaves ~/Library/Preferences/houdini/<ver>
    # root-owned; hand it back to the build user so the non-sudo submitter install
    # can write its package JSON. Before the loop so it also runs on cached runners.
    prefs_root = Path("~/Library/Preferences/houdini").expanduser()
    prefs_root.mkdir(parents=True, exist_ok=True)
    run(["sudo", "chown", "-R", f"{getpass.getuser()}:staff", str(prefs_root)], check=False)

    print("Installing Houdini submitter...")
    for version in houdini_versions:
        major_minor = ".".join(version.split(".")[:2])
        run(["hatch", "run", "install", "--houdini-version", major_minor])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup Houdini test environment")
    parser.add_argument("--versions", nargs="+", help="Houdini versions to install")
    args = parser.parse_args()

    houdini_versions = args.versions if args.versions else list(HOUDINI_VERSION_CONFIG.keys())

    system = platform.system()
    print(f"Setting up {system} with Houdini {', '.join(houdini_versions)}")

    if system == "Linux":
        setup_linux(houdini_versions)
    elif system == "Windows":
        setup_windows(houdini_versions)
    elif system == "Darwin":
        setup_macos(houdini_versions)
    else:
        print(f"Unsupported platform: {system}")
        sys.exit(1)
