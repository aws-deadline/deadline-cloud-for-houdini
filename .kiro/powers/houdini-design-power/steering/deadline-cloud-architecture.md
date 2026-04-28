# Deadline Cloud Architecture for Houdini

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                    User's Workstation                    │
│                                                         │
│  ┌─────────┐    ┌──────────────────┐    ┌───────────┐  │
│  │ Houdini │───▶│ Submitter Plugin │───▶│ Deadline   │  │
│  │  (GUI)  │    │  (ROP Node/HDA)  │    │ Cloud API  │  │
│  └─────────┘    └──────────────────┘    └─────┬─────┘  │
│                                               │         │
└───────────────────────────────────────────────┼─────────┘
                                                │
                                    ┌───────────▼──────────┐
                                    │   AWS Deadline Cloud  │
                                    │   (Job Scheduling)    │
                                    └───────────┬──────────┘
                                                │
┌───────────────────────────────────────────────┼─────────┐
│                   Render Farm                  │         │
│                                               │         │
│  ┌──────────────┐    ┌─────────────────────┐  │         │
│  │   Adaptor     │───▶│     hython          │  │         │
│  │ (houdini-openjd)   │  (HoudiniClient)    │  │         │
│  └──────────────┘    └─────────────────────┘  │         │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## Components

### 1. Submitter Plugin (Workstation)

**What it does:** Provides a Deadline Cloud ROP node in Houdini. Users connect it to a render node, configure job settings, and submit or save a job bundle.

**How it loads:** Via Houdini's package system. A JSON file at `~/houdini{X.Y}/packages/` sets `PYTHONPATH` and `hpath` to point at the plugin source.

**Key flow:**
1. User creates a Deadline Cloud ROP node in `/out` network
2. Connects a render node (Mantra, Karma, etc.) to the Deadline Cloud node
3. Configures job name, frame range, queue, and other settings
4. Clicks "Submit" or "Save Bundle"
5. Submitter generates an OpenJD job template and uploads assets

### 2. Adaptor (Render Farm)

**What it does:** Runs on render farm workers. Receives job data from Deadline Cloud, launches hython, loads the HIP file, and executes renders.

**How it runs:** As the `houdini-openjd` CLI command, invoked by the Deadline Cloud worker agent.

**Key flow:**
1. Worker agent calls `houdini-openjd` with init_data (HIP file, render node)
2. `on_start`: Launches hython, loads HIP file, configures render settings
3. `on_run`: Renders a specific frame (called once per task/frame)
4. `on_stop`: Cleans up after the render batch
5. `on_cleanup`: Terminates hython process

### 3. HoudiniClient (Inside hython)

**What it does:** Runs inside the hython process. Receives commands from the adaptor via a socket connection and executes them using the `hou` API.

**Key flow:**
1. Adaptor starts hython with the HoudiniClient script
2. Client connects to the adaptor's socket server
3. Adaptor sends commands (load scene, set parameters, render)
4. Client executes commands and returns results

## Data Flow

### Submission
```
ROP Node Parameters → submitter.py → OpenJD Job Template → Deadline Cloud API
                      _assets.py   → Job Attachments (files to upload)
```

### Rendering
```
Deadline Cloud → Worker Agent → houdini-openjd (adaptor)
                                    ↓
                              adaptor.py (on_start)
                                    ↓
                              hython + HoudiniClient
                                    ↓
                              houdini_handler.py (execute render)
                                    ↓
                              Render output files
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `deadline` | Deadline Cloud client library, job attachments, GUI |
| `openjd-adaptor-runtime` | OpenJD adaptor lifecycle, socket communication |

Version constraints are defined in `pyproject.toml` under `[project] dependencies`.

## Supported Houdini Versions

| Houdini | Python | Status |
|---------|--------|--------|
| 19.5 | 3.9 | Supported |
| 20.0 | 3.10 | Supported |
| 20.5 | 3.11 | Supported |
| 21.0 | 3.11 | Supported |

## Supported Renderers

The submitter works with any Houdini render node connected to the Deadline Cloud ROP. Common renderers:
- **Mantra** — Houdini's built-in renderer
- **Karma** — Houdini's USD-based renderer (via Husk)
- **Third-party** — Any renderer with a Houdini ROP node

The adaptor does not need renderer-specific code. It delegates rendering to hython, which uses whatever render node is configured in the HIP file.
