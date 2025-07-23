# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import argparse
import sys

from deadline_cloud_for_houdini.submitter import _create_job_bundle  # type: ignore
from deadline_cloud_for_houdini._assets import _get_evaluated_asset_references, _parse_files  # type: ignore
import hou


def _create_scene_and_rop(output_dir: str) -> hou.RopNode:
    """
    Uses the Houdini API to create nodes comprising a simple scene.
    """

    hou.hipFile.setName("test.hip")

    geo_node = hou.node("/obj").createNode("geo", "test_geometry")
    geo_node.createNode("box", "test_box")
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
    render_node.parm("camera").set(cam_node.name())
    render_node.parm("vm_picture").set(f"{output_dir}/test.png")

    return render_node


def _set_parameters(submitter_node: hou.Node):
    """
    Set scene parameters in the submitter to create the template we expect.
    """
    submitter_node.parm("name").set("$HIPNAME")
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


def create_submitter_bundle(job_history_dir: str, output_dir: str) -> None:
    """
    Add a submitter node to the pre-defined scene and set parameters.
    """
    submitter_node = hou.node("/out").createNode("deadline_cloud")
    render_node = _create_scene_and_rop(output_dir)
    submitter_node.setFirstInput(render_node)
    _set_parameters(submitter_node)

    _parse_files(submitter_node)
    _create_job_bundle(
        submitter_node, job_history_dir, _get_evaluated_asset_references(submitter_node)
    )


def save_as_hip(job_history_dir: str, output_dir: str) -> None:
    """
    Not yet implemented.

    Save the pre-defined scene as a HIP file so it can be used in adaptor tests.
    """
    pass


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
    if args.test_type == "submitter":
        create_submitter_bundle(args.job_history_dir, args.output_dir)
    elif args.test_type == "adaptor":
        save_as_hip(args.job_history_dir, args.output_dir)
