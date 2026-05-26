# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Creates a minimal HIP file with a Geometry ROP for testing adaptor session persistence
without requiring a render license (mantra).
"""

import argparse
import sys
from pathlib import Path

import hou


def save_as_hip(output_dir: str) -> None:
    hou.hipFile.setName("test_dcc_open.hip")
    # Create a geometry ROP - has standard render parms (trange) but doesn't need mantra
    geo_node = hou.node("/obj").createNode("geo", "test_geo")
    geo_node.createNode("box", "box1")
    rop = hou.node("/out").createNode("geometry", "geo_rop")
    rop.parm("soppath").set("/obj/test_geo/box1")
    rop.parm("sopoutput").set(f"{output_dir}/output.$F4.bgeo.sc")
    hou.hipFile.save(str(Path(output_dir).joinpath(hou.hipFile.basename())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir")
    args = parser.parse_args(args=sys.argv[sys.argv.index("--") :])
    save_as_hip(args.output_dir)
