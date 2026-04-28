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
