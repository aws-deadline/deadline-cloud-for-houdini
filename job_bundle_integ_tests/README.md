# Houdini Integration Tests on Deadline Cloud

Runs the deadline-cloud-for-houdini integration tests on a Deadline Cloud Service Managed Fleet.

## Prerequisites

- A Deadline Cloud farm with a queue that has the default Conda queue environment.
- The `deadline` CLI installed and configured (`deadline config`).
- Git LFS files resolved (expected test images are stored in LFS):
  ```bash
  cd /path/to/deadline-cloud-for-houdini
  git lfs install
  git lfs pull
  ```

## Usage

```bash
cd /path/to/deadline-cloud-for-houdini
deadline bundle submit job_bundle_integ_tests \
  --name "houdini-integ-tests" \
  --parameter "RepoDir=." \
  --parameter "CondaPackages=houdini=21.0" \
  --max-retries-per-task 0
```

## Steps

| Step | Description |
|------|-------------|
| `submitter` | Runs submitter tests (`test_houdini_submitters.py`). Validates that the Houdini submitter generates correct job bundles from HIP scenes. |
| `adaptor` | Runs adaptor tests (`test_houdini_adaptors.py`). Validates that the Houdini adaptor renders scenes correctly with Mantra. |

## How It Works

1. The repo is uploaded as a job attachment via the `RepoDir` input parameter.
2. A shared job environment installs the package and test dependencies into `hython`'s Python environment.
3. The `deadline_cloud_for_houdini` Houdini plugin module is copied directly into `hython`'s site-packages. This is necessary because the plugin is not part of the pip-installable package, and `hython` ignores `PYTHONPATH`.
4. A Houdini package JSON is created at `~/houdini{version}/packages/` to register the `deadline_cloud` HDA node type. This is equivalent to running `hatch run install --houdini-version X.Y` locally.
5. The `HYTHON_EXECUTABLE` environment variable is set so the test fixtures can locate `hython`.

## Customization

- To test against a different Houdini version, change the `CondaPackages` parameter (e.g., `houdini=20.5`).
- To run only specific tests, modify the pytest marker filter in the step's run script.


deadline bundle submit ~/workplace/jairaws/job-bundles/houdini-integ-tests-v3 \
  --name "houdini-integ-tests" \
  --parameter "RepoDir=/Users/ruizjair/workplace/jairaws/deadline-cloud-for-houdini" \
  --parameter "CondaPackages=houdini=21.0" \
  --max-retries-per-task 0