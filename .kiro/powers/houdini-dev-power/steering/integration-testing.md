# Integration Testing Guide

## Prerequisites

Before running integration tests:

1. **Houdini installed** with a valid license
2. **Dev submitter installed:** `hatch run install --houdini-version X.Y`
3. **Git LFS pulled:** `git lfs pull` (retrieves expected test images)
4. **Logged out of Deadline Cloud Monitor:** `deadline auth logout`
   - Running tests while logged in writes queue parameters to test job templates, which fails tests
5. **Environment variables set:** `HYTHON_EXECUTABLE` and `HOUDINI_VERSION`

## Environment Variables

| Variable | Description | Example (Linux) |
|----------|-------------|-----------------|
| `HYTHON_EXECUTABLE` | Full path to hython binary | `/opt/hfs<VERSION>/bin/hython` |
| `HOUDINI_VERSION` | Full Houdini version string | `<VERSION>` |

### Setting Environment Variables

**Linux:**
```bash
export HYTHON_EXECUTABLE="/opt/hfs<VERSION>/bin/hython"
export HOUDINI_VERSION="<VERSION>"
```

**macOS:**
```bash
export HYTHON_EXECUTABLE="/Applications/Houdini/Houdini<VERSION>/Frameworks/Houdini.framework/Resources/bin/hython"
export HOUDINI_VERSION="<VERSION>"
```

**Windows (PowerShell):**
```powershell
$Env:HYTHON_EXECUTABLE = "C:\Program Files\Side Effects Software\Houdini <VERSION>\bin\hython.exe"
$Env:HOUDINI_VERSION = "<VERSION>"
```

### Windows: Install Dependencies into Houdini's Python

On Windows, install test dependencies into Houdini's Python site-packages with Admin privileges:
```powershell
# Python version: 3.9 for 19.5, 3.10 for 20.0, 3.11 for 20.5/21.0
pip install -r requirements-integ-dcc-env.txt --python-version=<PYTHON_VERSION> --only-binary=:all: --target="C:\Program Files\Side Effects Software\Houdini <VERSION>\python<PYTHON_VERSION_NO_DOT>\lib\site-packages"
```

Then run the pywin32 post-install script and grant permissions:
```powershell
# Run in elevated PowerShell
& "<HOUDINI_PYTHON>/python.exe" "<HOUDINI_SITE_PACKAGES>/win32/scripts/pywin32_postinstall.py" -install
icacls "<HOUDINI_SITE_PACKAGES>/win32" /grant "<YOUR_USER>:(OI)(CI)R" /T
icacls "<HOUDINI_SITE_PACKAGES>/pywin32_system32" /grant "<YOUR_USER>:(OI)(CI)R" /T
icacls "<HOUDINI_SITE_PACKAGES>/pywin32.pth" /grant "<YOUR_USER>:R"
```

## Running Integration Tests

### All Integration Tests
```bash
hatch run integ:test
```
On Windows, you may need Admin privileges.

### Submitter Tests Only
```bash
hatch run integ:test_submitters
```
Runs tests decorated with `@pytest.mark.submitter`.

### Adaptor Tests Only
```bash
hatch run integ:test_adaptors
```
Runs tests decorated with `@pytest.mark.adaptor`.

### Verbose Output
```bash
hatch run integ:test -- -v --tb=long
```

## Test Structure

```
test/integ/
├── test_houdini_submitters.py    # Submitter integration tests
├── test_houdini_adaptors.py      # Adaptor integration tests
├── conftest.py                   # Shared fixtures
├── helpers/
│   ├── image_comparison.py       # Render output comparison
│   └── test_runners.py           # Test execution helpers
└── test_scripts/                 # Test scenes
    ├── minimal_test/             # Basic render test
    ├── render_dependencies_test/ # Multi-dependency render
    ├── wedge_node_test/          # Wedge node variations
    └── usd_scene_test/           # USD scene test
```

Each test script directory contains:
- A scene script (`.py`) that creates a Houdini scene programmatically
- An `expected_job_bundle/` directory with the expected OpenJD job template
- Expected rendered images (stored in Git LFS)

## How Integration Tests Work

### Submitter Tests (`test_houdini_submitters.py`)
1. Launch hython with the test scene script
2. The script creates a Houdini scene, adds a Deadline Cloud ROP node, and saves a job bundle
3. Compare the saved job bundle against the expected job bundle
4. Compare rendered images against expected images

### Adaptor Tests (`test_houdini_adaptors.py`)
1. Use `openjd-cli` to run the adaptor with a test job bundle
2. The adaptor launches hython, loads the scene, and renders
3. Compare output against expected results

## Multi-Version Testing

### Manual Multi-Version Script
Run integration tests across multiple Houdini versions:
```bash
python scripts/run_integ_tests.py '{"<VERSION_A>": "/opt/hfs<VERSION_A>/bin/hython", "<VERSION_B>": "/opt/hfs<VERSION_B>/bin/hython"}'
```

### CI Environment (`integ-ci`)
The `integ-ci` hatch environment runs a matrix across Houdini versions. The specific versions are defined in `hatch.toml` under `[[envs.integ-ci.matrix]]`.

```bash
hatch build
hatch run integ-ci:setup   # Downloads and installs Houdini from S3
hatch run integ-ci:test
```

CI environment variables:
- `INSTALLER_BUCKET` — S3 bucket with Houdini installers
- `INSTALLER_BUCKET_EXPECTED_OWNER` — 12-digit AWS account ID owning the bucket

## Job Bundle Integration Tests

The `job_bundle_integ_tests/` directory contains an OpenJD job template for running integration tests on Service Managed Fleets:

```bash
deadline bundle submit --farm-id FARM_ID --queue-id QUEUE_ID job_bundle_integ_tests/
```

This template:
1. Sets up the test environment (installs package, registers HDA)
2. Runs submitter tests (`-m submitter`)
3. Runs adaptor tests (`-m adaptor`)

See `job_bundle_integ_tests/README.md` for details.

## PDG Integration Tests

The PDG integ tests cook TOP networks with the Deadline Cloud Scheduler node on a real Deadline Cloud farm, then check the cook against the farm task by task. The scheduler is experimental. The tests submit real jobs, so they run only when you name the farm and queue they may submit to.

### Running

```bash
export HYTHON_EXECUTABLE="/opt/hfs<VERSION>/bin/hython"
export PDG_INTEG_FARM_ID=farm-...
export PDG_INTEG_QUEUE_ID=queue-...
export PDG_TEST_SCENES_DIR=~/pdg-test-scenes   # only for scenes you download

hatch run integ:test_pdg                  # the smoke set
hatch run integ:test_pdg_full             # also the large scenes
hatch run integ:test_pdg -k minimal_cook  # one scene; other arguments go to pytest
hatch run integ:test_pdg -k refused       # only the refusal tests, which make no job
```

Both scripts run `scripts/run_pdg_integ_tests.py`, which automated runs can call directly. When `PDG_INTEG_FARM_ID` or `PDG_INTEG_QUEUE_ID` is unset, it offers your default farm and queue by name and ID in an interactive terminal, and uses them for that run only if you answer `y`. Otherwise it exits with status 2 and runs no tests. A plain `hatch run integ:test` skips the PDG tests and says why.

| Variable | What it does |
|----------|--------------|
| `PDG_INTEG_FARM_ID`, `PDG_INTEG_QUEUE_ID` | The farm and queue the test jobs go to. hython gets them through a copy of your Deadline config, so your own config is not changed. |
| `PDG_TEST_SCENES_DIR` | The folder holding the downloaded scenes. With the farm set, a test of such a scene fails if this is unset or the file is missing. |
| `HYTHON_EXECUTABLE` or `HOUDINI_VERSION` | Which hython cooks, found as for the other integ tests. |

The tests need hython with a Houdini license, your Deadline Cloud login, and `hatch` on your `PATH`. Your Houdini packages are skipped, so a dev install of another checkout isn't loaded. hython loads this checkout's scheduler and submitter, with the dependencies in `plugin_env_<major.minor>`. That folder is built with the dev install's builder the first time; delete it to rebuild it. Each session builds this checkout's wheel, and downloads the `deadline` and `openjd-adaptor-runtime` wheels at the versions in that folder, and the workers install them.

### Test scenes

`test/integ/pdg/scenes.yaml` lists every scene and the result its cook must give. A scene that must cook lists its outcome, its steps and their task counts, and for one scene the host requirements every step must carry. A scene that must be refused lists every refusal its cook logs. A scene is either:

- **built**: a script in `test/integ/pdg/scene_builders/` builds it in hython when the test runs; or
- **external**: a `.hip` file you download into `PDG_TEST_SCENES_DIR`. Save each one under the file name `scenes.yaml` gives it. Its SHA-256 is pinned there, so a different file is refused.

`max_minutes` is how long a cook may take. A cook that runs longer is killed, and its job is canceled. Each built refusal scene is the smallest graph that triggers one refusal.

### What a cook test checks

`test_scene_cooks_end_to_end` cooks each scene through the TOP Cook button's path, `cookWorkItems(block=True)`, in its own temporary folder. It passes when:

- the cook ends within `max_minutes`;
- `cook.json`'s outcome and the snapshot's steps equal `scenes.yaml`;
- the comparison finds no difference between the cook, PDG's work items and the farm: each task's status, the files it uploaded, and their tags.

The same comparison runs by hand on any cook's debug directory: `hatch run integ:compare_pdg_cook_with_farm <debug_dir>`.

### What a refusal test checks

`test_refused_scene_creates_no_job` cooks each scene that must be refused in the same way. It passes when:

- the cook ends within `max_minutes`;
- the cook's `Refused, <subject> (<scenario>)` log lines and the scene's expected refusals pair one to one: each expected refusal takes its own line with the same scenario and a subject that contains `subject_contains`, and no line is left over;
- one of the scheduler node's errors holds "The graph was refused (";
- SearchJobs finds no job on the queue, created since the test started, with the cook's job name or cook ID. The search starts 5 minutes early, so a workstation clock ahead of the service's can't hide a job. A job it finds is canceled, and the test fails, naming it.

Every cook's job name is `pdg-integ-<scene id>-<8 random hex digits>`, so the search can't find another run's job. A refusal test builds no worker wheels.

## Troubleshooting

### Tests fail with "queue parameters" errors
Run `deadline auth logout` before testing. Being logged in causes queue parameters to leak into test job templates.

### Image comparison failures
1. Run `git lfs pull` to ensure expected images are downloaded
2. Check that the Houdini version matches the expected images
3. Small rendering differences across platforms are expected — check the tolerance thresholds

### hython not found
Verify `HYTHON_EXECUTABLE` points to the correct binary and the file is executable.

### Permission denied on Windows
Run PowerShell as Administrator for integration tests that need to write to Houdini's installation directory.
