# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The PDG code imports the ROP submitter by the name the installer ships it under."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

_PDG: Path = Path(__file__).resolve().parents[3] / "src" / "deadline" / "houdini_pdg"

# An import of the ROP by its source tree name, which an installed Houdini doesn't have.
_SOURCE_TREE_IMPORT: re.Pattern = re.compile(
    r"^\s*(from|import)\s+deadline\.houdini_submitter\b", re.M
)


def test_no_pdg_module_or_hda_script_imports_the_rop_by_its_source_tree_name() -> None:
    """Installed, only ``deadline_cloud_for_houdini`` exists, so the long name fails to import."""
    sources: List[Path] = [
        *_PDG.rglob("*.py"),
        *_PDG.glob("houdini_plugin/otls/deadline_cloud_scheduler.hda/*/PythonModule"),
    ]
    # If the HDA moved, the glob would find nothing and the test would pass without reading it.
    assert any(path.name == "PythonModule" for path in sources)
    offenders: List[str] = [
        str(path.relative_to(_PDG))
        for path in sources
        if _SOURCE_TREE_IMPORT.search(path.read_text(encoding="utf8"))
    ]
    assert offenders == []
