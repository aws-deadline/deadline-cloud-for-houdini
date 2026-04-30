# Submitter Development Guide

## Overview

The Houdini submitter is a plugin that provides a Deadline Cloud ROP (Render Output) node. Users connect it to a render node in Houdini's `/out` network to submit render jobs to AWS Deadline Cloud.

The submitter is NOT pip-installable. It is loaded by Houdini's package system via a JSON file that sets environment variables pointing to the source code.

## Source Layout

```
src/deadline/houdini_submitter/
├── python/deadline_cloud_for_houdini/
│   ├── submitter.py                  # Core submission logic (29KB)
│   ├── _assets.py                    # Asset/file dependency detection (17KB)
│   ├── queue_parameters.py           # Queue parameter handling (15KB)
│   ├── houdini_submitter_widget.py   # Qt widget for submitter panel
│   ├── hip_settings.py               # HIP file settings
│   ├── constants.py                  # Shared constants
│   └── adaptor_override_environment.yaml
├── otls/deadline_cloud.hda/          # Houdini Digital Asset
│   └── Driver_1deadline__cloud/      # HDA definition
│       ├── DialogScript              # Parameter interface definition
│       ├── PythonModule              # Python callbacks
│       ├── CreateScript              # Node creation script
│       ├── OnCreated                 # Post-creation hook
│       ├── IconSVG                   # Node icon
│       └── Tools.shelf              # Shelf tool definition
├── panel/submitter_panel.py          # Panel registration
└── soho/deadline_cloud_soho.py       # SOHO integration
```

## Key Files

### `submitter.py`
The core of the submitter. Handles:
- **Job template generation** — Converts ROP node parameters into an OpenJD job template
- **Submit callback** — Called when the user clicks "Submit" in the UI
- **Save bundle callback** — Called when the user clicks "Save Bundle"
- **Render node detection** — Finds connected render nodes and their settings
- **Step/task mapping** — Maps Houdini frame ranges to OpenJD steps and tasks

### `_assets.py`
Asset detection for job attachments:
- Scans the HIP scene for file references using Houdini's `hou` API
- Resolves Houdini path variables (`$HIP`, `$JOB`, `$HOUDINI_TEMP_DIR`)
- Detects USD scene dependencies (references, payloads, sublayers)
- Returns a list of files to upload as job attachments

### `queue_parameters.py`
Queue parameter handling:
- Fetches queue parameters from the Deadline Cloud API
- Maps Deadline Cloud parameters to Houdini ROP node parameters
- Handles parameter updates when the user changes the target queue

### `houdini_submitter_widget.py`
Qt widget that wraps the Deadline Cloud submitter UI. Integrates with Houdini's Qt environment.

## Houdini Package System

The submitter is loaded via a JSON package file. The `hatch run install --houdini-version X.Y` command creates this file at:

- **Linux:** `~/houdini{X.Y}/packages/deadline_submitter_for_houdini.json`
- **macOS:** `~/Library/Preferences/houdini/{X.Y}/packages/deadline_submitter_for_houdini.json`
- **Windows:** `%USERPROFILE%\Documents\houdini{X.Y}\packages\deadline_submitter_for_houdini.json`

The JSON structure:
```json
{
    "env": [
        {"DEADLINE_CLOUD_FOR_HOUDINI": "/path/to/src/deadline/houdini_submitter"},
        {"PYTHONPATH": "/path/to/python:/path/to/src:/path/to/plugin_env_X.Y"}
    ],
    "hpath": "$DEADLINE_CLOUD_FOR_HOUDINI"
}
```

- `DEADLINE_CLOUD_FOR_HOUDINI` — Points to the submitter source directory
- `PYTHONPATH` — Adds the Python code, source root, and plugin dependencies
- `hpath` — Tells Houdini to scan this directory for HDAs, panels, SOHO scripts, etc.

## HDA (Houdini Digital Asset)

The Deadline Cloud ROP node is defined as an HDA at `otls/deadline_cloud.hda/Driver_1deadline__cloud/`.

Key HDA files:
- **DialogScript** — Defines the parameter interface (all the UI fields the user sees)
- **PythonModule** — Python callbacks triggered by parameter changes and button clicks
- **CreateScript** — Runs when the node is first created
- **OnCreated** — Post-creation initialization

To edit the HDA:
1. Open Houdini
2. Assets → Asset Manager
3. Under Operator Type Libraries → Current HIP File, find "Driver/deadline_cloud"
4. Right-click → Type Properties
5. Edit parameters in the Parameter tab
6. Click Apply — the DialogScript file updates automatically

## Adding a New Submitter Parameter

1. Edit the HDA DialogScript (via Houdini's Type Properties UI)
2. Add the parameter read logic in `submitter.py`
3. If the parameter affects job template generation, update the template logic
4. If the parameter affects asset detection, update `_assets.py`
5. Add unit tests for the new parameter
6. Add an integration test scene if the parameter changes submission behavior

## Testing the Submitter

### Unit Tests
```bash
hatch run test -- test/unit/deadline_submitter_for_houdini/ -v
```

Tests use `mock_hou.py` to mock the `hou` module. The mock provides:
- `hou.node()` — Returns mock nodes with parameter values
- `hou.hipFile` — Mock HIP file operations
- `hou.parm()` — Mock parameter access

### Integration Tests
```bash
hatch run integ:test_submitters
```

Requires `HYTHON_EXECUTABLE` and `HOUDINI_VERSION` environment variables.
