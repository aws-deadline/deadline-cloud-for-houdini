# Submitter Architecture

## Overview

The submitter is a Houdini plugin that provides a Deadline Cloud ROP (Render Output) node. It generates OpenJD job templates from Houdini scene data and submits them to AWS Deadline Cloud.

## Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Houdini Session                           │
│                                                             │
│  ┌──────────────┐     ┌──────────────────────────────────┐  │
│  │  ROP Network  │     │  Deadline Cloud ROP Node (HDA)   │  │
│  │  (/out)       │────▶│                                  │  │
│  │               │     │  DialogScript → Parameter UI     │  │
│  │  Render Node  │     │  PythonModule → Callbacks        │  │
│  │  (Mantra,     │     │  OnCreated   → Initialization    │  │
│  │   Karma, etc) │     └──────────┬───────────────────────┘  │
│  └──────────────┘                 │                          │
│                                   ▼                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │              submitter.py                               │  │
│  │  - Read ROP parameters                                  │  │
│  │  - Detect connected render node                         │  │
│  │  - Generate OpenJD job template                         │  │
│  │  - Submit to Deadline Cloud API                         │  │
│  └────────────┬───────────────────┬───────────────────────┘  │
│               │                   │                          │
│               ▼                   ▼                          │
│  ┌────────────────────┐  ┌────────────────────────────────┐  │
│  │    _assets.py       │  │   queue_parameters.py          │  │
│  │  - Scan HIP scene   │  │  - Fetch queue params from API │  │
│  │  - Find file refs   │  │  - Map to ROP parameters       │  │
│  │  - Resolve $HIP etc │  │  - Handle param updates        │  │
│  │  - USD dependencies │  └────────────────────────────────┘  │
│  └────────────────────┘                                      │
│                                                             │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  houdini_submitter_widget.py                            │  │
│  │  - Qt widget wrapping Deadline Cloud submitter UI       │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow: Submit Job

1. User clicks "Submit" on the Deadline Cloud ROP node
2. `PythonModule` callback triggers `submitter.py`
3. `submitter.py` reads all ROP node parameters via `hou.node().parm()`
4. `submitter.py` detects the connected render node and its settings
5. `_assets.py` scans the HIP scene for file dependencies
6. `submitter.py` generates an OpenJD job template with steps and tasks
7. The `deadline` library uploads job attachments and submits the job
8. User sees confirmation with job ID

## Data Flow: Save Bundle

Same as Submit, but step 7 writes the job template and asset manifest to disk instead of submitting.

## Key Data Structures

### Job Template
The submitter generates an OpenJD job template with:
- **Job name** — From the ROP node parameter
- **Steps** — One step per render node (or per frame range segment)
- **Tasks** — One task per frame
- **Environments** — Adaptor configuration, Houdini version
- **Attachments** — HIP file and all detected dependencies

### Asset List
`_assets.py` returns a list of file paths that the HIP scene depends on:
- Texture files
- Geometry caches (`.bgeo`, `.abc`)
- USD references and payloads
- Simulation caches
- HDRI environment maps

## Plugin Loading

The submitter loads via Houdini's package system:

1. Houdini reads JSON files from `~/houdini{X.Y}/packages/`
2. `deadline_submitter_for_houdini.json` sets:
   - `DEADLINE_CLOUD_FOR_HOUDINI` → submitter source path
   - `PYTHONPATH` → Python code + dependencies
   - `hpath` → scanned for HDAs, panels, SOHO scripts
3. Houdini loads the HDA from `otls/deadline_cloud.hda/`
4. The Deadline Cloud ROP node becomes available in the `/out` network

## File Responsibilities

| File | Responsibility |
|------|---------------|
| `submitter.py` | Core logic: template generation, submit/save callbacks, render node detection |
| `_assets.py` | Asset detection: file scanning, path resolution, USD dependency detection |
| `queue_parameters.py` | Queue parameter fetch, mapping, and validation |
| `houdini_submitter_widget.py` | Qt widget integration with Houdini's UI |
| `hip_settings.py` | HIP file settings (save behavior, file path) |
| `constants.py` | Shared constants (version strings, default values) |
| `adaptor_override_environment.yaml` | Adaptor environment override configuration |
| `submitter_panel.py` | Houdini panel registration |
| `deadline_cloud_soho.py` | SOHO render output integration |

## Extension Points

### Adding a New Parameter
1. Add to HDA DialogScript (via Houdini Type Properties UI)
2. Read in `submitter.py` via `hou.node().parm("param_name").eval()`
3. Map to job template field

### Supporting a New File Type in Asset Detection
1. Add detection logic to `_assets.py`
2. Handle Houdini path variables (`$HIP`, `$JOB`, `$HOUDINI_TEMP_DIR`)
3. Add unit test with mock scene data

### Modifying Queue Parameter Behavior
1. Update `queue_parameters.py`
2. Ensure backward compatibility with existing queue configurations
