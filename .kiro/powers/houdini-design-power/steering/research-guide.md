# Houdini API Research Guide

## How to Research Houdini APIs

### 1. SideFX Documentation
The primary reference for Houdini's Python API:
- **HOM Reference:** https://www.sidefx.com/docs/houdini/hom/hou/
- **HOM Cookbook:** https://www.sidefx.com/docs/houdini/hom/cb/

### 2. Houdini Python Shell
Test API calls interactively:
1. Open Houdini
2. Windows → Python Shell
3. Type Python code directly

### 3. hython (Headless)
Test without the GUI:
```bash
hython -c "import hou; hou.hipFile.load('/path/to/scene.hip'); print(hou.node('/out').children())"
```

## Common API Patterns in This Project

### Reading Node Parameters
```python
import hou

node = hou.node("/out/deadline_cloud1")
job_name = node.parm("job_name").eval()           # String value
frame_start = node.parm("f1").eval()               # Float/int value
use_custom = node.parm("use_custom_range").eval()  # Toggle (0 or 1)
```

### Finding Connected Nodes
```python
# Get the render node connected to the Deadline Cloud ROP
node = hou.node("/out/deadline_cloud1")
inputs = node.inputs()
if inputs:
    render_node = inputs[0]
    print(render_node.type().name())  # e.g., "ifd" (Mantra), "usdrender" (Karma)
```

### Scanning for File References
```python
# Find all file references in a node
node = hou.node("/obj/geo1")
refs = node.references()  # Returns referenced nodes

# Find file parameters
for parm in node.parms():
    template = parm.parmTemplate()
    if template.type() == hou.parmTemplateType.String:
        if template.stringType() == hou.stringParmType.FileReference:
            value = parm.eval()
            if value:
                print(f"{parm.name()}: {value}")
```

### Working with HIP File
```python
import hou

# Get current HIP file path
hip_path = hou.hipFile.path()

# Get HIP file variables
hip_dir = hou.text.expandString("$HIP")
job_dir = hou.text.expandString("$JOB")

# Save HIP file
hou.hipFile.save()
```

### Resolving Houdini Path Variables
```python
import hou

# Expand variables like $HIP, $JOB, $HOUDINI_TEMP_DIR
resolved = hou.text.expandString("$HIP/textures/wood.exr")

# Expand channel references
value = hou.text.expandString('`chs("/obj/geo1/file1/file")`')
```

### Working with Render Nodes
```python
import hou

# Get all render nodes
out_net = hou.node("/out")
for child in out_net.children():
    print(f"{child.name()} ({child.type().name()})")

# Common render node types:
# "ifd"        → Mantra
# "usdrender"  → Karma/Husk
# "rop_geometry" → Geometry ROP
# "rop_alembic"  → Alembic ROP
```

### Frame Range
```python
import hou

# Get global frame range
start, end, step = hou.playbar.frameRange()

# Get render node frame range
node = hou.node("/out/mantra1")
f1 = node.parm("f1").eval()  # Start frame
f2 = node.parm("f2").eval()  # End frame
f3 = node.parm("f3").eval()  # Frame step
```

## USD-Specific APIs

### USD Scene Inspection
```python
import hou

# Load a USD stage
stage_node = hou.node("/stage/usdimport1")

# Get the USD stage
stage = stage_node.stage()

# Traverse prims
for prim in stage.Traverse():
    print(prim.GetPath())

# Find references
for prim in stage.Traverse():
    refs = prim.GetReferences()
    for ref in refs.GetAddedOrExplicitItems():
        if ref.assetPath:
            print(f"Reference: {ref.assetPath}")
```

## Key `hou` Module Classes

| Class | Purpose | Docs |
|-------|---------|------|
| `hou.node` | Access nodes in the scene | https://www.sidefx.com/docs/houdini/hom/hou/node_.html |
| `hou.Parm` | Read/write parameter values | https://www.sidefx.com/docs/houdini/hom/hou/Parm.html |
| `hou.hipFile` | HIP file operations | https://www.sidefx.com/docs/houdini/hom/hou/hipFile.html |
| `hou.text` | String expansion, variable resolution | https://www.sidefx.com/docs/houdini/hom/hou/text.html |
| `hou.NodeType` | Node type information | https://www.sidefx.com/docs/houdini/hom/hou/NodeType.html |
| `hou.playbar` | Timeline/frame range | https://www.sidefx.com/docs/houdini/hom/hou/playbar.html |

## Research Checklist

When researching a Houdini API for a design:

1. Find the relevant `hou` class in the SideFX docs
2. Test the API call in Houdini's Python Shell
3. Check if the API works in hython (headless) — some APIs require a GUI
4. Check version compatibility (does it work in Houdini 19.5?)
5. Look for existing usage in the codebase: `grep -r "hou.something" src/`
6. Document the API call, its return type, and any edge cases
