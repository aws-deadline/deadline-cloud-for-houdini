# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import os

from deadline.client.job_bundle.submission import AssetReferences

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


def _get_hip_file() -> str:
    return hou.hipFile.path()


def _get_asset_references(rop_node: hou.Node) -> AssetReferences:
    """
    Get the current paths stored in the parms backing the UI and return them as
    an AssetReferences object
    """
    asset_references = AssetReferences()

    asset_references.input_filenames.update(
        [n.eval() for n in rop_node.parm("input_filenames").multiParmInstances()]
    )
    asset_references.input_directories.update(
        [n.eval() for n in rop_node.parm("input_directories").multiParmInstances()]
    )
    asset_references.output_directories.update(
        [n.eval() for n in rop_node.parm("output_directories").multiParmInstances()]
    )

    return asset_references


def _get_saved_auto_detected_asset_references(rop_node: hou.Node) -> AssetReferences:
    """
    Get all of the paths saved in the hidden auto_* parms on the node and return
    them as an AssetReferences object.
    """
    saved_auto_refs = AssetReferences()
    saved_auto_refs.input_filenames.update(
        [n.eval() for n in rop_node.parm("auto_input_filenames").multiParmInstances()]
    )
    saved_auto_refs.input_directories.update(
        [n.eval() for n in rop_node.parm("auto_input_directories").multiParmInstances()]
    )
    saved_auto_refs.output_directories.update(
        [n.eval() for n in rop_node.parm("auto_output_directories").multiParmInstances()]
    )

    return saved_auto_refs


def _parse_files(node: hou.Node):
    """
    Generate the lists of input filenames, input directories and output directories
    based on the detected paths in the scene, any previously saved values and the
    current values in the UI. Then update the UI with the new lists of paths.
    """
    display_asset_refs = _get_asset_references(node)
    auto_asset_refs = _get_scene_asset_references(node)
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


def _get_scene_asset_references(rop_node: hou.Node) -> AssetReferences:
    # collect input filenames
    asset_references = AssetReferences()
    asset_references.input_filenames.add(_get_hip_file())

    for parm, ref in hou.fileReferences():
        if (
            (not parm)
            or (parm.node() == rop_node)
            or (ref.startswith(_IGNORE_REF_VALUES))
            or (parm.name() in _IGNORE_REF_PARMS)
        ):
            continue

        path = parm.evalAsString()
        if os.path.isdir(path):
            asset_references.input_directories.add(path)
        if os.path.isfile(path):
            asset_references.input_filenames.add(path)

    all_inputs = rop_node.inputAncestors()
    for node in all_inputs:
        asset_references.output_directories.update(_get_output_directories(node))

    return asset_references


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
        except Exception:
            # no products found in standard namespace
            return output_directories
        for child in products.GetChildren():
            if child.GetTypeName() == "RenderProduct":
                product_name_attr = child.GetAttribute("productName")
                path = product_name_attr.Get(0)
                output_directories.add(os.path.dirname(path))
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
    "Lop/usdrender_rop": _husk_outputs,  # Husk
    "Driver/karma": "picture",  # Karma
    "Driver/ifd": "vm_picture",  # Mantra
    "Driver/opengl": "picture",  # OpenGL
    "Driver/ris::3.0": _renderman_outputs,  # Renderman
    "Driver/Redshift_ROP": "RS_outputFileNamePrefix",  # Redshift
    "Sop/rop_alembic": "filename",  # ROP Alembic Output
    "Dop/rop_dop": "dopoutput",  # ROP Output Driver
    "Driver/vray_renderer": "SettingsOutput_img_file_path",  # Vray
    "Sop/rop_vrayproxy": "filepath",  # Vray
    "Driver/rop_vrayproxy": "filepath",  # Vray
    "Driver/wedge": _wedge_outputs,  # Wedge
}
