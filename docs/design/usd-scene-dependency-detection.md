# USD Scene Dependency Detection

## Problem

The submitter uses `hou.fileReferences()` to discover files needed for a render job. This API returns file paths stored in Houdini node parameters. It does not look inside those files.

When a USD file is loaded into Houdini through a LOP node (e.g., Sublayer), `hou.fileReferences()` reports the top-level `.usd` file. It does not discover files referenced within that USD file — sublayers, referenced models, textures, or other assets.

The worker receives an incomplete set of files. The render fails.

```
scene.usda                          ← detected by hou.fileReferences()
├── sublayer: model.usda            ← NOT detected
│   └── asset: textures/wood.exr   ← NOT detected
└── sublayer: lighting.usda         ← NOT detected
```

References:
- [#319](https://github.com/aws-deadline/deadline-cloud-for-houdini/issues/319)
- [#318](https://github.com/aws-deadline/deadline-cloud-for-houdini/issues/318)

## Background

### How Asset Detection Works

The submitter's asset detection pipeline lives in `_assets.py`. The function `_get_scene_asset_references()` runs when the user clicks "Parse Files" or submits a job. It:

1. Calls `hou.fileReferences()` to enumerate all file paths in node parameters.
2. Filters out internal references (`opdef:`, `oplib:`, etc.).
3. Classifies each path as an input file or input directory.
4. Walks the ROP network to detect output directories via `_NODE_DIR_MAP`.
5. Merges auto-detected references with manually-added references.

### What is USD?

USD (Universal Scene Description) is a scene description framework. A single `.usd` file can reference other files through composition arcs:

- **Sublayers** — stacks another USD layer on top.
- **References** — pulls in a prim (object) from another file.
- **Payloads** — like references, but lazily loaded.
- **Asset paths** — direct file paths to textures, volumes, etc.

These form a dependency graph that can be arbitrarily deep. All files in the graph are needed to render the scene.

## Proposed Change

Add a USD dependency traversal step to the existing asset detection pipeline. During the `hou.fileReferences()` loop, collect any files with USD extensions (`.usd`, `.usda`, `.usdc`, `.usdz`). After the loop, traverse each USD file's composition arc to discover all nested dependencies. Merge the results into the existing `AssetReferences` object.

```
Before:  hou.fileReferences() → filter → classify → output dirs → merge
After:   hou.fileReferences() → filter → classify → USD traverse → output dirs → merge
```

No changes to the adaptor, job template, or submission flow. The user's workflow does not change.

### New Functions

**`_get_usd_output_directories(usd_path)`** — Opens a USD stage and returns output directories from RenderProduct prims at `/Render/Products`. Mirrors the existing `_husk_outputs` pattern.

**`_get_usd_asset_references(usd_file_paths)`** — Takes a set of resolved USD file paths. For each file, calls `UsdUtils.ComputeAllDependencies()` to recursively discover all layers and assets. Also calls `_get_usd_output_directories()` to find render product output paths. Returns three sets: input files, output directories, and unresolved paths.

### Modified Function

**`_get_scene_asset_references(rop_node)`** — The existing `hou.fileReferences()` loop gains a check: if a file has a USD extension, its resolved absolute path (via `parm.evalAsString()`) is collected for traversal. After the loop, if any USD files were found, `_get_usd_asset_references()` is called and results are merged into the `AssetReferences` object. Unresolved paths are logged as warnings.

### Unchanged Components

| Component | Why No Change |
|---|---|
| `submitter.py` | Consumes `AssetReferences` — the object shape doesn't change. |
| `_parse_files()` | Calls `_get_scene_asset_references()` — the merge logic works on the same sets. |
| `_NODE_DIR_MAP` / `_husk_outputs` | Still handles output detection for the live LOP stage. USD traversal supplements it. |
| `HoudiniAdaptor` / `HoudiniHandler` | Run on the worker. No change — they just need the files to be present. |

## Key Design Decisions

### Why `UsdUtils.ComputeAllDependencies`?

This is the USD SDK's built-in function for this purpose. It handles all composition arcs (sublayers, references, payloads, inherits, specializes), resolves asset paths recursively, handles circular references, and returns unresolved paths separately. The same function is used in the [`generate_usd_job.py`](https://github.com/aws-deadline/deadline-cloud-samples/tree/mainline/job_bundles/houdini_husk_usd_render) sample script.

### Why collect USD files during the existing loop?

We identify USD files inside the `hou.fileReferences()` loop using the evaluated path. This avoids a second iteration over `hou.fileReferences()` and ensures we only traverse USD files that are actually referenced by the scene.

### Why use evaluated paths instead of unexpanded refs?

`hou.fileReferences()` returns unexpanded strings (e.g., `$HIP/scene.usda`). `UsdUtils.ComputeAllDependencies` needs a real filesystem path. We use `parm.evalAsString()` to resolve Houdini variables before passing to the USD API.

### Why not a separate UI action?

The USD traversal runs as part of the existing "Parse Files" flow. The user's workflow doesn't change. Zero friction.

## Tests

### Unit Tests

Five tests in `test_assets.py` covering:
- Layer and asset collection from `ComputeAllDependencies` results.
- Separation of resolved assets and unresolved paths.
- Deduplication of shared dependencies across multiple USD files.
- RenderProduct output directory extraction (single, multiple, and no products).
- USD file presence triggers traversal; non-USD scenes skip it entirely.

### Integration Test

One test in `test_houdini_submitters.py` that creates a USD scene exercising sublayers, references, payloads, and texture assets. Loads it in Houdini via a LOP Sublayer node, runs the full submitter pipeline, and asserts all dependencies appear in the generated `asset_references.yaml`.
