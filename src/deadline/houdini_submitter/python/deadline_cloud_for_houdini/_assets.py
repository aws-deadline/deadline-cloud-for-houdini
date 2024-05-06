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
    asset_references = AssetReferences()
    for n in rop_node.parm("input_filenames").multiParmInstances():
        asset_references.input_filenames.add(n.eval())
    for n in rop_node.parm("input_directories").multiParmInstances():
        asset_references.input_directories.add(n.eval())
    for n in rop_node.parm("output_directories").multiParmInstances():
        asset_references.output_directories.add(n.eval())
    return asset_references


def _parse_files(node):
    asset_references = _get_scene_asset_references(node)
    for ref in ("input_filenames", "input_directories", "output_directories"):
        p = node.parm(ref)
        while p.multiParmInstancesCount():
            p.removeMultiParmInstance(0)
        paths = sorted(list(getattr(asset_references, ref)))
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
