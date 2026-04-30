# Houdini Dev Setup — Automated Workflow

Complete automated setup workflow for deadline-cloud-for-houdini development environment.

## Setup Workflow

### Step 1: Prompt for Houdini Version

Ask the user which version of Houdini to set up for:
- Supported: 19.5, 20.0, 20.5, 21.0
- Default: 21.0

Example prompt:
```
Which version of Houdini would you like to set up for? (default: 21.0)
```

### Step 2: Verify Houdini Installation

Check if Houdini is installed and hython is accessible.

**Linux:**
```bash
test -x "/opt/hfs${VERSION}/bin/hython" && echo "Found" || echo "Not found"
```

**macOS:**
```bash
test -x "/Applications/Houdini/Houdini${VERSION}/Frameworks/Houdini.framework/Resources/bin/hython" && echo "Found" || echo "Not found"
```

**Windows (PowerShell):**
```powershell
Test-Path "C:\Program Files\Side Effects Software\Houdini ${VERSION}\bin\hython.exe"
```

If Houdini is NOT found, abort and instruct the user to install Houdini from [SideFX](https://www.sidefx.com/download/).

If found, confirm the version and continue.

### Step 3: Read Project Documentation

Read and extract key information from:
1. `README.md` - Project overview, compatibility, requirements
2. `DEVELOPMENT.md` - Development workflow, build instructions

### Step 4: Install Hatch

Check if hatch is installed:
```bash
hatch --version
```

If not installed:
```bash
pip install hatch
```

Verify:
```bash
hatch --version
hatch env show
```

### Step 5: Build the Package

```bash
hatch build
```

Expected output in `dist/`:
- `deadline_cloud_for_houdini-{VERSION}-py3-none-any.whl`
- `deadline_cloud_for_houdini-{VERSION}.tar.gz`

### Step 6: Install Dev Submitter Plugin

This creates a Houdini package JSON and installs dependencies into `plugin_env_{MAJOR.MINOR}/`:

```bash
hatch run install --houdini-version MAJOR.MINOR
```

For example:
```bash
hatch run install --houdini-version 21.0
```

This script (`scripts/install_dev_submitter.py`):
1. Resolves dependencies from `pyproject.toml`
2. Installs them into `plugin_env_{MAJOR.MINOR}/` using pip with `--platform` and `--python-version` flags
3. Creates a Houdini package JSON at `~/houdini{MAJOR.MINOR}/packages/deadline_submitter_for_houdini.json` (Linux), `~/Library/Preferences/houdini/{MAJOR.MINOR}/packages/` (macOS), or `%USERPROFILE%\Documents\houdini{MAJOR.MINOR}\packages\` (Windows)

The package JSON sets:
- `DEADLINE_CLOUD_FOR_HOUDINI` → path to `src/deadline/houdini_submitter/`
- `PYTHONPATH` → includes `python/`, `src/`, and `plugin_env_{MAJOR.MINOR}/`
- `hpath` → `$DEADLINE_CLOUD_FOR_HOUDINI` (adds HDA and other Houdini resources)

Verify by checking the JSON file exists:
```bash
# Linux
cat ~/houdini21.0/packages/deadline_submitter_for_houdini.json

# macOS
cat ~/Library/Preferences/houdini/21.0/packages/deadline_submitter_for_houdini.json
```

### Step 7: Build Adaptor Wheels

Build wheels for adaptor development workflow:
```bash
pip install build  # if not already installed
./scripts/build_wheels.sh
```

Verify wheels exist:
```bash
ls ./wheels/
# deadline_cloud_for_houdini-{VERSION}-py3-none-any.whl
# deadline-{VERSION}-py3-none-any.whl
# openjd_adaptor_runtime-{VERSION}-py3-none-any.whl
```

### Step 8: Install Test Dependencies

Install test packages into the hatch environment:
```bash
hatch run sync  # installs requirements-testing.txt
```

For integration tests on **Windows**, install into Houdini's Python:
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

### Step 9: Pull Git LFS Files

Integration tests compare rendered output against expected images stored in Git LFS:
```bash
git lfs pull
```

### Step 10: Configure Environment Variables

Set these environment variables for integration testing:

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

### Step 11: Verify Installation

Run unit tests to confirm everything works:
```bash
hatch run test
```

Expected: all tests pass, coverage ≥ 65%.

Launch Houdini and verify the Deadline Cloud node appears:
1. Open Houdini
2. Go to Network View → `out` network
3. Press TAB, type `deadline`
4. The Deadline Cloud ROP node should appear

## Post-Setup Configuration

### Developer Options
Set `DEADLINE_ENABLE_DEVELOPER_OPTIONS=true` to enable:
- "Include Adaptor Wheels" option in the submitter
- TEST button for local testing

### Integration Test Prerequisite
Before running integration tests, log out of Deadline Cloud Monitor:
```bash
deadline auth logout
```
Running tests while logged in causes queue parameters to be written to test job templates, which fails the tests.

## Common Issues and Solutions

### `hatch run install` fails with pip errors
- Ensure you have network access for pip to download dependencies
- Check that the Houdini version string is correct (e.g., `21.0`, not `21`)
- Try `hatch env prune` and retry

### Plugin not visible in Houdini
- Verify the package JSON path matches your Houdini version
- Check that `plugin_env_{MAJOR.MINOR}/` exists and contains packages
- Restart Houdini completely (not just reload)

### Unit tests fail with import errors
- Run `hatch env prune` then `hatch run test` to recreate the environment
- Ensure `_version.py` exists (run `hatch build` first)

### Coverage below 65%
- The coverage threshold is configured in `pyproject.toml` under `[tool.coverage.report]`
- Coverage measures `src/deadline/houdini_adaptor` and `src/deadline/houdini_submitter`

## Environment Variables Reference

| Variable | Purpose | Example |
|----------|---------|---------|
| `HYTHON_EXECUTABLE` | Path to hython binary (integration tests) | `/opt/hfs<VERSION>/bin/hython` |
| `HOUDINI_VERSION` | Full Houdini version (integration tests) | `<VERSION>` |
| `DEADLINE_ENABLE_DEVELOPER_OPTIONS` | Enable dev features in submitter | `true` |
| `DEADLINE_CLOUD_FOR_HOUDINI` | Set by package JSON — path to submitter source | (auto-set) |
| `PYTHONPATH` | Set by package JSON — includes plugin_env and source | (auto-set) |

## File Structure After Setup

```
deadline-cloud-for-houdini/
├── dist/                          # Built wheel and sdist
├── wheels/                        # Adaptor wheels for dev workflow
├── plugin_env_21.0/               # Dependencies for Houdini 21.0
│   ├── deadline/
│   ├── openjd/
│   ├── boto3/
│   └── ...
├── src/deadline/
│   ├── houdini_submitter/         # Submitter plugin source
│   └── houdini_adaptor/           # Adaptor source
└── test/
    ├── unit/                      # Unit tests
    └── integ/                     # Integration tests
```

## Next Steps

After setup is complete:
1. Run `hatch run test` to verify unit tests pass
2. Launch Houdini and verify the Deadline Cloud node loads
3. For adaptor development, use `./scripts/build_wheels.sh` and "Include Adaptor Wheels" in the submitter
4. For integration tests, see the integration-testing steering file in houdini-dev-power
