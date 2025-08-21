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


def _create_scene_and_rop(output_dir: str) -> hou.RopNode:
    """
    Uses the Houdini API to create nodes comprising a simple scene.
    """

    geo_node = hou.node("/obj").createNode("geo", "test_geometry")
    geo_node.createNode("box", "test_box")

    # Create two distinct cameras for our render nodes
    cam_node1 = hou.node("/obj").createNode("cam", "test_cam")
    cam_node2 = hou.node("/obj").createNode("cam", "test_cam2")

    light_node = hou.node("/obj").createNode("hlight", "test_light")

    # Camera transformations
    cam_node1.setParmTransform(hou.hmath.buildTranslate(5, 5, 5))
    cam_node1.setWorldTransform(cam_node1.buildLookatRotation(geo_node))
    cam_node2.setParmTransform(hou.hmath.buildTranslate(5, 2, 0))
    cam_node2.setWorldTransform(cam_node2.buildLookatRotation(geo_node))

    # Translate the light to visibly shade the box
    light_translate = hou.hmath.buildTranslate(1, 1, 2)
    light_node.setParmTransform(light_translate)

    # To create two distinct frames, we will move the camera.
    # This requires creating keyframes on the camera's transform
    cam_tx1 = hou.parm("/obj/test_cam/tx")
    cam1_frame1 = hou.Keyframe()
    cam1_frame1.setFrame(1)
    cam1_frame1.setValue(5)

    cam1_frame2 = hou.Keyframe()
    cam1_frame2.setFrame(2)
    cam1_frame2.setValue(5.5)

    cam_tx1.setKeyframes((cam1_frame1, cam1_frame2))

    cam_tx2 = hou.parm("/obj/test_cam2/tx")
    cam2_frame1 = hou.Keyframe()
    cam2_frame1.setFrame(1)
    cam2_frame1.setValue(5)

    cam2_frame2 = hou.Keyframe()
    cam2_frame2.setFrame(2)
    cam2_frame2.setValue(5.5)

    cam_tx2.setKeyframes((cam2_frame1, cam2_frame2))

    render_node1 = hou.node("/out").createNode("ifd")
    render_node2 = hou.node("/out").createNode("ifd")

    # Set the render node to use the camera we just created
    render_node1.parm("camera").set(cam_node1.path())
    render_node1.parm("vm_picture").set(f"{output_dir}/$HIPNAME.$OS.$F4.png")
    render_node1.parm("soho_mkpath").set(1)  # Set intermediate directories

    render_node2.parm("camera").set(cam_node2.path())
    render_node2.parm("vm_picture").set(f"{output_dir}/$HIPNAME.$OS.$F4.png")
    render_node2.parm("soho_mkpath").set(1)  # Set intermediate directories

    # Create a dependency by chaining the render nodes
    render_node2.setFirstInput(render_node1)

    return render_node2


def _set_parameters(submitter_node: hou.Node):
    """
    Set scene parameters in the submitter to create the template we expect.
    """
    submitter_node.parm("name").set("$HIPNAME")
    submitter_node.parm("description").set("Render dependencies")
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


def _build_scene(output_dir: str, scene_name: str) -> None:
    hou.hipFile.setName(scene_name)
    final_render_node = _create_scene_and_rop(output_dir)
    submitter_node = hou.node("/out").createNode("deadline_cloud")

    submitter_node.setFirstInput(final_render_node)
    _set_parameters(submitter_node)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_history_dir")
    parser.add_argument("output_dir")
    parser.add_argument("scene_name")
    parser.add_argument("test_type", type=str, choices=["submitter", "adaptor"])
    args = parser.parse_args(args=sys.argv[sys.argv.index("--") :])

    # We want to use the same scene for both submitter and adaptor tests.
    # Depending on which test, we have different uses for the scene file:
    # Either use the submitter node to generate a job bundle,
    # or save the scene for use in an `openjd run` call.
    _build_scene(args.output_dir, args.scene_name)

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
