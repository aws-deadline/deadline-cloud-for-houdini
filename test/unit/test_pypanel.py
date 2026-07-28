# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# scripts/ holds flat (non-package) modules; put it on the path to import the render helper.
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pypanel import (  # noqa: E402
    SUBMITTER_PANEL_PLACEHOLDER,
    get_submitter_panel_source_path,
    get_template_path,
    render_pypanel,
)


def test_render_pypanel_injects_source(tmp_path: Path) -> None:
    template = tmp_path / "deadline_cloud.pypanel"
    template.write_text(
        f"<script><![CDATA[\n{SUBMITTER_PANEL_PLACEHOLDER}\n]]></script>", encoding="utf-8"
    )
    src = tmp_path / "submitter_panel.py"
    src.write_text("def onCreateInterface():\n    return None\n", encoding="utf-8")

    rendered = render_pypanel(template, src)

    assert SUBMITTER_PANEL_PLACEHOLDER not in rendered
    assert "def onCreateInterface():" in rendered


def test_render_pypanel_raises_when_placeholder_missing(tmp_path: Path) -> None:
    template = tmp_path / "deadline_cloud.pypanel"
    template.write_text("<script><![CDATA[\n# no token here\n]]></script>", encoding="utf-8")
    src = tmp_path / "submitter_panel.py"
    src.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Placeholder"):
        render_pypanel(template, src)


def test_render_pypanel_real_files_are_well_formed_xml() -> None:
    """The committed template + real submitter_panel.py must render to valid, complete XML."""
    submitter_src = Path(__file__).resolve().parents[2] / "src" / "deadline" / "houdini_submitter"
    rendered = render_pypanel(
        get_template_path(submitter_src),
        get_submitter_panel_source_path(submitter_src),
    )

    ET.fromstring(rendered)  # raises if the injected result is not well-formed XML
    assert SUBMITTER_PANEL_PLACEHOLDER not in rendered
    assert "def onCreateInterface" in rendered
