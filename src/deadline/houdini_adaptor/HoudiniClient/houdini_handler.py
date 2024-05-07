# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

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
            "wedge_node": self.set_wedge_node,
            "wedgenum": self.set_wedge_num,
            "start_render": self.start_render,
        }
        self.render_kwargs = {"ignore_input_nodes": True}
        self.node = None
        self.wedge = None
        self.wegenum = None

    def set_node_settings(self, node):
        # this is a place holder function
        # TODO remove after common node library implemented

        node_type = node.type().nameWithCategory().split("/")

        if node_type[0] != "Driver":
            return

        if len(node_type) < 2:
            return

        if node_type[1] == "ifd":
            # Mantra render node
            alfredProgress = node.parm("vm_alfprogress")
            if alfredProgress is not None:
                alfredProgress.set(1)
                print("Enabled Alfred style progress")
            verbosity = node.parm("vm_verbose")
            if verbosity is not None:
                # Mantra verbosity is an int with range 0 to 5
                if isinstance(verbosity.eval(), int) and verbosity.eval() < 2:
                    # 2 provides basic logging, we set it as a minimum to help with debugging issues
                    verbosity.set(2)
                    if verbosity.eval() == 2:
                        # The verbosity won't be changed if the parameter is "keyed", so we check the value before
                        # logging that it was increased
                        # https://www.sidefx.com/docs/houdini/network/parms.html#color
                        print("Increased verbosity to 2 to include basic logging")
                print(f"Logging verbosity is set to {verbosity.eval()}")
            return

        if node_type[1] == "usdrender":
            # Karma render node
            alfredProgress = node.parm("alfprogress")
            if alfredProgress is not None:
                alfredProgress.set(1)
                print("Enabled Alfred style progress")
            verbosity = node.parm("verbosity")
            if verbosity is not None:
                # Karma verbosity is a str with options "", "3", "9", "9p", "9P"
                # 3 means "Rendering Statistics", we set it as a minimum to help with debugging issues
                if isinstance(verbosity.eval(), str) and verbosity.eval() == "":
                    verbosity.set("3")
                    if verbosity.eval() == "3":
                        # The verbosity won't be changed if the parameter is "keyed", so we check the value before
                        # logging that it was increased
                        # https://www.sidefx.com/docs/houdini/network/parms.html#color
                        print("Increased verbosity to '3' to include basic logging")
                print(f"Logging verbosity is set to '{verbosity.eval()}'")

    def start_render(self, data: dict) -> None:
        """
        Uses active node and calls hou's render, currently hardcoded to rendering a single frame

        Args:
            data (dict):

        Raises:
            RuntimeError: .
        """

        def setenvvariable(var, val):
            hou.hscript(f"set {var} = {val}")
            hou.hscript("varchange")

        if not self.node:
            raise TypeError("Render node is 'None', no render node has been loaded")

        self.set_node_settings(self.node)

        # handle wedge data
        if self.wedge and self.wedgenum:
            print("Wedged step")
            wedgenum = int(self.wedgenum)
            hm = self.wedge.hdaModule()
            allwedge, _stashedparms, _errormsg = hm.getwedges(self.wedge)
            if 0 <= wedgenum < len(allwedge):
                wl = allwedge[wedgenum]
                setenvvariable("WEDGENUM", str(wedgenum))
                print(f"Applying wedge: {wedgenum}")
                hm.applyspecificwedge(self.wedge, wl)
            else:
                raise ValueError(f"WEDGENUM out of range: {wedgenum}")

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

        setenvvariable("WEDGENUM", "")

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
        Sets the render node to render

        Args:
            data (dict):
        """
        node = hou.node(data.get("render_node", ""))
        if node is None:
            raise TypeError("Render node is 'None', no render node has been loaded")
        print(f"node: {node}")
        self.node = node

    def set_wedge_node(self, data: dict) -> None:
        """
        sets the wedge node

        args:
            data (dict):
        """
        wedge_node_path = data.get("wedge_node", "")
        if wedge_node_path:
            wedge = hou.node(wedge_node_path)
            if wedge is not None:
                print(f"wedge node: {wedge}")
                self.wedge = wedge

    def set_wedge_num(self, data: dict) -> None:
        """
        sets the wedge num

        args:
            data (dict):
        """
        wedgenum = data.get("wedgenum", None)
        if wedgenum is not None:
            print(f"wedgenum {wedgenum}")
            self.wedgenum = wedgenum

    def set_frame(self, data: dict) -> None:
        """
        Sets the frame to render

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
