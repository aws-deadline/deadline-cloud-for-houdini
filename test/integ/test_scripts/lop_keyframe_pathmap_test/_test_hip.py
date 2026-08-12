# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Creates a minimal HIP file with a LOP sublayer node whose filepath1 parm has a
keyframe set on it.  This exercises the code path in _remap_lop_file_paths that
must gracefully handle hou.OperationFailed from parm.unexpandedString() on
keyframed/animated parms (PR #355 regression).

A second sublayer node has a NON-keyframed filepath1 under the same mapped
source prefix so the path mapping rule remaps it successfully, proving one
skipped parm does not cost the rest of the scene its path mapping.

Both filepaths live under a "submitter_assets" directory that is deliberately
never created on disk, because Houdini deactivates a HOUDINI_PATHMAP rule whose
source path exists locally.

A Geometry ROP is also created so the adaptor has a valid render_node to target
without requiring a render license.
"""

import argparse
import sys
from pathlib import Path

import hou


def save_as_hip(output_dir: str) -> None:
    hou.hipFile.setName("test_lop_keyframe.hip")

    # Houdini DEACTIVATES a HOUDINI_PATHMAP rule whose source path exists on the
    # local filesystem — `pathmap` annotates it "# Not active. Source path exists."
    # So the scene must reference paths under a source directory that is NOT
    # created on disk. That also mirrors a real render: the scene holds
    # submission-machine paths and the rule redirects them to worker paths.
    #
    # submitter_dir is deliberately never created. Creating it would deactivate
    # the rule and stop this test from exercising remapping at all.
    submitter_dir = Path(output_dir) / "submitter_assets"

    # The destination the rule maps to. This one DOES exist and holds real files.
    worker_dir = Path(output_dir) / "worker_assets"
    worker_dir.mkdir(parents=True, exist_ok=True)
    for asset_name in ("anim_asset.usda", "static_asset.usda"):
        (worker_dir / asset_name).write_text(
            '#usda 1.0\n\ndef Xform "Root"\n{\n}\n',
            encoding="utf-8",
        )

    # Create a LOP network with a sublayer node. Its filepath1 sits under the
    # mapped source prefix, so it WOULD be remapped were it not keyframed —
    # that is what makes skipping it meaningful rather than incidental.
    keyframed_path = (submitter_dir / "anim_asset.usda").as_posix()
    lopnet = hou.node("/obj").createNode("lopnet", "test_lopnet")
    sublayer = lopnet.createNode("sublayer", "sublayer1")
    sublayer.parm("num_files").set(1)
    sublayer.parm("filepath1").set(keyframed_path)

    # Set a KEYFRAME on filepath1 so that parm.unexpandedString() will raise
    # hou.OperationFailed — this is the trigger for the PR #355 bug.
    keyframe = hou.StringKeyframe()
    # Use repr() to produce a properly escaped Python string literal.
    # A raw f-string would break on Windows where backslash paths like
    # C:\Users\... contain \U (unicode escape) and \t (tab).
    keyframe.setExpression(repr(keyframed_path), hou.exprLanguage.Python)
    keyframe.setTime(0)
    sublayer.parm("filepath1").setKeyframe(keyframe)

    # Second sublayer node: NON-keyframed filepath under the same mapped source
    # prefix. It exercises the successful remap branch, proving one skipped parm
    # does not cost the rest of the scene its path mapping.
    #
    # as_posix() because the adaptor forward-slashes both sides of every rule when
    # building HOUDINI_PATHMAP (see _set_houdini_pathmap), so a backslash value
    # here would never prefix-match the rule on Windows.
    sublayer2 = lopnet.createNode("sublayer", "sublayer2")
    sublayer2.parm("num_files").set(1)
    sublayer2.parm("filepath1").set((submitter_dir / "static_asset.usda").as_posix())

    # Create a Geometry ROP (no render license needed) as the render_node target
    geo_node = hou.node("/obj").createNode("geo", "test_geo")
    geo_node.createNode("box", "box1")
    rop = hou.node("/out").createNode("geometry", "geo_rop")
    rop.parm("soppath").set("/obj/test_geo/box1")
    rop.parm("sopoutput").set(f"{output_dir}/output.$F4.bgeo.sc")

    hou.hipFile.save(str(Path(output_dir) / hou.hipFile.basename()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir")
    args = parser.parse_args(args=sys.argv[sys.argv.index("--") :])
    save_as_hip(args.output_dir)
