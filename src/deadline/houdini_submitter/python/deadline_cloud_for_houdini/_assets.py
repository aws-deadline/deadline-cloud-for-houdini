# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import glob
import os
import re

from deadline.client.job_bundle.submission import AssetReferences
from pxr import Usd, UsdUtils

import hou

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

# The ROP toggle that turns on output subtraction and the exclusion list, and that list itself.
_FILTER_AUTO_INPUTS_PARM: str = "filter_auto_inputs"
_EXCLUDED_INPUTS_PARM: str = "excluded_inputs"
# The SOP types with the File Cache's Load from Disk and read path parms, whose loaded cache files
# are added as inputs when the toggle is on.
_FILE_CACHE_TYPES: tuple = (
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

    for n in rop_node.parm("input_filenames").multiParmInstances():
        filepath = n.unexpandedString()
        filepath_glob = _houdini_time_vars_to_glob(filepath)
        if filepath == filepath_glob:
            asset_references.input_filenames.add(n.eval())
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
    auto_asset_refs = _get_scene_asset_references(node)
    prev_auto_asset_refs = _get_saved_auto_detected_asset_references(node)

    filter_inputs: bool = _is_filter_auto_inputs_enabled(node)
    if filter_inputs:
        # Read the exclusions before the lists are rebuilt below: an auto-detected input the
        # artist deleted from the lists is only known by comparing the lists as they are now.
        excluded_inputs: list[str] = sorted(
            {n.unexpandedString() for n in _get_input_exclusion_parms(node)} - {""}
        )

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
    if filter_inputs:
        # Save the deleted inputs in the Excluded Inputs list, so every later scan leaves them out.
        _update_paths_parm(node, _EXCLUDED_INPUTS_PARM, excluded_inputs)


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


def _get_scene_asset_references(rop_node: hou.Node) -> AssetReferences:
    # collect input filenames
    asset_references: AssetReferences = AssetReferences()
    asset_references.input_filenames.add(_get_hip_file())

    usd_files: set[str] = set()

    # With the ROP's filter toggle on, each input is kept as the path it evaluates to on its own
    # parm, with its refs and that parm's path, so outputs and exclusions can be compared with it
    # and each left-out input can be reported. With the toggle off, the scan is unchanged.
    filter_inputs: bool = _is_filter_auto_inputs_enabled(rop_node)
    detected_directories: dict[str, tuple[set[str], str]] = {}
    detected_files: dict[str, tuple[set[str], str]] = {}
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
        # Check the evaluated version to ensure _something_ exists, but add
        # the unexpanded version to evaluate afterwards. Allows us to limit
        # files with parameters, such as $F, to one entry instead of possibly
        # hundreds/thousands. hou.fileReferences() already returns the refs as
        # the equivalent of calling parm.unexpandedString()
        # https://www.sidefx.com/docs/houdini/hom/hou/fileReferences.html
        if os.path.isdir(path):
            if filter_inputs:
                # Keep the evaluated folder, not the ref: the ref, evaluated later on this ROP,
                # loses the context of expressions such as `chs("shot")` and widens to its parent.
                _add_detected_input(detected_directories, path, ref, parm.path())
            else:
                asset_references.input_directories.add(ref)
        if os.path.isfile(path):
            if filter_inputs:
                # Keep the files the parm reads, not the ref: the Submit dialog and the headless
                # API attach these paths as they are, and deadline-cloud doesn't expand $HIP.
                for read_file in _get_parm_read_files(parm, ref, path):
                    asset_references.input_filenames.add(read_file)
                    _add_detected_input(detected_files, read_file, ref, parm.path())
            else:
                asset_references.input_filenames.add(ref)
            if os.path.splitext(path)[1].lower() in _USD_EXTENSIONS:
                # Collect USD files for dependency traversal in the next step.
                # We use the resolved absolute path because UsdUtils.ComputeAllDependencies
                # needs a real filesystem path, not a Houdini variable like $HIP/scene.usda.
                usd_files.add(os.path.realpath(path))
        if filter_inputs and _is_loaded_file_cache(parm.node()):
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

    if filter_inputs:
        # Add the files each loading File Cache reads, so they stay attached when its Base
        # Folder, which also holds every other version, is left out.
        for file_cache_path, file_cache_node in file_cache_nodes.items():
            for cache_file in _get_file_cache_read_files(file_cache_node):
                asset_references.input_filenames.add(cache_file)
                _add_detected_input(detected_files, cache_file, cache_file, file_cache_path)
        _filter_auto_inputs(rop_node, asset_references, detected_directories, detected_files)

    return asset_references


def _add_detected_input(
    detected: dict[str, tuple[set[str], str]], path: str, ref: str, source: str
) -> None:
    """Record that ref, from source, names the input at the evaluated path. The first source of a
    path is the one reported; every ref is kept, so an exclusion can match any of them."""
    refs, _ = detected.setdefault(path, (set(), source))
    refs.add(ref)


def _is_filter_auto_inputs_enabled(rop_node: hou.Node) -> bool:
    """Return whether the ROP's filter toggle is on. A node without the toggle, such as the PDG
    scheduler node, counts as off."""
    parm: hou.Parm = rop_node.parm(_FILTER_AUTO_INPUTS_PARM)
    return parm is not None and parm.eval() == 1


def _normalize_path(path: str) -> str:
    """Return the path in the one form used to compare paths: no trailing separator or '..'
    segments, and on Windows lowercase with backslashes."""
    return os.path.normcase(os.path.normpath(path))


def _is_inside(path: str, directory: str) -> bool:
    """Return whether the normalized path is strictly inside the normalized directory. A sibling
    that only shares a string prefix (/proj/render2 and /proj/render) isn't inside it."""
    prefix: str = directory if directory.endswith(os.sep) else directory + os.sep
    return path != directory and path.startswith(prefix)


def _get_scene_root_directories() -> set[str]:
    """Return the normalized $HIP, $JOB, and the folder that holds the .hip file.

    Default output paths, such as the Geometry ROP's $HIP/$OS.$F.bgeo.sc, often write straight into
    one of these. An input folder inside one of them is not dropped for being inside an output.
    """
    roots: set[str] = set()
    for root in (hou.getenv("HIP", ""), hou.getenv("JOB", ""), os.path.dirname(_get_hip_file())):
        if root:
            roots.add(_normalize_path(root))
    return roots


def _get_output_overlap_reason(
    directory: str, output_directories: list[str], scene_roots: set[str]
) -> str:
    """Return why the normalized input directory overlaps a detected output directory, or "".

    An input folder that is, holds, or sits inside a folder the job writes would upload earlier
    outputs, such as previous render or cache versions. The "inside" case is skipped for an output
    folder that is a scene root (see _get_scene_root_directories).
    """
    for output_directory in output_directories:
        if directory == output_directory:
            return f"it is the output directory {output_directory}"
        if _is_inside(output_directory, directory):
            return f"it contains the output directory {output_directory}"
        if output_directory not in scene_roots and _is_inside(directory, output_directory):
            return f"it is inside the output directory {output_directory}"
    return ""


def _get_input_exclusion_parms(rop_node: hou.Node) -> list[hou.Parm]:
    """Return the ROP's parms that each name one excluded input.

    These are the entries of the Excluded Inputs list, plus each saved auto-detected input that
    the artist deleted from the Input Filenames or Input Directories list since the last scan.
    The deletions count right away, so the Submit dialog honors them before the next scan saves
    them to the Excluded Inputs list. A deletion is found the way _parse_files finds manually
    added inputs: by comparing the displayed lists with the saved auto-detected lists.
    """
    parms: list[hou.Parm] = list(rop_node.parm(_EXCLUDED_INPUTS_PARM).multiParmInstances())
    # A saved auto-detected input that is no longer in the displayed lists was deleted there.
    displayed: AssetReferences = _get_unevaluated_asset_references(rop_node)
    displayed_inputs: set[str] = displayed.input_filenames | displayed.input_directories
    for auto_parm_name in ("auto_input_filenames", "auto_input_directories"):
        for auto_parm in rop_node.parm(auto_parm_name).multiParmInstances():
            if auto_parm.unexpandedString() not in displayed_inputs:
                parms.append(auto_parm)
    return parms


def _filter_auto_inputs(
    rop_node: hou.Node,
    asset_references: AssetReferences,
    detected_directories: dict[str, tuple[set[str], str]],
    detected_files: dict[str, tuple[set[str], str]],
) -> None:
    """Add the detected input directories to asset_references, leaving out each one that overlaps
    a detected output directory or is excluded, and remove each excluded input file.

    detected_directories and detected_files map each evaluated folder or file to the refs that
    named it and the parm or node it came from (see _add_detected_input). Each left-out
    input is printed with its path, source, and reason, so the artist can see what was left out.
    """
    exclusion_parms: list[hou.Parm] = _get_input_exclusion_parms(rop_node)
    # An exclusion matches an input by its text, which can be the input's path or one of its refs,
    # or by the path it evaluates to on the ROP. An exclusion with a backtick expression is matched
    # by its text only: on the ROP its expression loses its node and can widen to a parent folder.
    excluded_strings: set[str] = {n.unexpandedString() for n in exclusion_parms} - {""}
    excluded_paths: set[str] = {
        _normalize_path(n.evalAsString())
        for n in exclusion_parms
        if n.evalAsString() and "`" not in n.unexpandedString()
    }

    def is_excluded(path: str, refs: set[str]) -> bool:
        return (
            path in excluded_strings
            or _normalize_path(path) in excluded_paths
            or not refs.isdisjoint(excluded_strings)
        )

    output_directories: list[str] = sorted(
        _normalize_path(d) for d in asset_references.output_directories if d
    )
    scene_roots: set[str] = _get_scene_root_directories()

    # Keep each detected directory unless it overlaps an output or is excluded.
    for directory, (directory_refs, source) in sorted(detected_directories.items()):
        excluded: bool = is_excluded(directory, directory_refs)
        reason: str = _get_output_overlap_reason(
            _normalize_path(directory), output_directories, scene_roots
        ) or ("it is in the Excluded Inputs list" if excluded else "")
        if reason:
            print(f"Deadline Cloud: left out input directory {directory} (from {source}): {reason}")
        else:
            asset_references.input_directories.add(directory)

    # Remove each excluded file. An entry with no recorded source, such as a USD dependency or the
    # .hip file, is already an evaluated path.
    for entry in sorted(asset_references.input_filenames):
        file_refs, file_source = detected_files.get(entry, (set[str](), "the scene"))
        if is_excluded(entry, file_refs):
            asset_references.input_filenames.discard(entry)
            print(
                f"Deadline Cloud: left out input file {entry} (from {file_source}): "
                "it is in the Excluded Inputs list"
            )


def _is_loaded_file_cache(node: hou.Node) -> bool:
    """Return whether the node is a File Cache SOP, or an RBD or Vellum I/O SOP, with Load from
    Disk on."""
    if node is None or node.type().nameWithCategory() not in _FILE_CACHE_TYPES:
        return False
    load_parm: hou.Parm = node.parm("loadfromdisk")
    return load_parm is not None and load_parm.eval() == 1


def _get_frame_glob(path: str, next_path: str) -> str:
    """Return a glob that matches every frame of a sequence, from the paths of two adjacent frames.

    The two paths first differ inside the frame number. The whole run of digits there (1001 and
    1002 differ in the last digit, 1009 and 1010 in the last two) becomes "*". Like the
    time-variable globs in _get_evaluated_asset_references, it can match more frames than the job
    renders, but only files of the same sequence. Only the first frame number that differs is
    replaced, so a path that holds the frame number twice, such as /c/1001/f.1001.bgeo, matches
    only the first frame's file name.
    """
    start: int = 0
    while start < min(len(path), len(next_path)) and path[start] == next_path[start]:
        start += 1
    stop: int = start
    # Widen to the frame number's digits on both sides of the first difference.
    while start > 0 and path[start - 1].isdigit():
        start -= 1
    while stop < len(path) and path[stop].isdigit():
        stop += 1
    return glob.escape(path[:start]) + "*" + glob.escape(path[stop:])


def _get_file_cache_read_files(node: hou.Node) -> set[str]:
    """Return the existing cache files that a loading File Cache SOP reads.

    The read path is the sopoutput parm (file on the first File Cache version).
    """
    parm: hou.Parm = node.parm("sopoutput") or node.parm("file")
    if parm is None:
        return set()
    return _get_frame_sequence_files(parm)


def _get_parm_read_files(parm: hou.Parm, ref: str, path: str) -> set[str]:
    """Return the existing files that a file parm reads, as paths evaluated on the parm itself.

    path is the parm's value at the current frame. A ref with a time variable or an expression,
    such as $F4, can read a frame sequence, so it gets the files of that sequence, the way
    _get_evaluated_asset_references globs such refs.
    """
    if _houdini_time_vars_to_glob(ref) == ref:
        return {path}
    return _get_frame_sequence_files(parm)


def _get_frame_sequence_files(parm: hou.Parm) -> set[str]:
    """Return the existing files that the parm's path names, on every frame.

    When the path changes from one frame to the next, every file of that sequence is returned.
    """
    # Compare two adjacent frames to tell a single file from a frame sequence.
    frame: float = hou.frame()
    path: str = parm.evalAtFrame(frame)
    next_path: str = parm.evalAtFrame(frame + 1)
    if path == next_path:
        return {path} if os.path.isfile(path) else set()
    return {f for f in glob.glob(_get_frame_glob(path, next_path)) if os.path.isfile(f)}


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
