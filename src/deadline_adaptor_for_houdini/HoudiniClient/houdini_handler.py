# Copyright 2022 Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import os as os
from typing import TYPE_CHECKING, Any, Callable, Dict, List

try:
    import hou  # type: ignore
except ImportError:  # pragma: no cover
    raise OSError("Could not find the Houdini module. Are you running this inside of Houdini?")

if TYPE_CHECKING:  # pragma: no cover
    from hou import Node


class HoudiniHandler:
    action_dict: Dict[str, Callable[[Dict[str, Any]], None]] = {}
    render_kwargs: Dict[str, Any]
    nodes: List[Node]

    def __init__(self) -> None:
        """
        Constructor for the houdini handler. Initializes action_dict and render variables
        """
        self.action_dict = {
            "scene_file": self.set_scene_file,
            "render_node": self.set_render_node,
            "frame": self.set_frame,
            "ignore_input_nodes": self.set_ignore_input_nodes,
            "start_render": self.start_render,
        }
        self.render_kwargs = {"ignore_input_nodes": True}
        self.node = None

    def set_node_settings(self, node):
        # this is a place holder function
        # TODO remove after commom node library implemented

        node_type = node.type().nameWithCategory().split("/")
        if node_type[0] == "Driver":
            if node_type[1] == "ifd":
                # mantra render node
                alfredProgress = node.parm("vm_alfprogress")
                if alfredProgress is not None:
                    alfredProgress.set(1)
                    print("Enabled Alfred style progress")

                verbosity = node.parm("vm_verbose")
                if verbosity is not None:
                    verbosity.set(3)
                    print("Set verbosity to 3")

            elif node_type[1] == "karma":
                alfredProgress = node.parm("alfprogress")
                if alfredProgress is not None:
                    alfredProgress.set(1)
                    print("Enabled Alfred style progress")
                verbosity = node.parm("verbosity")
                if verbosity is not None:
                    verbosity.set("3")
                    hou.logging.setRenderLogVerbosity(3)
                    print("Set verbosity to 3")

        else:
            pass

    def start_render(self, data: dict) -> None:
        """
        Uses active node and calls hou's render, currently hardcoded to rendering a single fram

        Args:
            data (dict):

        Raises:
            RuntimeError: .
        """

        if not self.node:
            raise TypeError("Render node is 'None', no render node has been loaded")

        self.set_node_settings(self.node)
        increment = 1
        self.node.parm("trange").set(1)
        frame = self.render_kwargs["frame"]
        frame_range = (frame, frame, increment)
        interleave = hou.renderMethod.RopByRop
        self.node.render(
            verbose=True,
            frame_range=frame_range,
            ignore_inputs=self.render_kwargs["ignore_input_nodes"],
            method=interleave,
        )

        print("Finished Rendering")

    def set_ignore_input_nodes(self, data: dict) -> None:
        """
        Sets
        Args:
            data (dict): ]
        """
        self.render_kwargs["ignore_input_nodes"] = bool(data.get("ignore_input_nodes", True))

    def set_render_node(self, data: dict) -> None:
        """
        Sets .

        Args:
            data (dict):
        """
        self.node = hou.node(data.get("render_node", ""))

    def set_frame(self, data: dict) -> None:
        """
        Sets

        Args:
            data (dict):

        """
        self.render_kwargs["frame"] = int(data.get("frame", ""))

    def set_scene_file(self, data: dict) -> None:
        """
        Opens the scene file in Houdini.

        Args:
            data (dict): The data given from the Adaptor. Keys expected: ['scene_file']

        Raises:
            FileNotFoundError: If path to the scene file does not yield a file
        """
        scene_path = data.get("scene_file", "")
        if not os.path.isfile(scene_path):
            raise FileNotFoundError(f"The scene file '{scene_path}' does not exist")
        try:
            hou.hipFile.load(scene_path)
        except hou.LoadWarning as e:
            print(e)
