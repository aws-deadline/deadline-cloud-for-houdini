# Development Guide

## Architecture Overview

deadline-cloud-for-houdini has two modules:

1. **Submitter** (`src/deadline/houdini_submitter/`) — Houdini plugin loaded via the Houdini package system. Provides the Deadline Cloud ROP node for submitting render jobs.

2. **Adaptor** (`src/deadline/houdini_adaptor/`) — pip-installable Python package. Provides the `houdini-openjd` CLI for executing Houdini renders on the farm via the OpenJD adaptor runtime.

## Namespace Packages

This project uses Python namespace packages. The `deadline` package at `src/deadline/` has no `__init__.py`. Both `houdini_submitter` and `houdini_adaptor` are sub-packages under this namespace.

The `hatch_custom_hook.py` build hook copies `_version.py` to three locations:
- `src/deadline/houdini_adaptor/`
- `src/deadline/houdini_submitter/`
- `src/deadline/houdini_submitter/python/deadline_cloud_for_houdini/`

## Submitter Code Layout

The submitter is NOT a standard pip package. It is loaded by Houdini's package system.

```
src/deadline/houdini_submitter/
├── python/deadline_cloud_for_houdini/    # Main Python code
│   ├── submitter.py          # Core: job template generation, submission callbacks
│   ├── _assets.py            # Asset detection: finds file dependencies in HIP scenes
│   ├── queue_parameters.py   # Queue parameter handling for Deadline Cloud
│   ├── houdini_submitter_widget.py  # Qt widget for the submitter panel
│   ├── hip_settings.py       # HIP file settings management
│   ├── constants.py          # Shared constants
│   └── adaptor_override_environment.yaml  # Adaptor override config
├── otls/deadline_cloud.hda/  # Houdini Digital Asset (the ROP node)
├── panel/submitter_panel.py  # Houdini panel registration
└── soho/deadline_cloud_soho.py  # SOHO render output integration
```

### Key Submitter Files

**`submitter.py`** (29KB) — The core of the submitter. Contains:
- Job template generation from ROP node parameters
- Submit and save-bundle callbacks
- Render node detection and configuration
- Step/task parameter mapping

**`_assets.py`** (17KB) — Asset detection:
- Scans HIP scene for file references
- Handles Houdini-specific path variables (`$HIP`, `$JOB`)
- Detects USD scene dependencies
- Returns asset list for job attachments

**`queue_parameters.py`** (15KB) — Queue parameter handling:
- Reads queue parameters from Deadline Cloud API
- Maps parameters to Houdini ROP node parameters
- Handles parameter updates and validation

## Adaptor Code Layout

The adaptor is a standard pip-installable package with CLI entry points.

```
src/deadline/houdini_adaptor/
├── HoudiniAdaptor/
│   ├── adaptor.py        # Main adaptor: OpenJD lifecycle implementation
│   ├── __main__.py       # CLI entry point for houdini-openjd
│   ├── __init__.py       # Exports main() function
│   └── schemas/          # JSON schemas
│       ├── init_data.schema.json
│       └── run_data.schema.json
└── HoudiniClient/
    ├── houdini_client.py   # Client that runs inside the Houdini process
    ├── houdini_handler.py  # Handles commands sent from the adaptor
    └── __init__.py
```

### Adaptor Lifecycle

The adaptor implements the OpenJD adaptor runtime lifecycle:

1. **`on_start`** — Launches hython, loads the HIP file, configures render settings
2. **`on_run`** — Executes a render for a specific frame/task
3. **`on_stop`** — Cleans up after rendering
4. **`on_cleanup`** — Final cleanup, terminates hython process

The adaptor communicates with the HoudiniClient running inside hython via a socket connection.

### CLI Entry Points

Defined in `pyproject.toml`:
```
houdini-openjd = "deadline.houdini_adaptor.HoudiniAdaptor:main"
HoudiniAdaptor = "deadline.houdini_adaptor.HoudiniAdaptor:main"  # deprecated
```

## Dependencies

From `pyproject.toml`:
- `deadline` — AWS Deadline Cloud client library
- `openjd-adaptor-runtime` — OpenJD adaptor runtime

See `pyproject.toml` under `[project] dependencies` for current version constraints.

## Code Style

- **Line length:** 100 (ruff and black)
- **Linter:** ruff with isort (known-first-party: `deadline`, `openjd`)
- **Formatter:** black
- **Type checker:** mypy (check_untyped_defs, namespace_packages)
- **Copyright headers:** All source files must have Apache-2.0 headers (enforced by `test_copyright_headers.py`)

## Branching Strategy

- `mainline` — Active development. May break APIs at any time.
- `release` — Official release for consumers. Breaking changes increment the interface version.

## Versioning

- Semantic versioning via `python-semantic-release`
- Version source: git tags via `hatch-vcs`
- Version scheme: `post-release` (e.g., `0.7.7.post67+gb392ccb47`)
- Conventional commits determine version bumps

## Adding New Features

### Submitter Feature
1. Modify code in `src/deadline/houdini_submitter/python/deadline_cloud_for_houdini/`
2. If adding parameters, update the HDA DialogScript
3. Add unit tests in `test/unit/deadline_submitter_for_houdini/`
4. Add integration test scene in `test/integ/test_scripts/` if needed
5. Run `hatch run test` and `hatch run lint`

### Adaptor Feature
1. Modify code in `src/deadline/houdini_adaptor/`
2. Update JSON schemas if init_data or run_data changes
3. Add unit tests in `test/unit/deadline_adaptor_for_houdini/`
4. Run `hatch run test` and `hatch run lint`
