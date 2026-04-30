---
name: houdini-dev-power
version: 1.0.0
displayName: Houdini Dev Power
description: Development power for deadline-cloud-for-houdini - build, test, debug, and implement features for the Houdini submitter and adaptor
keywords:
  - houdini
  - deadline
  - development
  - testing
  - submitter
  - adaptor
  - openjd
  - hatch
author: AWS Deadline Cloud Team
---

# Houdini Dev Power

Development workflows for the deadline-cloud-for-houdini project. Covers building, testing, debugging, and implementing features for the Houdini submitter plugin and OpenJD adaptor.

## Quick Start

### Build
```bash
hatch build                                    # Build wheel + sdist
hatch run install --houdini-version 21.0       # Install dev submitter
```

### Test
```bash
hatch run test                                 # Unit tests
hatch run all:test                             # Unit tests across all Python versions
hatch run integ:test                           # Integration tests (requires Houdini)
hatch run integ:test_submitters                # Submitter integration tests only
hatch run integ:test_adaptors                  # Adaptor integration tests only
```

### Lint and Format
```bash
hatch run lint                                 # ruff + black + mypy
hatch run fmt                                  # Auto-format with black
```

### Build Adaptor Wheels
```bash
pip install build && ./scripts/build_wheels.sh
```

## Project Structure

```
src/deadline/
├── houdini_submitter/                    # Houdini plugin (loaded by Houdini)
│   ├── python/deadline_cloud_for_houdini/  # Submitter Python code
│   │   ├── submitter.py                    # Core submission logic
│   │   ├── _assets.py                      # Asset/file dependency detection
│   │   ├── queue_parameters.py             # Queue parameter handling
│   │   ├── houdini_submitter_widget.py     # Qt widget
│   │   ├── hip_settings.py                 # HIP file settings
│   │   └── constants.py                    # Constants
│   ├── otls/deadline_cloud.hda/            # Houdini Digital Asset (ROP node)
│   │   └── Driver_1deadline__cloud/        # HDA definition files
│   ├── panel/submitter_panel.py            # Houdini panel
│   └── soho/deadline_cloud_soho.py         # SOHO integration
│
└── houdini_adaptor/                      # OpenJD adaptor (pip-installable)
    ├── HoudiniAdaptor/
    │   ├── adaptor.py                      # Main adaptor (on_start, on_run, on_stop, on_cleanup)
    │   ├── __main__.py                     # CLI entry point
    │   └── schemas/                        # JSON schemas for init_data and run_data
    └── HoudiniClient/
        ├── houdini_client.py               # Client running inside Houdini
        └── houdini_handler.py              # Command handler
```

## Two-Module Architecture

1. **Submitter** (`houdini_submitter/`) — A Houdini plugin loaded via the Houdini package system. NOT pip-installable. Contains the ROP node (HDA), UI panel, and submission logic.

2. **Adaptor** (`houdini_adaptor/`) — A standard pip-installable Python package. Provides the `houdini-openjd` CLI command. Runs on the render farm to execute Houdini renders via the OpenJD adaptor runtime lifecycle.

## Development Workflows

### Submitter Development
1. Run `hatch run install --houdini-version X.Y` once
2. Edit code in `src/deadline/houdini_submitter/python/deadline_cloud_for_houdini/`
3. Restart Houdini to pick up changes
4. Run `hatch run test` to verify

### Adaptor Development
1. Build wheels: `pip install build && ./scripts/build_wheels.sh`
2. In the Houdini submitter, enable "Include Adaptor Wheels" and point to `wheels/`
3. Submit a test job to a service-managed fleet
4. Run `hatch run test` to verify

### HDA Development
1. Open Houdini → Assets → Asset Manager
2. Under Operator Type Libraries → Current HIP File, find "Driver/deadline_cloud"
3. Right-click → Type Properties → Parameter tab
4. Changes update `DialogScript` in the HDA source files

## Key Commands Reference

| Command | Description |
|---------|-------------|
| `hatch build` | Build wheel and sdist to `dist/` |
| `hatch run test` | Run unit tests with coverage |
| `hatch run all:test` | Run unit tests across Python 3.9-3.13 |
| `hatch run integ:test` | Run all integration tests |
| `hatch run integ:test_submitters` | Run submitter integration tests only |
| `hatch run integ:test_adaptors` | Run adaptor integration tests only |
| `hatch run lint` | Run ruff, black --check, mypy |
| `hatch run fmt` | Auto-format code with black |
| `hatch run install --houdini-version X.Y` | Install dev submitter for Houdini version |
| `hatch run installer:build-installer` | Build standalone installer |
| `hatch run test-installer` | Run installer tests |
| `hatch shell` | Enter hatch shell environment |
| `hatch env prune` | Delete all hatch environments |
