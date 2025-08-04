# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import argparse
import sys
from pathlib import Path

import hou
from deadline_cloud_for_houdini._assets import (  # type: ignore
    _get_evaluated_asset_references,
    _parse_files,
)
from deadline_cloud_for_houdini.submitter import _create_job_bundle  # type: ignore


def _create_material_node() -> hou.VopNode:
    """
    Create a material node to use as a wedge channel.
    """
    mat_node = hou.node("/mat").createNode("tooncolorshader", "tooncolorshader1")

    mat_node.parm("colorhighr").set(0.771)
    mat_node.parm("colorhighg").set(0.279)
    mat_node.parm("colorhighb").set(0.411)

    mat_node.parm("colormidr").set(0)
    mat_node.parm("colormidg").set(0.5)
    mat_node.parm("colormidb").set(1)

    mat_node.parm("colorlowr").set(0.314667)
    mat_node.parm("colorlowg").set(-0.129333)
    mat_node.parm("colorlowb").set(0.564667)

    return mat_node


def _create_scene_and_rop(output_dir: str) -> hou.RopNode:
    """
    Uses the Houdini API to create nodes comprising a simple scene.
    """

    hou.hipFile.setName("test_wedge.hip")

    geo_node = hou.node("/obj").createNode("geo", "test_geometry")
    geo_node.createNode("box", "test_box")
    geo_node.parm("shop_materialpath").set("/mat/tooncolorshader1")

    cam_node = hou.node("/obj").createNode("cam", "test_cam")
    light_node = hou.node("/obj").createNode("hlight", "test_light")

    # Translate & rotate the camera back to capture the box
    cam_translate = hou.hmath.buildTranslate(5, 5, 5)
    cam_node.setParmTransform(cam_translate)
    cam_node.setWorldTransform(cam_node.buildLookatRotation(geo_node))

    # Translate the light to visibly shade the box
    light_translate = hou.hmath.buildTranslate(1, 1, 2)
    light_node.setParmTransform(light_translate)

    # To create two distinct frames, we will move the camera.
    # This requires creating keyframes on the camera's transform
    cam_tx = hou.parm("/obj/test_cam/tx")
    cam_frame1 = hou.Keyframe()
    cam_frame1.setFrame(1)
    cam_frame1.setValue(5)

    cam_frame2 = hou.Keyframe()
    cam_frame2.setFrame(2)
    cam_frame2.setValue(5.5)

    cam_tx.setKeyframes((cam_frame1, cam_frame2))

    render_node = hou.node("/out").createNode("ifd")

    # Set the render node to use the camera we just created
    render_node.parm("camera").set(cam_node.path())
    render_node.parm("vm_picture").set(f"{output_dir}/render/$HIPNAME.$OS.$WEDGE.$WEDGENUM.$F4.png")
    render_node.parm("soho_mkpath").set(1)  # Set intermediate directories

    return render_node


def _create_wedge_node() -> hou.RopNode:
    """
    Place a wedge node in the /out network and give it the parameters we expect.
    """
    wedge_node = hou.node("/out").createNode("wedge")
    wedge_node.parm("random").set(0)
    wedge_node.parm("driver").set("/out/mantra1")

    # Create one wedge parameter using the material node as a channel
    wedge_node.parm("wedgeparams").set(1)

    wedge_node.parm("name1").set("wedge_colour_midr")
    wedge_node.parm("chan1").set("../../mat/tooncolorshader1/colormidr")
    wedge_node.parm("range1x").set(0)
    wedge_node.parm("range1y").set(1)
    wedge_node.parm("steps1").set(2)

    return wedge_node


def _set_parameters(submitter_node: hou.Node):
    """
    Set scene parameters in the submitter to create the template we expect.
    """
    submitter_node.parm("name").set("$HIPNAME")
    submitter_node.parm("description").set("Wedge")
    submitter_node.parm("separate_steps").set(1)
    submitter_node.parm("include_adaptor_wheels").set(0)
    submitter_node.parm("auto_unlock_rops").set(0)
    submitter_node.parm("auto_parse_hip").set(0)
    submitter_node.parm("auto_save_hip").set(0)

    # Set the frame range
    submitter_node.parm("trange").set(
        2
    )  # This is an enum dropdown. 2 corresponds to the "Render Frame Range" option.
    hou.playbar.setFrameRange(1, 2)


def _build_scene(output_dir: str) -> None:
    _create_material_node()
    _create_scene_and_rop(output_dir)
    wedge_node = _create_wedge_node()
    submitter_node = hou.node("/out").createNode("deadline_cloud")

    submitter_node.setFirstInput(wedge_node)
    _set_parameters(submitter_node)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_history_dir")
    parser.add_argument("output_dir")
    parser.add_argument("test_type", type=str, choices=["submitter", "adaptor"])
    args = parser.parse_args(args=sys.argv[sys.argv.index("--") :])

    # We want to use the same scene for both submitter and adaptor tests.
    # Depending on which test, we have different uses for the scene file:
    # Either use the submitter node to generate a job bundle,
    # or save the scene for use in an `openjd run` call.
    _build_scene(args.output_dir)

    if args.test_type == "submitter":
        submitter_node = hou.node("/out/deadline_cloud1")
        _parse_files(submitter_node)
        _create_job_bundle(
            submitter_node,
            args.job_history_dir,
            _get_evaluated_asset_references(submitter_node),
        )
    elif args.test_type == "adaptor":
        hou.hipFile.save(str(Path(args.output_dir).joinpath(hou.hipFile.basename())))
