# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import fnmatch
import glob
import os
import re
from typing import Iterable, Optional

from deadline.client.job_bundle.submission import AssetReferences
from pxr import Usd, UsdUtils

import hou

# A frame number in the one row Parse Files writes for a frame sequence, such as $F4 or ${F4}.
_FRAME_SEQUENCE_TOKEN: str = r"(\$\{F\d+\}|\$F\d+)"
_IGNORE_REF_VALUES = ("opdef:", "oplib:", "temp:", "op:")
_IGNORE_REF_PARMS = (
    "taskgraphfile",
    "pdg_workingdir",
    "soho_program",
    "BakeView_img_file_path",
    "OutputDeepWriter_file",
    "SettingsOutput_img_file_path",
    "RS_outputFileNamePrefix",
    "savetodirectory_directory",
)

# The ROP toggle that skips output folders and keeps loaded caches (see its gate below).
_SKIP_OUTPUTS_KEEP_CACHES_PARM: str = "skip_outputs_keep_caches"
# The ROP's excluded input paths, one pattern per excluded_input_path# row.
_EXCLUDED_INPUT_PATHS_PARM: str = "excluded_input_paths"
# Patterns in this env var, separated by os.pathsep, are added to the node's excluded input paths.
EXCLUDED_INPUT_PATHS_ENV_VAR: str = "DEADLINE_HOUDINI_EXCLUDED_INPUT_PATHS"
# The SOP types with the File Cache's Load from Disk and read path parms, whose loaded cache files
# are added as inputs when the toggle is on.
_FILE_CACHE_TYPES: tuple[str, ...] = (
    "Sop/filecache",
    "Sop/filecache::2.0",
    "Sop/rbdio::2.0",
    "Sop/vellumio::2.0",
)


def _get_hip_file() -> str:
    return hou.hipFile.path()


def _houdini_time_vars_to_glob(path: str) -> str:
    """
    Given a path, replace any houdini time-based variables or expressions with globs
    to capture complete file sequences for job attachments. If there is no time-based
    parameterization, then the original string is returned.

    These are the useful timebased variables that can be included in files.
    ref: https://www.sidefx.com/docs/houdini/render/expressions.html
    """
    time_based_variable_patterns: tuple = (
        r"(`.*`)",  # an expression, like python!
        r"(\$FF)",  # current fractional frame number
        r"(\${FF})",
        r"(\$F\d*)",  # current frame number, only variable with digits that follow
        r"(\${F\d*})",
        r"(\$T)",  # current time
        r"(\${T})",
        r"(\$SF)",  # current simulation timestep number
        r"(\${SF})",
        r"(\$ST)",  # current simulation time
        r"(\${ST})",
    )
    time_var_pattern = "|".join(time_based_variable_patterns)

    return re.sub(time_var_pattern, "*", path)


def _get_evaluated_glob_path(parm, globbed_path: str) -> str:
    """Given a parm that contains a path, temporarily set it to a globbed path
    and perform an evaluation to remove all other variables, return the value
    and ensure that we put the original value back.

    Rationale: Houdini does not appear to provide a way to evaluate an arbitary
    string, only as a Parm. Thus, we can use this function to put a value into
    a parm to then evaluate.
    """
    orig_path = parm.unexpandedString()
    try:
        parm.set(globbed_path)
        return parm.eval()
    finally:
        parm.set(orig_path)


def _get_evaluated_asset_references(rop_node: hou.Node) -> AssetReferences:
    """
    Get the current paths stored in the parms backing the UI and return them as
    an AssetReferences object with any Houdini tokens evaluated
    """
    asset_references = AssetReferences()
    auto_rows: set[str] = {
        n.unexpandedString() for n in rop_node.parm("auto_input_filenames").multiParmInstances()
    }

    for n in rop_node.parm("input_filenames").multiParmInstances():
        filepath = n.unexpandedString()
        filepath_glob = _houdini_time_vars_to_glob(filepath)
        if filepath == filepath_glob:
            asset_references.input_filenames.add(n.eval())
            continue

        pieces: list[str] = re.split(_FRAME_SEQUENCE_TOKEN, filepath)
        text: str = "".join(pieces[::2])
        # A frame sequence row the scan wrote gets the files the Submit dialog gets. A row an
        # older version wrote is a ref with a variable such as $HIP, and keeps the glob below.
        if filepath in auto_rows and "$" not in text:
            frames: range = range(1, len(pieces), 2)
            asset_references.input_filenames.update(
                f
                for f in _find_frame_files(pieces, list(frames), [0] * len(frames))
                if os.path.isfile(f)
            )
            continue

        evaluated_glob = _get_evaluated_glob_path(parm=n, globbed_path=filepath_glob)
        # This is a work-around to avoid evaluating the offsets for
        # scene introspection. It's possible that this will capture
        # extra files since a glob will not enforce numbers, but
        # handles well-defined file path patterns
        for file in glob.iglob(evaluated_glob):
            asset_references.input_filenames.add(file)

    # Before we start properly evaluating directory paths, we should filter globbed
    # results by the pattern the variables expects to avoid an explosion of
    # of included input files
    asset_references.input_directories.update(
        [n.eval() for n in rop_node.parm("input_directories").multiParmInstances()]
    )
    asset_references.output_directories.update(
        [n.eval() for n in rop_node.parm("output_directories").multiParmInstances()]
    )

    return asset_references


def _get_unevaluated_asset_references(rop_node: hou.Node) -> AssetReferences:
    """
    Get the current paths stored in the parms backing the UI and return them as
    an AssetReferences object without evaluating any Houdini tokens
    """
    asset_references = AssetReferences()
    asset_references.input_filenames.update(
        [n.unexpandedString() for n in rop_node.parm("input_filenames").multiParmInstances()]
    )
    asset_references.input_directories.update(
        [n.unexpandedString() for n in rop_node.parm("input_directories").multiParmInstances()]
    )
    asset_references.output_directories.update(
        [n.unexpandedString() for n in rop_node.parm("output_directories").multiParmInstances()]
    )

    return asset_references


def _get_saved_auto_detected_asset_references(rop_node: hou.Node) -> AssetReferences:
    """
    Get all of the paths saved in the hidden auto_* parms on the node and return
    them as an AssetReferences object in their unevaluated forms.
    """
    saved_auto_refs = AssetReferences()
    saved_auto_refs.input_filenames.update(
        [n.unexpandedString() for n in rop_node.parm("auto_input_filenames").multiParmInstances()]
    )
    saved_auto_refs.input_directories.update(
        [n.unexpandedString() for n in rop_node.parm("auto_input_directories").multiParmInstances()]
    )
    saved_auto_refs.output_directories.update(
        [
            n.unexpandedString()
            for n in rop_node.parm("auto_output_directories").multiParmInstances()
        ]
    )

    return saved_auto_refs


def _parse_files(node: hou.Node):
    """
    Generate the lists of input filenames, input directories and output directories
    based on the detected paths in the scene, any previously saved values and the
    current values in the UI. Then update the UI with the new lists of paths.
    """
    display_asset_refs = _get_unevaluated_asset_references(node)
    # One row per frame sequence, which Save Bundle globs, keeps the node's lists short.
    auto_asset_refs = _get_scene_asset_references(node, expand_frame_sequences=False)
    prev_auto_asset_refs = _get_saved_auto_detected_asset_references(node)

    # Remove all previous and current auto detected assets from the lists currently
    # displayed in the UI to determine which ones have been manually added
    display_asset_refs.input_filenames.difference_update(auto_asset_refs.input_filenames)
    display_asset_refs.input_filenames.difference_update(prev_auto_asset_refs.input_filenames)
    display_asset_refs.input_directories.difference_update(auto_asset_refs.input_directories)
    display_asset_refs.input_directories.difference_update(prev_auto_asset_refs.input_directories)
    display_asset_refs.output_directories.difference_update(auto_asset_refs.output_directories)
    display_asset_refs.output_directories.difference_update(prev_auto_asset_refs.output_directories)

    manual_input_filenames = sorted(list(display_asset_refs.input_filenames))
    manual_input_directories = sorted(list(display_asset_refs.input_directories))
    manual_output_directories = sorted(list(display_asset_refs.output_directories))

    auto_detected_input_filenames = sorted(list(auto_asset_refs.input_filenames))
    auto_detected_input_directories = sorted(list(auto_asset_refs.input_directories))
    auto_detected_output_directories = sorted(list(auto_asset_refs.output_directories))

    new_display_input_filenames = manual_input_filenames + auto_detected_input_filenames
    new_display_input_directories = manual_input_directories + auto_detected_input_directories
    new_display_output_directories = manual_output_directories + auto_detected_output_directories

    _update_paths_parm(node, "input_filenames", new_display_input_filenames)
    _update_paths_parm(node, "input_directories", new_display_input_directories)
    _update_paths_parm(node, "output_directories", new_display_output_directories)
    _update_paths_parm(node, "auto_input_filenames", auto_detected_input_filenames)
    _update_paths_parm(node, "auto_input_directories", auto_detected_input_directories)
    _update_paths_parm(node, "auto_output_directories", auto_detected_output_directories)


def _update_paths_parm(node: hou.Node, parm_name: str, paths: list[str]):
    """
    Clear the existing path multiparm parm_name and rebuild it based on the list
    of paths passed in.
    """
    p = node.parm(parm_name)
    while p.multiParmInstancesCount():
        p.removeMultiParmInstance(0)
    p.set(len(paths))
    for i, n in enumerate(p.multiParmInstances()):
        n.set(paths[i])


_USD_EXTENSIONS: set[str] = {".usd", ".usda", ".usdc", ".usdz"}


def _get_usd_output_directories(usd_path: str) -> set[str]:
    """
    Open a USD stage and return output directories from RenderProduct prims
    at /Render/Products.
    """
    output_directories: set[str] = set()
    stage: Usd.Stage = Usd.Stage.Open(usd_path)
    if not stage:
        return output_directories
    products: Usd.Prim = stage.GetPrimAtPath("/Render/Products")
    if not products.IsValid():
        return output_directories
    for child in products.GetChildren():
        if child.GetTypeName() == "RenderProduct":
            product_name_attr: Usd.Attribute = child.GetAttribute("productName")
            if product_name_attr:
                path: str = product_name_attr.Get(0)
                if path:
                    resolved: str = os.path.normpath(os.path.join(os.path.dirname(usd_path), path))
                    output_directories.add(os.path.dirname(resolved))
    return output_directories


def _get_usd_asset_references(usd_file_paths: set[str]) -> tuple[set[str], set[str], list[str]]:
    """
    Traverse USD composition arcs to find all dependencies of the given USD files.

    Returns:
        A tuple of (input_files, output_directories, unresolved_paths).
    """
    input_files: set[str] = set()
    output_directories: set[str] = set()
    all_unresolved: list[str] = []

    for usd_path in usd_file_paths:
        layers, assets, unresolved = UsdUtils.ComputeAllDependencies(usd_path)
        usd_dir: str = os.path.dirname(usd_path)

        for layer in layers:
            if layer.realPath:
                input_files.add(layer.realPath)

        for asset in assets:
            input_files.add(os.path.normpath(os.path.join(usd_dir, asset)))
        all_unresolved.extend(unresolved)
        output_directories.update(_get_usd_output_directories(usd_path))

    return input_files, output_directories, all_unresolved


def _path_key(path: str) -> str:
    """``path`` spelled one way, for comparing it with another path.

    It is normalized, so ``/`` and ``\\`` are one separator on Windows, and its case is folded
    where the filesystem ignores case. ``os.path`` is read at call time, so tests can swap it.
    """
    return os.path.normcase(os.path.normpath(path))


def _get_scene_asset_references(
    rop_node: hou.Node, keep_refs: bool = False, expand_frame_sequences: bool = True
) -> AssetReferences:
    """Return the scene's auto-detected inputs and outputs for the ROP. Each input is the path its
    parm evaluates to, or with keep_refs the ref as typed, for the PDG scheduler's own scan. A frame
    sequence is its files, or with expand_frame_sequences off one path with $F, as Parse Files keeps.
    """
    asset_references: AssetReferences = AssetReferences()
    asset_references.input_filenames.add(_get_hip_file())

    usd_files: set[str] = set()

    skip_outputs_keep_caches: bool = _is_skip_outputs_keep_caches_enabled(rop_node)
    # Each stored input's evaluated paths, so a pattern matches a ref as its parms evaluate it.
    evaluated: dict[str, set[str]] = {}
    sources: dict[str, str] = {}
    file_cache_nodes: dict[str, hou.Node] = {}

    for parm, ref in hou.fileReferences():
        if (
            (not parm)
            or (parm.node() == rop_node)
            or (ref.startswith(_IGNORE_REF_VALUES))
            or (parm.name() in _IGNORE_REF_PARMS)
        ):
            continue

        path: str = parm.evalAsString()
        # Each input is the path its own parm evaluates to, not its ref. The Submit dialog
        # attaches inputs as they are, and deadline-cloud doesn't expand $HIP or expressions.
        if os.path.isdir(path):
            asset_references.input_directories.add(ref if keep_refs else path)
            evaluated.setdefault(ref if keep_refs else path, set()).add(path)
            sources.setdefault(_path_key(path), parm.path())
        if os.path.isfile(path):
            sequences: dict[str, set[str]] = {ref if keep_refs else path: {path}}
            if _houdini_time_vars_to_glob(ref) != ref and not keep_refs:
                # A time variable reads a frame sequence. Evaluating the parm on two frames finds
                # it, also where the ref has node-local tokens such as $OS or a channel reference.
                sequences = _get_frame_sequences(parm)
            for entry, files in sequences.items():
                asset_references.input_filenames.add(entry)
                evaluated.setdefault(entry, set()).update(files)
                for file in files:
                    sources.setdefault(_path_key(file), parm.path())
            if os.path.splitext(path)[1].lower() in _USD_EXTENSIONS:
                # Collect USD files for dependency traversal in the next step.
                # We use the resolved absolute path because UsdUtils.ComputeAllDependencies
                # needs a real filesystem path, not a Houdini variable like $HIP/scene.usda.
                usd_files.add(os.path.realpath(path))
        if skip_outputs_keep_caches and parm.node().type().nameWithCategory() in _FILE_CACHE_TYPES:
            # A File Cache's loaded files aren't file references, so collect the node here.
            file_cache_nodes.setdefault(parm.node().path(), parm.node())

    # Traverse USD files to find nested dependencies
    if usd_files:
        usd_inputs: set[str]
        usd_outputs: set[str]
        unresolved: list[str]
        usd_inputs, usd_outputs, unresolved = _get_usd_asset_references(usd_files)
        asset_references.input_filenames.update(usd_inputs)
        asset_references.output_directories.update(usd_outputs)
        if unresolved:
            print(f"WARNING: {len(unresolved)} USD asset paths could not be resolved:")
            for p in unresolved:
                print(f"  {p}")

    all_inputs: list[hou.Node] = rop_node.inputAncestors()
    for node in all_inputs:
        asset_references.output_directories.update(_get_output_directories(node))

    file_cache_files: set[str] = set()
    if skip_outputs_keep_caches:
        file_cache_files = _add_file_cache_files(
            file_cache_nodes, asset_references, evaluated, sources
        )
        _drop_inputs_in_outputs(asset_references, sources)

    _drop_excluded_inputs(
        rop_node, asset_references, evaluated, sources, file_cache_files, keep_refs
    )
    if expand_frame_sequences and not keep_refs:
        # deadline-cloud doesn't expand $F, so the Submit dialog and the headless API get files.
        for entry in sorted(asset_references.input_filenames):
            if evaluated.get(entry, {entry}) != {entry}:
                asset_references.input_filenames.discard(entry)
                asset_references.input_filenames.update(evaluated[entry])
    return asset_references


def _add_file_cache_files(
    file_cache_nodes: dict[str, hou.Node],
    asset_references: AssetReferences,
    evaluated: dict[str, set[str]],
    sources: dict[str, str],
) -> set[str]:
    """Attach the files each File Cache with Load from Disk on reads, so its Base Folder, which
    holds every version, can be left out. Return the files, normalized for comparing."""
    file_cache_files: set[str] = set()
    for file_cache_path, file_cache_node in file_cache_nodes.items():
        load_parm: hou.Parm = file_cache_node.parm("loadfromdisk")
        # The read path is sopoutput, or file on the first File Cache version.
        read_parm: hou.Parm = file_cache_node.parm("sopoutput") or file_cache_node.parm("file")
        if load_parm is None or load_parm.eval() != 1 or read_parm is None:
            continue
        for entry, cache_files in _get_frame_sequences(read_parm).items():
            asset_references.input_filenames.add(entry)
            evaluated.setdefault(entry, set()).update(cache_files)
            for cache_file in cache_files:
                file_cache_files.add(_path_key(cache_file))
                sources.setdefault(_path_key(cache_file), file_cache_path)
    return file_cache_files


def _is_skip_outputs_keep_caches_enabled(rop_node: hou.Node) -> bool:
    """Return whether the ROP's Skip output folders, keep loaded caches toggle is on, which turns
    on the output folder rule and File Cache read files. A node without it is off."""
    parm: hou.Parm = rop_node.parm(_SKIP_OUTPUTS_KEEP_CACHES_PARM)
    return parm is not None and parm.eval() == 1


def _drop_inputs_in_outputs(asset_references: AssetReferences, sources: dict[str, str]) -> None:
    """Remove each input folder that is, holds, or is inside an output folder, which would upload
    earlier outputs, and print it with its source and the reason."""
    output_directories: list[str] = sorted(
        _path_key(d) for d in asset_references.output_directories if d
    )
    # Default outputs such as $HIP/$OS.$F.bgeo.sc write straight into these, so a folder
    # inside one isn't left out for being inside an output.
    scene_roots: set[str] = {
        _path_key(root)
        for root in (hou.getenv("HIP", ""), hou.getenv("JOB", ""), os.path.dirname(_get_hip_file()))
        if root
    }
    for directory in sorted(asset_references.input_directories):
        folder: str = _path_key(directory)
        reason: str = ""
        for output_directory in output_directories:
            # A root such as "/" already ends in a separator.
            if folder == output_directory:
                reason = f"it is the output directory {output_directory}"
            elif output_directory.startswith(folder.rstrip(os.sep) + os.sep):
                reason = f"it contains the output directory {output_directory}"
            elif output_directory not in scene_roots and folder.startswith(
                output_directory.rstrip(os.sep) + os.sep
            ):
                reason = f"it is inside the output directory {output_directory}"
            if reason:
                break
        if reason:
            asset_references.input_directories.discard(directory)
            source: str = sources.get(folder, "the scene")
            print(f"Deadline Cloud: left out input directory {directory} (from {source}): {reason}")


def _get_excluded_input_path_rows(rop_node: hou.Node) -> list[str]:
    """Return the node's excluded input path rows as typed, without expanding variables. A node
    without them, such as the PDG scheduler, has none."""
    parm: hou.Parm = rop_node.parm(_EXCLUDED_INPUT_PATHS_PARM)
    return [] if parm is None else [row.unexpandedString() for row in parm.multiParmInstances()]


def _set_excluded_input_path_rows(rop_node: hou.Node, patterns: list[str]) -> None:
    """Replace the node's excluded input path rows, dropping blank ones, as one undo entry that
    writes only the rows that changed."""
    rows: list[str] = [p.strip() for p in patterns if p.strip()]
    parm: hou.Parm = rop_node.parm(_EXCLUDED_INPUT_PATHS_PARM)
    # The box saves on every keystroke, so an unchanged save must not add an undo entry.
    if [row.unexpandedString() for row in parm.multiParmInstances()] == rows:
        return
    with hou.undos.group("Edit excluded input paths"):
        # Setting the count keeps the existing rows, so only the changed ones are written.
        if parm.multiParmInstancesCount() != len(rows):
            parm.set(len(rows))
        for row, value in zip(parm.multiParmInstances(), rows):
            if row.unexpandedString() != value:
                row.set(value)


def _split_path(path: str) -> list[str]:
    """Return the names in the normalized path. A root such as "/" or "C:\\" is one name."""
    separators: str = re.escape(os.sep) + (re.escape(os.altsep) if os.altsep else "")
    names: list[str] = re.split(f"[{separators}]", _path_key(path))
    # A root ends in a separator, so drop the empty last name.
    if len(names) > 1 and names[-1] == "":
        names.pop()
    return names


def _match_names(names: list[str], pattern_names: list[str]) -> bool:
    """Return whether the names match the pattern's names, with "**" matching any number of
    folders. Wildcards and "**" don't match a name that starts with a dot."""
    if not pattern_names:
        return not names
    if pattern_names[0] == "**":
        # Stop at the first dot folder, which "**" can't cross.
        dot_folder: int = next((i for i, n in enumerate(names) if n.startswith(".")), len(names))
        return any(_match_names(names[i:], pattern_names[1:]) for i in range(dot_folder + 1))
    return (
        bool(names)
        and (pattern_names[0].startswith(".") or not names[0].startswith("."))
        and fnmatch.fnmatchcase(names[0], pattern_names[0])
        and _match_names(names[1:], pattern_names[1:])
    )


def _get_matching_pattern(path: str, patterns: list[str], whole_path: bool) -> Optional[str]:
    """Return the first pattern the evaluated path is at or under, or None. With whole_path,
    the pattern must name the path itself, and a trailing "**" names nothing."""
    names: list[str] = _split_path(path)
    matching_pattern: Optional[str] = None
    for pattern in patterns:
        pattern_names: list[str] = _split_path(pattern)
        # A leading run of whole names must match, so everything under a match matches too.
        prefix_lengths: Iterable[int] = range(1, len(names) + 1)
        if whole_path:
            while pattern_names and pattern_names[-1] == "**":
                pattern_names.pop()
            prefix_lengths = [len(names)]
        if any(_match_names(names[:length], pattern_names) for length in prefix_lengths):
            matching_pattern = pattern
            break
    return matching_pattern


def _drop_excluded_inputs(
    rop_node: hou.Node,
    asset_references: AssetReferences,
    evaluated: dict[str, set[str]],
    sources: dict[str, str],
    file_cache_files: set[str],
    keep_refs: bool,
) -> None:
    """Remove the auto-detected inputs at or under an excluded input path, from the node or the
    env var. The scene file stays, and so does a File Cache's file unless a pattern names it. A
    frame sequence keeps its other frames as files, and with keep_refs a ref stays whole."""
    typed: list[str] = _get_excluded_input_path_rows(rop_node)
    typed += os.environ.get(EXCLUDED_INPUT_PATHS_ENV_VAR, "").split(os.pathsep)
    # A variable can expand to nothing, so drop empty patterns after expanding too.
    expanded = (hou.text.expandString(p.strip()) for p in typed if p.strip())
    patterns: list[str] = [p for p in expanded if p]
    if not patterns:
        return
    hip: str = _path_key(_get_hip_file())
    for kind, entries in (
        ("file", asset_references.input_filenames),
        ("directory", asset_references.input_directories),
    ):
        for entry in sorted(entries):
            # A ref held by several parms is left out only when every path they give matches.
            paths: list[str] = sorted(evaluated.get(entry) or {entry})
            pattern_by_path: dict[str, Optional[str]] = {
                path: _get_matching_pattern(path, patterns, _path_key(path) in file_cache_files)
                for path in paths
            }
            unmatched: list[str] = [p for p, pattern in pattern_by_path.items() if pattern is None]
            if _path_key(entry) == hip:
                if not unmatched:
                    print(f"Deadline Cloud: kept the scene file {entry}, which every job needs")
            elif unmatched and keep_refs:
                if len(unmatched) < len(paths):
                    print(
                        f"Deadline Cloud: kept input {kind} {entry}, because these paths match "
                        f"no excluded input path: {', '.join(unmatched)}"
                    )
            elif len(unmatched) < len(paths):
                for path, pattern in pattern_by_path.items():
                    if pattern is not None:
                        print(
                            f"Deadline Cloud: left out input {kind} {path} (from "
                            f"{sources.get(_path_key(path), 'the scene')}): it is at or under "
                            f"the excluded input path {pattern}"
                        )
                entries.discard(entry)
                # A frame sequence with some frames left out keeps the others as files.
                entries.update(unmatched)


def _get_frame_sequences(parm: hou.Parm) -> dict[str, set[str]]:
    """Return the parm's path with $F for each frame number, and the existing files it names on
    every frame. Every number that changes between two adjacent frames is a frame number."""
    frame: float = hou.frame()
    pieces: list[str] = re.split(r"(-?\d+)", parm.evalAtFrame(frame))
    next_pieces: list[str] = re.split(r"(-?\d+)", parm.evalAtFrame(frame + 1))
    changed: list[int] = [i for i in range(1, len(pieces), 2) if pieces[i] != next_pieces[i]]
    # Text that changes, such as an expression, isn't a frame number, so only those two files.
    sequences: dict[str, set[str]] = {p: {p} for p in ("".join(pieces), "".join(next_pieces))}
    if len(pieces) == len(next_pieces) and pieces[::2] == next_pieces[::2] and changed:
        # The frame numbers in one path keep the same offsets, such as v1001/a.1001.bgeo.
        offsets: list[int] = [int(pieces[i]) - int(pieces[changed[0]]) for i in changed]
        files: list[str] = _find_frame_files(pieces, changed, offsets)
        # One $F row can't say v1001/a.1002.bgeo, so a sequence with offsets is its files.
        sequences = {f: {f} for f in files}
        if not any(offsets):
            # The sequence as one row, which Save Bundle expands, such as sim.$F4.bgeo for 1001.
            # Braces keep Houdini from reading $F4_v2 as one variable name.
            sequence: str = "".join(
                (
                    ("${F%d}" if re.match(r"\w", pieces[i + 1]) else "$F%d") % len(p.lstrip("-"))
                    if i in changed
                    else p
                )
                for i, p in enumerate(pieces)
            )
            sequences = {sequence: set(files)}
    return {
        path: existing
        for path, files in sequences.items()
        if (existing := {f for f in files if os.path.isfile(f)})
    }


def _find_frame_files(pieces: list[str], frames: list[int], offsets: list[int]) -> list[str]:
    """Return the paths on disk that are pieces with a frame number at each index in frames, the
    numbers keeping offsets, such as v1001/a.1001.bgeo. Each keeps the form of pieces."""
    pattern: str = "".join("*" if i in frames else glob.escape(p) for i, p in enumerate(pieces))
    # glob joins with os.sep, so on Windows a match mixes forward and back slashes.
    frame_text: str = "".join(
        (
            r"(-?\d+)"
            if i in frames
            else r"[\\/]".join(re.escape(name) for name in re.split(r"[\\/]", p))
        )
        for i, p in enumerate(pieces)
    )
    found_files: list[str] = []
    for found in glob.glob(pattern):
        # Read the numbers loosely, then let the path key decide: on Windows a match mixes
        # slashes and has the file name's case on disk.
        match = re.fullmatch(frame_text, found, re.IGNORECASE)
        numbers: list[str] = list(match.groups()) if match else []
        if not numbers or [int(n) - int(numbers[0]) for n in numbers] != offsets:
            continue
        frame_numbers = iter(numbers)
        candidate: str = "".join(
            next(frame_numbers) if i in frames else p for i, p in enumerate(pieces)
        )
        if _path_key(candidate) == _path_key(found):
            found_files.append(candidate)
    return found_files


def _get_output_directories(node: hou.Node) -> set[str]:
    """
    Find and return the set of all output directories detected from the passed
    in node.
    """
    type_name = node.type().nameWithCategory()
    out_parm = _NODE_DIR_MAP.get(type_name, None)

    if not out_parm:
        return set()

    if not callable(out_parm):
        path = node.parm(out_parm).eval()
        return {os.path.dirname(path)}

    return out_parm(node)


def _fetch_outputs(node: hou.Node) -> set[str]:
    """
    Check the source parm of the fetch node to get the pointed to node and its output directories.
    """
    inner_node = node.node(node.parm("source").eval())
    if not inner_node:
        return set()
    return _get_output_directories(inner_node)


def _wedge_outputs(node: hou.Node) -> set[str]:
    """
    Check the driver parm of the wedge node to get the pointed to node and its output directories.
    """
    inner_node = node.node(node.parm("driver").eval())
    if not inner_node:
        return set()
    return _get_output_directories(inner_node)


def _renderman_outputs(node: hou.Node) -> set[str]:
    """Obtain list of outputs from renderman displays with a file device"""
    output_directories: set[str] = set()
    displays = node.parm("ri_displays").eval()
    for index in range(displays):
        device = node.parm(f"ri_device_{index}").eval()
        # skip display devices, only collect file devices
        if device in ("it", "houdini"):
            continue
        path = node.parm(f"ri_display_{index}").eval()
        output_directories.add(os.path.dirname(path))
    return output_directories


def _husk_outputs(node: hou.Node) -> set[str]:
    """Obtain list of outputs by searching the standard /Render/Products scope
    of the USD stage input
    """
    output_directories: set[str] = set()

    for n in node.inputs():
        try:
            products = n.stage().GetPrimAtPath("/Render/Products")
            # GetPrimAtPath returns an invalid null Prim object
            # if a Prim doesn't exist at the specified path
            if not products.IsValid():
                continue
        except Exception:
            # no products found in standard namespace
            continue

        for child in products.GetChildren():
            if child.GetTypeName() == "RenderProduct":
                product_name_attr = child.GetAttribute("productName")
                path = product_name_attr.Get(0)
                output_directories.add(os.path.dirname(path))
    return output_directories


def _usd_render_outputs(node: hou.Node) -> set[str]:
    """Obtain list of outputs by checking in priority order for:
    1) The override output image value
    2) The input nodes to the node set by the LOP Path
    3) The input nodes to the current USD Render ROP
    """
    output_directories: set[str] = set()

    if override_path := node.parm("outputimage").eval():
        output_directories.add(os.path.dirname(override_path))
    elif (lop_path_node := hou.node(node.parm("loppath").evalAsNodePath())) and not node.parm(
        "loppath"
    ).isDisabled():
        output_directories.update(_husk_outputs(lop_path_node))
    else:
        output_directories.update(_husk_outputs(node))

    return output_directories


_NODE_DIR_MAP = {
    "Driver/alembic": "filename",  # Alembic
    "Driver/arnold": "ar_picture",  # Arnold
    "Driver/baketexture::3.0": "vm_uvoutputpicture1",  # Bake Texture
    "Driver/channel": "chopoutput",  # Channel
    "Driver/comp": "copoutput",  # Composite
    "Driver/dop": "dopoutput",  # Dynamics
    "Driver/fetch": _fetch_outputs,  # Fetch
    "Sop/filecache": "file",  # File Cache
    "Sop/filecache::2.0": "file",  # File Cache v2
    "Driver/filmboxfbx": "sopoutput",  # Filmbox FBX
    "Driver/geometry": "sopoutput",  # Geometry
    "Driver/karma": "picture",  # Karma
    "Driver/ifd": "vm_picture",  # Mantra
    "Driver/opengl": "picture",  # OpenGL
    "Driver/ris::3.0": _renderman_outputs,  # Renderman
    "Driver/Redshift_ROP": "RS_outputFileNamePrefix",  # Redshift
    "Sop/rop_alembic": "filename",  # ROP Alembic Output
    "Sop/rop_geometry": "sopoutput",  # ROP Geometry Output
    "Dop/rop_dop": "dopoutput",  # ROP Output Driver
    "Driver/usdrender": _usd_render_outputs,  # USD Render ROP
    "Lop/usdrender_rop": _usd_render_outputs,  # USD Render LOP ROP
    "Driver/vray_renderer": "SettingsOutput_img_file_path",  # Vray
    "Sop/rop_vrayproxy": "filepath",  # Vray
    "Driver/rop_vrayproxy": "filepath",  # Vray
    "Driver/wedge": _wedge_outputs,  # Wedge
}
