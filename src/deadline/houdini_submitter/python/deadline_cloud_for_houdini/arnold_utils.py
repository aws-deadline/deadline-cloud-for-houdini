# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Arnold ROP utility functions for the Deadline Cloud Houdini submitter.

Provides detection, configuration, and output directory resolution for Arnold
ROP nodes. Handles known Arnold pitfalls:
- ar_picture set to 'ip' causes hython to hang (interactive render mode)
- ar_ass_export_enable may be disabled, preventing .ass file generation
- Arnold menu parameters expect strings, not integers (type conversion needed)
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import hou

from .hip_settings import ArnoldExportSettings

logger = logging.getLogger(__name__)


def is_arnold_rop(node: "hou.Node") -> bool:
    """Check if a node is an Arnold ROP (Driver/arnold)."""
    try:
        return node.type().name() == "arnold"
    except Exception:
        return False


def find_arnold_rops_in_network(rop_node: "hou.Node") -> list["hou.Node"]:
    """Walk inputAncestors() and return all Arnold ROP nodes."""
    arnold_rops = []
    for ancestor in rop_node.inputAncestors():
        if is_arnold_rop(ancestor):
            arnold_rops.append(ancestor)
    return arnold_rops


def _set_parm_safe(parm: "hou.Parm", value) -> None:
    """Set a parameter value with automatic type conversion for menu parameters.

    Arnold menu parameters (like ar_log_verbosity, ar_ass_export_enable) are
    string-typed in Houdini but often receive integer values. This function
    handles the TypeError fallback by converting int → str.
    """
    if isinstance(value, bool):
        parm.set(1 if value else 0)
        return

    try:
        parm.set(value)
    except TypeError:
        if isinstance(value, int):
            parm.set(str(value))
        else:
            raise


def configure_arnold_rop_for_export(
    rop: "hou.Node", settings: ArnoldExportSettings
) -> None:
    """Apply safe export configuration to an Arnold ROP before submission.

    Addresses known Arnold pitfalls:
    - Sets ar_ass_export_enable = 1 so .ass files are actually written
    - Clears ar_picture to prevent hython hanging on 'ip' (interactive render)
    - Sets logging and license-failure parameters
    - Optionally overrides ar_ass_file output path

    All parameter sets use try/except with string fallback for menu params.
    Missing optional parameters are silently skipped.
    """
    params_to_set: dict[str, tuple] = {}

    if settings.enable_ass_export:
        params_to_set["ar_ass_export_enable"] = (1, False)

    if settings.disable_image_render:
        params_to_set["ar_picture"] = ("", False)

    params_to_set["ar_log_verbosity"] = (settings.log_verbosity, False)
    params_to_set["ar_log_console_enable"] = (1, False)
    params_to_set["ar_abort_on_license_fail"] = (1, False)

    if settings.ass_output_path:
        params_to_set["ar_ass_file"] = (settings.ass_output_path, True)

    for param_name, (param_value, is_required) in params_to_set.items():
        parm = rop.parm(param_name)
        if parm is None:
            if is_required:
                raise RuntimeError(
                    f"Required parameter '{param_name}' not found on Arnold ROP '{rop.path()}'"
                )
            continue

        try:
            _set_parm_safe(parm, param_value)
        except Exception as e:
            logger.warning(
                "Failed to set '%s' on '%s': %s", param_name, rop.path(), e
            )


def _snapshot_parms(rop: "hou.Node", parm_names: list[str]) -> dict[str, str]:
    """Capture the unexpanded string values of parameters for later restore."""
    snapshot = {}
    for name in parm_names:
        parm = rop.parm(name)
        if parm is not None:
            try:
                snapshot[name] = parm.unexpandedString()
            except Exception:
                snapshot[name] = parm.evalAsString()
    return snapshot


def _restore_parms(rop: "hou.Node", snapshot: dict[str, str]) -> None:
    """Restore parameter values from a snapshot."""
    for name, value in snapshot.items():
        parm = rop.parm(name)
        if parm is not None:
            try:
                parm.set(value)
            except Exception:
                logger.warning("Failed to restore '%s' on '%s'", name, rop.path())


# Parameters that configure_arnold_rop_for_export may modify
_ARNOLD_EXPORT_PARM_NAMES = [
    "ar_ass_export_enable",
    "ar_picture",
    "ar_log_verbosity",
    "ar_log_console_enable",
    "ar_abort_on_license_fail",
    "ar_ass_file",
]


def export_arnold_ass_locally(
    rop: "hou.Node",
    settings: ArnoldExportSettings,
    frame_range: tuple[int, int, int] | None = None,
    restore_parms: bool = True,
) -> list[str]:
    """Configure an Arnold ROP and render it locally to produce .ass files.

    Args:
        rop: The Arnold ROP node to export from.
        settings: Export configuration settings.
        frame_range: Optional (start, end, step) tuple. If None, renders current frame.
        restore_parms: If True, restore original ROP parameter values after export.

    Returns:
        List of .ass file paths that were written.
    """
    import hou as _hou

    # Snapshot original values so we can restore after export
    original_parms = _snapshot_parms(rop, _ARNOLD_EXPORT_PARM_NAMES) if restore_parms else {}

    try:
        # Configure the ROP for .ass export
        configure_arnold_rop_for_export(rop, settings)

        # Ensure ar_ass_file has a value
        ass_parm = rop.parm("ar_ass_file")
        if ass_parm is None:
            raise RuntimeError(f"Arnold ROP '{rop.path()}' has no ar_ass_file parameter")

        ass_path = ass_parm.eval()
        if not ass_path:
            # Set a default output path
            hip = _hou.getenv("HIP", "/tmp")
            default_path = f"{hip}/ass/{rop.name()}.$F4.ass"
            ass_parm.set(default_path)
            ass_path = ass_parm.eval()
            logger.info("Set default .ass output path: %s", default_path)

        # Create output directory if needed
        out_dir = os.path.dirname(ass_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        # Render (this triggers the .ass export)
        # hou.RopNode.render() accepts frame_range as a 2- or 3-element tuple:
        #   (start, end) or (start, end, step)
        if frame_range:
            rop.render(frame_range=frame_range, ignore_inputs=True)
        else:
            rop.render(ignore_inputs=True)

        # Collect exported files
        exported_files = []
        import glob as _glob

        # Replace frame tokens with glob pattern to find exported files
        unexpanded = ass_parm.unexpandedString()
        glob_pattern = re.sub(
            r"\$F\d*|\${F\d*}|\$FF|\${FF}", "*", unexpanded
        )
        # Evaluate remaining Houdini variables in the glob pattern
        # Use try/finally to guarantee parm is restored even on error
        orig = ass_parm.unexpandedString()
        try:
            ass_parm.set(glob_pattern)
            evaluated_glob = ass_parm.eval()
        except Exception:
            evaluated_glob = glob_pattern
        finally:
            ass_parm.set(orig)

        for f in sorted(_glob.iglob(evaluated_glob)):
            exported_files.append(f)

        if not exported_files:
            # Single frame case — check the evaluated path directly
            if os.path.isfile(ass_path):
                exported_files.append(ass_path)

        return exported_files
    finally:
        # Restore original ROP parameters so the scene isn't permanently modified
        if restore_parms and original_parms:
            _restore_parms(rop, original_parms)


def get_arnold_ass_output_directories(node: "hou.Node") -> set[str]:
    """Get output directories from an Arnold ROP.

    Prioritizes ar_ass_file (the .ass export path) over ar_picture.
    Handles Houdini time variables ($F, $F4, etc.) via glob pattern replacement
    so that the directory portion is still valid.
    """
    from ._assets import _houdini_time_vars_to_glob

    output_dirs: set[str] = set()

    # Check ar_ass_file first (preferred for .ass export)
    for parm_name in ("ar_ass_file", "ar_picture"):
        parm = node.parm(parm_name)
        if parm is None:
            continue
        path = parm.eval()
        if not path or path == "ip":
            continue
        # Strip time variables before taking dirname
        clean_path = _houdini_time_vars_to_glob(path)
        dirname = os.path.dirname(clean_path)
        if dirname:
            output_dirs.add(dirname)
        # If we got a valid dir from ar_ass_file, prefer that over ar_picture
        if output_dirs and parm_name == "ar_ass_file":
            break

    return output_dirs
