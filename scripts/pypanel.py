# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Shared helper for rendering the Houdini Python panel.

The committed template lives at ``src/deadline/houdini_submitter/python_panels/
deadline_cloud.pypanel.template``. Its ``<script>`` block contains only a placeholder token; at
install/build time the placeholder is replaced with the contents of ``submitter_panel.py`` so the
Python stays a normal, lintable/typable source file while still being deployed embedded in the
pypanel. The rendered result is written next to the template as ``deadline_cloud.pypanel``
(gitignored). Houdini discovers it via the plugin's ``hpath`` (which points at the submitter src
dir) and only loads files ending in ``.pypanel`` -- so the ``.template`` sitting beside it is
ignored. This module is shared by the dev installer (``install_dev_submitter.py``) and the
production installer build (``build_installer.py``) so the two can't drift.
"""

from pathlib import Path

# Token that lives inside the pypanel template's <script> CDATA and is replaced with the
# submitter_panel.py source at render time.
SUBMITTER_PANEL_PLACEHOLDER = "# __SUBMITTER_PANEL_SOURCE__"

# Canonical locations, all relative to the submitter source dir (which is the plugin's hpath).
# Centralized here so the dev and production installers can't drift.
_PYPANEL_DIRNAME = "python_panels"
_TEMPLATE_FILENAME = "deadline_cloud.pypanel.template"
_RENDERED_FILENAME = "deadline_cloud.pypanel"
_SUBMITTER_PANEL_SOURCE_RELPATH = ("panel", "submitter_panel.py")


def get_template_path(submitter_src: Path) -> Path:
    """Return the committed pypanel template path under the submitter source dir."""
    return submitter_src / _PYPANEL_DIRNAME / _TEMPLATE_FILENAME


def get_rendered_path(submitter_src: Path) -> Path:
    """Return the rendered (gitignored) pypanel path, co-located with the template."""
    return submitter_src / _PYPANEL_DIRNAME / _RENDERED_FILENAME


def get_submitter_panel_source_path(submitter_src: Path) -> Path:
    """Return the submitter_panel.py source path whose contents get injected into the template."""
    return submitter_src.joinpath(*_SUBMITTER_PANEL_SOURCE_RELPATH)


def render_pypanel(template_path: Path, panel_source_path: Path) -> str:
    """Return the pypanel template with the submitter panel source injected into its placeholder.

    Args:
        template_path: Path to the committed ``.pypanel`` template containing the placeholder.
        panel_source_path: Path to ``submitter_panel.py`` whose contents get injected.

    Raises:
        RuntimeError: If the template does not contain the expected placeholder token.
    """
    template = template_path.read_text(encoding="utf-8")
    panel_source = panel_source_path.read_text(encoding="utf-8")

    if SUBMITTER_PANEL_PLACEHOLDER not in template:
        raise RuntimeError(
            f"Placeholder {SUBMITTER_PANEL_PLACEHOLDER!r} not found in {template_path}"
        )

    return template.replace(SUBMITTER_PANEL_PLACEHOLDER, panel_source)
