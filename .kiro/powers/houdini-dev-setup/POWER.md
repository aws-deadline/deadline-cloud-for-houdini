---
name: houdini-dev-setup
version: 1.0.0
displayName: Houdini Dev Setup
description: Automated development environment setup for deadline-cloud-for-houdini - builds packages, installs dependencies, and configures environment variables
keywords:
  - houdini
  - deadline
  - setup
  - build
  - install
  - environment
  - development
  - hatch
  - openjd
author: AWS Deadline Cloud Team
---

# Houdini Dev Setup Power

Automated development environment setup for deadline-cloud-for-houdini project.

## What This Power Does

This power automates the complete development environment setup for working on the deadline-cloud-for-houdini project. It handles everything from reading documentation to building packages, installing dependencies, and configuring environment variables.

## Setup Steps Performed

1. **Documentation Review** - Reads README.md and DEVELOPMENT.md to understand project requirements
2. **Houdini Detection** - Verifies Houdini installation and identifies version (19.5, 20.0, 20.5, or 21.0)
3. **Hatch Installation** - Installs and configures Hatch build tool
4. **Package Build** - Builds wheel and source distributions with `hatch build`
5. **Dev Submitter Installation** - Runs `hatch run install --houdini-version X.Y` to create Houdini package JSON and `plugin_env_{X.Y}/`
6. **Wheel Build** - Builds adaptor wheels with `./scripts/build_wheels.sh`
7. **OpenJD CLI Installation** - Installs openjd-cli for running integration tests
8. **Test Packages Installation** - Installs pytest, coverage, and test dependencies
9. **Git LFS Pull** - Retrieves expected test images for integration tests
10. **Environment Configuration** - Sets up HYTHON_EXECUTABLE, HOUDINI_VERSION, and PATH

## Prerequisites

- Python 3.9 or higher installed on system
- Houdini 19.5, 20.0, 20.5, or 21.0 installed with a valid license
- Linux, macOS, or Windows operating system
- Git LFS installed (for integration test images)

## Houdini Version to Python Version Mapping

| Houdini | Python |
|---------|--------|
| 19.5    | 3.9    |
| 20.0    | 3.10   |
| 20.5    | 3.11   |
| 21.0    | 3.11   |

## Usage

The power will prompt you for:
- **Houdini Version** (e.g., 19.5, 20.0, 20.5, 21.0)
- **Houdini Installation Path** (if not in standard location)

## What Gets Installed

### System Python Packages
- `hatch` - Build tool and environment manager

### Plugin Environment (`plugin_env_{X.Y}/`)
- `deadline` - AWS Deadline Cloud client library
- `openjd-adaptor-runtime` - OpenJD adaptor runtime
- All transitive dependencies (boto3, botocore, qtpy, psutil, etc.)

### Development Packages
- `openjd-cli` - OpenJD command-line interface
- `pytest`, `pytest-cov`, `pytest-xdist` - Test framework and plugins
- `coverage` - Code coverage measurement
- `ruff`, `black`, `mypy` - Linting and formatting tools

### Environment Variables
- `HYTHON_EXECUTABLE` - Full path to hython binary
- `HOUDINI_VERSION` - Full version string (e.g., `<MAJOR.MINOR.PATCH>`)
- `PATH` - Adds Houdini bin directory

## Platform-Specific Houdini Paths

### Linux
- Houdini install: `/opt/hfs{VERSION}/`
- hython: `/opt/hfs{VERSION}/bin/hython`
- User prefs: `~/houdini{MAJOR.MINOR}/`
- Package JSON: `~/houdini{MAJOR.MINOR}/packages/deadline_submitter_for_houdini.json`

### macOS
- Houdini install: `/Applications/Houdini/Houdini{VERSION}/`
- hython: `/Applications/Houdini/Houdini{VERSION}/Frameworks/Houdini.framework/Resources/bin/hython`
- User prefs: `~/Library/Preferences/houdini/{MAJOR.MINOR}/`
- Package JSON: `~/Library/Preferences/houdini/{MAJOR.MINOR}/packages/deadline_submitter_for_houdini.json`

### Windows
- Houdini install: `C:\Program Files\Side Effects Software\Houdini {VERSION}\`
- hython: `C:\Program Files\Side Effects Software\Houdini {VERSION}\bin\hython.exe`
- User prefs: `%USERPROFILE%\Documents\houdini{MAJOR.MINOR}\`
- Package JSON: `%USERPROFILE%\Documents\houdini{MAJOR.MINOR}\packages\deadline_submitter_for_houdini.json`

## After Setup

Once setup is complete, you can:

### Run Unit Tests
```bash
hatch run test
```

### Run Integration Tests
```bash
hatch run integ:test
```

### Build Package
```bash
hatch build
```

### Format and Lint Code
```bash
hatch run fmt
hatch run lint
```

## Troubleshooting

### Hatch Not Found
Restart your terminal or add to PATH:
```bash
# Linux/macOS
export PATH="$HOME/.local/bin:$PATH"
```

### hython Not Found
Set HYTHON_EXECUTABLE to the full path of your hython binary. See Platform-Specific Houdini Paths above.

### Plugin Not Loading in Houdini
1. Verify the package JSON exists at the correct user prefs path
2. Check that `plugin_env_{X.Y}/` was created in the repo root
3. Restart Houdini after running `hatch run install`

### Windows: pywin32 Required
On Windows, after installing pywin32 into Houdini's Python site-packages, run the post-install script and grant permissions:
```powershell
# Run in elevated PowerShell after installing Houdini
& "<HOUDINI_PYTHON>/python.exe" "<HOUDINI_SITE_PACKAGES>/win32/scripts/pywin32_postinstall.py" -install
icacls "<HOUDINI_SITE_PACKAGES>/win32" /grant "<YOUR_USER>:(OI)(CI)R" /T
icacls "<HOUDINI_SITE_PACKAGES>/pywin32_system32" /grant "<YOUR_USER>:(OI)(CI)R" /T
icacls "<HOUDINI_SITE_PACKAGES>/pywin32.pth" /grant "<YOUR_USER>:R"
```

## Notes

- Setup works on Linux, macOS, and Windows
- The `hatch run install` command creates a Houdini package JSON pointing to the repo source — changes to code are live on next Houdini launch
- The adaptor requires `hython` to be in PATH or `HYTHON_EXECUTABLE` set
- Integration tests require `deadline auth logout` first to avoid queue parameter interference
- Set `DEADLINE_ENABLE_DEVELOPER_OPTIONS=true` to access developer features (Include Adaptor Wheels)
