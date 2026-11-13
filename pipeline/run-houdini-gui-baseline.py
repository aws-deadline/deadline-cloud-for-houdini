# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Run the complete local baseline in a mock-only, owned Windows process tree."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from deadline_test_fixtures.deadline_mock import (
    MockDeadlineScenario,
    MockDeadlineServerProcess,
    build_mock_environment,
    write_deadline_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--gui-only", action="store_true", help="Run only the real GUI cases.")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "test/houdini_submitter_ui"))
    from _windows_process import WindowsProcessJob

    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "openjd-state").mkdir(exist_ok=False)
    server = MockDeadlineServerProcess(MockDeadlineScenario()).start()
    try:
        config = artifacts / "baseline-config"
        write_deadline_config(
            config,
            farm_id=server.scenario.farm_id,
            queue_id=server.scenario.queue_id,
            job_history_dir=artifacts / "history",
        )
        base = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AWS_", "ACTIONS_ID_TOKEN_"))
        }
        env = build_mock_environment(
            base,
            deadline_endpoint_url=server.base_url,
            config_path=config,
            home_dir=artifacts / "home",
        )
        env.update(
            HOUDINI_GUI_MOCK_ENDPOINT=server.base_url,
            AWS_ENDPOINT_URL=server.base_url,
            AWS_EC2_METADATA_DISABLED="true",
            AWS_CONFIG_FILE=str(artifacts / "empty-aws-config"),
            AWS_SHARED_CREDENTIALS_FILE=str(artifacts / "empty-aws-credentials"),
            # OpenJD uses PROGRAMDATA for Windows session storage. Keep all
            # session files inside this test's exclusively owned directory.
            PROGRAMDATA=str(artifacts / "openjd-state"),
        )
        suites = ["test/houdini_submitter_ui"]
        if not args.gui_only:
            suites += [
                "test/integ/test_houdini_submitters.py",
                "test/integ/test_houdini_adaptors.py",
            ]
        command = [
            sys.executable,
            "-m",
            "pytest",
            "--no-cov",
            "--numprocesses=0",
            "-vv",
            "-s",
            *suites,
            "--basetemp",
            str(artifacts / "cases"),
            "--junitxml",
            str(artifacts / "junit.xml"),
        ]
        process = subprocess.Popen(command, cwd=repo, env=env)
        job = None
        try:
            job = WindowsProcessJob(process)
            returncode = process.wait(timeout=2100)
        finally:
            if job is not None:
                job.close()
            elif process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            # OpenJD can race with a descendant holding its working directory.
            # The Job Object is closed first; retry only our isolated storage.
            for attempt in range(30):
                try:
                    shutil.rmtree(artifacts / "openjd-state")
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    if attempt == 29:
                        raise
                    time.sleep(1)
            (artifacts / "process-cleanup.txt").write_text(
                "Owned process job closed; isolated OpenJD session storage removed.\n",
                encoding="utf-8",
            )
        (artifacts / "baseline-mock-traffic.json").write_text(
            json.dumps(dict(server.backend.call_counts), indent=2), encoding="utf-8"
        )
        if server.backend.unmatched_requests or server.backend.call_counts.get("CreateJob", 0):
            raise RuntimeError("Unexpected baseline mock traffic")
        return returncode
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
