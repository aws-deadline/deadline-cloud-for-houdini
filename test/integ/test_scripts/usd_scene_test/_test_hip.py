# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Integration test script for USD scene dependency detection.

Creates a USD scene that exercises multiple composition arcs:
- Sublayer (scene.usda -> lighting.usda)
- Reference (lighting.usda -> model.usda)
- Payload (scene.usda -> heavy_asset.usda)
- Asset path with relative path (model.usda -> textures/wood.exr)
- RenderProduct output directory

Then loads it into Houdini via a LOP Sublayer node, connects a USD Render ROP
to a Deadline Cloud ROP, and runs _parse_files + _create_job_bundle.
"""

import argparse
import sys
from pathlib import Path

from deadline_cloud_for_houdini.submitter import _create_job_bundle  # type: ignore
from deadline_cloud_for_houdini._assets import _get_evaluated_asset_references, _parse_files  # type: ignore
import hou


def _create_usd_scene(base_dir: Path) -> dict[str, str]:
    """Create a multi-arc USD scene on disk. Returns paths to all files."""
    textures_dir = base_dir / "textures"
    textures_dir.mkdir(parents=True, exist_ok=True)

    # Dummy texture
    texture_path = textures_dir / "wood.exr"
    texture_path.write_bytes(b"\x00")

    # model.usda — references a texture via relative path
    model_path = base_dir / "model.usda"
    model_path.write_text(
        """\
#usda 1.0

def Xform "Model"
{
    def Mesh "Cube"
    {
        float3[] extent = [(-0.5, -0.5, -0.5), (0.5, 0.5, 0.5)]
    }

    def Material "WoodMaterial"
    {
        def Shader "DiffuseTexture"
        {
            uniform token info:id = "UsdUVTexture"
            asset inputs:file = @./textures/wood.exr@
        }
    }
}
"""
    )

    # heavy_asset.usda — loaded via payload
    heavy_asset_path = base_dir / "heavy_asset.usda"
    heavy_asset_path.write_text(
        """\
#usda 1.0

def Xform "HeavyGeo"
{
    def Mesh "Sphere"
    {
        float3[] extent = [(-1, -1, -1), (1, 1, 1)]
    }
}
"""
    )

    # lighting.usda — references model.usda
    lighting_path = base_dir / "lighting.usda"
    lighting_path.write_text(
        """\
#usda 1.0
(
    subLayers = [
        @./model.usda@
    ]
)

def DomeLight "DomeLight"
{
    float inputs:intensity = 1.0
}
"""
    )

    # scene.usda — sublayers lighting.usda, payloads heavy_asset.usda, defines RenderProduct
    scene_path = base_dir / "scene.usda"
    scene_path.write_text(
        """\
#usda 1.0
(
    subLayers = [
        @./lighting.usda@
    ]
)

def Xform "HeavyAssetRef" (
    payload = @./heavy_asset.usda@
)
{
}

def Camera "MainCamera"
{
    float focalLength = 50
    double3 xformOp:translate = (0, 0, 5)
    uniform token[] xformOpOrder = ["xformOp:translate"]
}

def Scope "Render"
{
    def Scope "Products"
    {
        def RenderProduct "RenderProduct"
        {
            token productName = "renders/output.exr"
            token productType = "raster"
        }
    }
}
"""
    )

    return {
        "scene": str(scene_path),
        "lighting": str(lighting_path),
        "model": str(model_path),
        "heavy_asset": str(heavy_asset_path),
        "texture": str(texture_path),
    }


def create_submitter_bundle(job_history_dir: str, output_dir: str) -> None:
    """Build a Houdini scene with a USD stage and generate a job bundle."""
    base_dir = Path(output_dir) / "usd_scene"
    usd_paths = _create_usd_scene(base_dir)

    hou.hipFile.setName("test_usd.hip")

    # Create LOP network with Sublayer node
    lopnet = hou.node("/obj").createNode("lopnet", "test_lopnet")
    sublayer = lopnet.createNode("sublayer", "sublayer1")
    sublayer.parm("num_files").set(1)
    sublayer.parm("filepath1").set(usd_paths["scene"])

    # Create USD Render ROP
    usd_render = hou.node("/out").createNode("usdrender", "usdrender1")
    usd_render.parm("loppath").set(sublayer.path())

    # Create Deadline Cloud ROP
    submitter_node = hou.node("/out").createNode("deadline_cloud")
    submitter_node.setFirstInput(usd_render)

    submitter_node.parm("name").set("$HIPNAME")
    submitter_node.parm("separate_steps").set(1)
    submitter_node.parm("include_adaptor_wheels").set(0)
    submitter_node.parm("auto_unlock_rops").set(0)
    submitter_node.parm("auto_parse_hip").set(0)
    submitter_node.parm("auto_save_hip").set(0)
    submitter_node.parm("trange").set(2)
    hou.playbar.setFrameRange(1, 1)

    _parse_files(submitter_node)
    _create_job_bundle(
        submitter_node, job_history_dir, _get_evaluated_asset_references(submitter_node)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_history_dir")
    parser.add_argument("output_dir")
    parser.add_argument("test_type", type=str, choices=["submitter"])
    args = parser.parse_args(args=sys.argv[sys.argv.index("--") :])

    create_submitter_bundle(args.job_history_dir, args.output_dir)
