# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Creates a minimal HIP file with a USD Import SOP whose File parm (filepath1) is an
absolute path under a mapped source directory. USD opens that path itself, so
HOUDINI_PATHMAP does not reach it: unless the adaptor remaps the SOP parm, the
import fails to cook on the worker with "Invalid source .../lopimport1".

The path lives under a "submitter_assets" directory that is deliberately never
created on disk, because Houdini deactivates a HOUDINI_PATHMAP rule whose source
path exists locally. The file itself is written to "worker_assets", where the rule
maps it.

A Geometry ROP writes the imported geometry, so the adaptor has a render_node that
cooks the USD Import SOP without requiring a render license.
"""

# Standard library modules, then hou, which only hython can import
import argparse
import sys
from pathlib import Path

import hou


def save_as_hip(output_dir: str) -> None:
    """Builds the scene under output_dir and saves it there as test_usd_import_sop.hip."""
    # Name the scene, so it saves under the name the job parameters use
    hou.hipFile.setName("test_usd_import_sop.hip")

    # The source directory is never created, so the path mapping rule stays active.
    submitter_dir: Path = Path(output_dir) / "submitter_assets"

    # The destination the rule maps to holds the USD file the SOP imports.
    worker_dir: Path = Path(output_dir) / "worker_assets"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "cables.usda").write_text(
        '#usda 1.0\n\ndef Mesh "cable"\n{\n'
        "    int[] faceVertexCounts = [3]\n"
        "    int[] faceVertexIndices = [0, 1, 2]\n"
        "    point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]\n"
        "}\n",
        encoding="utf-8",
    )

    # USD Import SOP -> Transform SOP, as an artist would bring USD into SOPs.
    # as_posix() because the adaptor forward-slashes both sides of every rule when
    # building HOUDINI_PATHMAP (see _set_houdini_pathmap).
    geo_node: hou.Node = hou.node("/obj").createNode("geo", "test_geo")
    usd_import: hou.Node = geo_node.createNode("usdimport", "usdimport1")
    usd_import.parm("filepath1").set((submitter_dir / "cables.usda").as_posix())
    transform: hou.Node = geo_node.createNode("xform", "transform1")
    transform.setInput(0, usd_import)

    # A Geometry ROP (no render license needed) cooks the import as the render_node
    rop: hou.Node = hou.node("/out").createNode("geometry", "geo_rop")
    rop.parm("soppath").set(transform.path())
    rop.parm("sopoutput").set(f"{output_dir}/output.$F4.bgeo.sc")

    hou.hipFile.save(str(Path(output_dir) / hou.hipFile.basename()))


if __name__ == "__main__":
    # Read the output folder from the arguments after "--", as hython passes them
    parser: argparse.ArgumentParser = argparse.ArgumentParser()
    parser.add_argument("output_dir")
    args: argparse.Namespace = parser.parse_args(args=sys.argv[sys.argv.index("--") :])
    save_as_hip(args.output_dir)
