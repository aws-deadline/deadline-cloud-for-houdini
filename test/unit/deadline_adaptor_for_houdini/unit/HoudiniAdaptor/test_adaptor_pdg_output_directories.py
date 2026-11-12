# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""How the Houdini adaptor passes a PDG job's extra output folders to the client."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List

import pytest

from deadline.houdini_adaptor.HoudiniAdaptor import HoudiniAdaptor

# The PDG adaptor tests' own fakes and builders, so both files drive the adaptor the same way.
from .test_adaptor_pdg import PDG_RUN, FakeHoudini, FakeStorage, make_adaptor, pdg_init


def test_the_client_gets_each_output_directory_as_this_worker_sees_it(tmp_path: Path) -> None:
    """The client scans a folder for what a task wrote, so it needs the worker's spelling and the workstation's."""
    # A session with one output folder and a rule that maps /studio to /mnt.
    init: Dict[str, Any] = pdg_init(str(tmp_path / "pdg"))
    init["pdg"]["output_directories"] = ["/studio/renders"]
    rule: Dict[str, str] = dict(
        source_path_format="POSIX",
        source_path="/studio",
        destination_path="/mnt",
        destination_os="POSIX",
    )
    adaptor: HoudiniAdaptor = make_adaptor(
        init, FakeStorage(), path_mapping_data={"path_mapping_rules": [rule]}
    )
    with FakeHoudini(adaptor) as houdini:
        adaptor.on_run(copy.deepcopy(PDG_RUN))
    sent: List[Any] = [
        (action.args or {})["mapped_output_directories"] for action in houdini.received
    ]
    assert sent == [[["/mnt/renders", "/studio/renders"]]]


@pytest.mark.parametrize(
    "folders, valid", [(["/studio/renders"], True), ("/studio/renders", False), ([""], False)]
)
def test_the_init_data_schema_takes_only_a_list_of_directories(
    tmp_path: Path, folders: Any, valid: bool
) -> None:
    """A single string would be scanned one letter at a time, so the schema refuses it at the start."""
    # Validate a PDG session's init data holding the drawn folders.
    init: Dict[str, Any] = pdg_init(str(tmp_path / "pdg"))
    init["pdg"]["output_directories"] = folders
    validator: Any = make_adaptor(init).validators.init_data
    if valid:
        validator.validate(init)
    else:
        with pytest.raises(Exception):
            validator.validate(init)
