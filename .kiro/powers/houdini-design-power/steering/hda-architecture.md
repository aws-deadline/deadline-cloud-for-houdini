# HDA Architecture

## What is the HDA?

The Deadline Cloud ROP node is implemented as a Houdini Digital Asset (HDA). It lives at:
```
src/deadline/houdini_submitter/otls/deadline_cloud.hda/Driver_1deadline__cloud/
```

The HDA defines the node's parameter interface, callbacks, icon, and shelf tool. It is the primary UI surface for the submitter.

## HDA File Structure

```
otls/deadline_cloud.hda/
├── Sections.list                    # Top-level section index
├── INDEX__SECTION                   # HDA index metadata
├── houdini.hdalibrary               # Library marker (empty)
└── Driver_1deadline__cloud/         # The ROP node type definition
    ├── DialogScript                 # Parameter interface (27KB)
    ├── PythonModule                 # Python callback code
    ├── CreateScript                 # Node creation script
    ├── OnCreated                    # Post-creation initialization
    ├── IconSVG                      # Node icon (SVG)
    ├── Tools.shelf                  # Shelf tool definition
    ├── ExtraFileOptions             # File option metadata
    ├── TypePropertiesOptions        # Type property settings
    └── Sections.list                # Section index for this type
```

## Key Files

### DialogScript (27KB)
The largest and most important file. Defines every parameter the user sees on the ROP node:
- Job name, description, priority
- Frame range settings
- Queue and farm selection
- Job attachments configuration
- Adaptor settings (Include Adaptor Wheels, etc.)
- Render node connection settings

The DialogScript uses Houdini's parameter definition language. Each parameter has a name, label, type, default value, and optional callback.

### PythonModule
Contains Python callback functions referenced by the DialogScript. Currently minimal — most logic lives in `submitter.py`.

The PythonModule imports from `deadline_cloud_for_houdini` and delegates to the submitter module.

### CreateScript
Runs when the node type is first created. Sets up the initial node state.

### OnCreated
Runs after a node instance is created. Initializes default parameter values and connections.

## How the HDA Loads

1. Houdini scans `hpath` directories for `.hda` files
2. The package JSON sets `hpath` to `$DEADLINE_CLOUD_FOR_HOUDINI`
3. Houdini finds `otls/deadline_cloud.hda/` and registers the `Driver/deadline_cloud` type
4. The node becomes available in the `/out` network TAB menu

## Editing the HDA

### Via Houdini UI (Recommended)
1. Open Houdini
2. Go to Assets → Asset Manager
3. Under Operator Type Libraries → Current HIP File, find "Driver/deadline_cloud"
4. Right-click → Type Properties
5. **Parameter tab** — Edit the parameter interface (adds/removes/reorders parameters)
6. Click Apply — the `DialogScript` file updates automatically
7. The changes are written to the HDA source files in the repo

### Via Direct File Edit (Advanced)
You can edit `DialogScript` directly, but this is error-prone. The Houdini UI is the recommended approach because it validates the parameter definitions.

Files you might edit directly:
- `PythonModule` — Add or modify callback functions
- `OnCreated` — Change post-creation initialization
- `CreateScript` — Change type creation behavior

## Parameter Interface Design

### Parameter Naming Convention
- Use lowercase with underscores: `job_name`, `frame_range`, `queue_id`
- Prefix related parameters: `attachment_*`, `adaptor_*`

### Parameter Types
Common types used in the DialogScript:
- `string` — Text input
- `int` — Integer input
- `toggle` — Boolean checkbox
- `button` — Action button (triggers callback)
- `menu` — Dropdown selection
- `file` — File path with browser

### Callbacks
Parameters can trigger Python callbacks defined in PythonModule:
```
callback = "hou.phm().my_callback(kwargs)"
```

The `kwargs` dict contains:
- `node` — The ROP node instance
- `parm` — The parameter that triggered the callback
- `script_value` — The new parameter value

## Design Considerations

### Adding Parameters
When adding a new parameter to the HDA:
1. Consider the parameter's position in the UI (group it with related parameters)
2. Set a sensible default value
3. Add help text (tooltip)
4. If the parameter affects submission, update `submitter.py` to read it
5. If the parameter needs validation, add a callback
6. Add unit tests that mock the parameter value

### Backward Compatibility
- Never remove parameters without a deprecation period
- Renamed parameters need migration logic in `OnCreated` or `CreateScript`
- New parameters must have defaults that preserve existing behavior

### Performance
- Avoid expensive operations in parameter callbacks (they run on every change)
- Defer API calls (like fetching queue parameters) to explicit user actions (button clicks)
- Cache results where possible
