# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from dataclasses import dataclass
from enum import Enum
import copy
import os
import sys
import yaml
import json
import traceback
from typing import Any, Dict, Optional, cast
from pathlib import Path

from botocore.exceptions import ClientError

from deadline.client.api import BaseSubmitter, BaseSubmitterSettings
from deadline.client.job_bundle._yaml import deadline_yaml_dump
from deadline.client import api
from deadline.client.job_bundle.submission import AssetReferences
from deadline.client.job_bundle import create_job_history_bundle_dir
from deadline.client.config import get_setting
from deadline.client.config.config_file import str2bool
from deadline.client.dataclasses import SubmitterInfo

from .queue_parameters import (
    update_queue_parameters,
    get_queue_parameter_values_as_openjd,
    set_queue_parameter_values_from_openjd,
    get_default_conda_packages,
    get_default_rez_packages,
)
from ._assets import (
    _get_hip_file,
    _get_evaluated_asset_references,
    _get_scene_asset_references,
    _parse_files,
)

# For temporary backwards compatibility
from ._assets import (
    _IGNORE_REF_PARMS as IGNORE_REF_PARMS,  # noqa
    _IGNORE_REF_VALUES as IGNORE_REF_VALUES,  # noqa
)
from ._version import version
from .hip_settings import HoudiniSubmitterUISettings
import hou


_NONE_SELECTED_TEXT = "<none selected>"
_REFRESHING_TEXT = "<refreshing>"


class RenderStrategy(Enum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"


@dataclass
class HoudiniSubmitterSettings(BaseSubmitterSettings):
    """Houdini-specific submission settings for the unified :class:`BaseSubmitter`.

    The Houdini submitter derives the job's step graph from the live ROP node
    (via the module-level ``_get_job_template``). Beyond the DCC-agnostic base
    fields (``job_name``, ``description``, ``priority``, ``initial_status``,
    ``max_failed_tasks_count``, ``max_retries_per_task``, the asset-reference
    lists) this carries the ROP node path plus the Houdini-specific
    adaptor-wheels fields, so the build methods can honor consumer edits to
    those values rather than silently re-reading the node.

    Note: the *frame range* is intentionally not a settings-driven input. A
    Houdini job's per-step frames/dependencies are derived from the live ROP
    network via ``hscript render`` (see ``_get_rop_steps``); a single
    ``frame_list`` string cannot represent that graph, so ``get_settings``
    populates ``frame_list`` for information only and the builders take frames
    from the ROP.
    """

    rop_node_path: str = ""
    include_adaptor_wheels: bool = False
    adaptor_wheels: str = ""


class HoudiniSubmitter(BaseSubmitter):
    """Headless :class:`BaseSubmitter` implementation for Houdini.

    This is the unified-API entry point that host-agnostic consumers (e.g. the
    AYON bridge, ``get_submitter_for_host("houdini")``) drive, and the same
    engine the native GUI submitter routes through (see ``_create_job_bundle``).
    ``get_job_template`` reuses the module-level ``_get_job_template`` for the
    ROP-derived step graph and overrides the settings-driven fields; parameter
    values are built from ``settings`` plus the ROP's queue parameters, and
    asset references come from the ``_assets`` scene scan. The submitter
    resolves everything from a single ROP node path; call
    :meth:`set_rop_node_path` (or construct with one) before requesting
    settings/templates.
    """

    def __init__(self, rop_node_path: str = "") -> None:
        self._rop_node_path = rop_node_path

    def set_rop_node_path(self, rop_node_path: str) -> None:
        """Point the submitter at the Deadline Cloud ROP node to submit.

        Lets external callers (e.g. the AYON create/publish plugins) seed the
        node without reaching into the private attribute.
        """
        self._rop_node_path = rop_node_path

    def _get_rop_node(self) -> Optional["hou.Node"]:
        if self._rop_node_path:
            return hou.node(self._rop_node_path)
        return None

    def _require_rop(self) -> "hou.Node":
        rop = self._get_rop_node()
        if rop is None:
            raise RuntimeError(f"Cannot find ROP node at path: {self._rop_node_path}")
        return rop

    def get_settings(self, scan_assets: bool = True) -> HoudiniSubmitterSettings:
        """Collect submission settings from the live scene / ROP node.

        Populates the base-contract fields (job name, frame range, and the
        input/output asset references discovered by walking the scene) so that
        consumers reading them off the returned settings — e.g. the AYON create
        plugin's parameterDefinitions UI and its publish-time job-attachments
        collection — see real values rather than empty defaults. The scene scan
        is the same one the native "Parse Files" button uses.

        ``scan_assets`` (default ``True``, so base-contract callers are
        unchanged) gates the input/output asset-reference scan. Callers that do
        not read ``settings.input_*``/``output_directories`` — notably the GUI
        submit/export path, which writes ``asset_references.yaml`` from the
        dialog-provided references — can pass ``False`` to skip a potentially
        expensive filesystem + USD walk whose output would be discarded.
        """
        settings = HoudiniSubmitterSettings()
        hip_file = _get_hip_file()
        settings.job_name = hou.hipFile.basename() or "Untitled"
        settings.project_path = hou.getenv("HIP", "") or ""
        # Baseline inputs: the hip file only. Overwritten below by the scene scan
        # when it runs and succeeds; otherwise this fallback stands (no ROP, scan
        # skipped, or scan failed).
        settings.input_filenames = [hip_file] if hip_file else []

        # frame_list from the playbar; refined from the ROP's frame-range tuple
        # below when a node is available.
        playbar = hou.playbar.frameRange()
        settings.frame_list = f"{int(playbar[0])}-{int(playbar[1])}"
        settings.output_path = settings.project_path

        rop = self._get_rop_node()
        if rop is not None:
            settings.rop_node_path = rop.path()
            if rop.parm("name"):
                settings.job_name = rop.parm("name").evalAsString() or settings.job_name
            if rop.parm("priority"):
                settings.priority = rop.parm("priority").eval()
            if rop.parm("initial_status"):
                settings.initial_status = rop.parm("initial_status").evalAsString()
            if rop.parm("failed_tasks_limit"):
                settings.max_failed_tasks_count = rop.parm("failed_tasks_limit").eval()
            if rop.parm("task_retry_limit"):
                settings.max_retries_per_task = rop.parm("task_retry_limit").eval()
            if rop.parm("description"):
                settings.description = rop.parm("description").evalAsString()
            if rop.parm("include_adaptor_wheels"):
                settings.include_adaptor_wheels = bool(rop.parm("include_adaptor_wheels").eval())
            if rop.parm("adaptor_wheels"):
                settings.adaptor_wheels = rop.parm("adaptor_wheels").evalAsString()

            frame_range = rop.parmTuple("f")
            if frame_range:
                start = int(frame_range[0].eval())
                end = int(frame_range[1].eval())
                step = int(frame_range[2].eval()) if len(frame_range) > 2 else 1
                settings.frame_list = f"{start}-{end}:{step}" if step != 1 else f"{start}-{end}"

            # Discover the real input/output asset references by scanning the
            # scene (same detection as "Parse Files"), so consumers that read
            # these off the settings get populated directories even when the
            # ROP's Job Attachments multiparms have not been parsed yet. Skipped
            # when scan_assets is False (the caller does not read these fields);
            # on failure the hip-file baseline set above stands.
            if scan_assets:
                try:
                    refs = _get_scene_asset_references(rop)
                    settings.input_filenames = sorted(refs.input_filenames)
                    settings.input_directories = sorted(refs.input_directories)
                    settings.output_directories = sorted(refs.output_directories)
                    if settings.output_directories:
                        settings.output_path = settings.output_directories[0]
                except Exception:
                    # Scene scan can fail (e.g. missing/locked nodes); keep the
                    # hip-file baseline rather than breaking settings collection.
                    pass

        return settings

    def get_job_template(
        self,
        settings: BaseSubmitterSettings,
        host_requirements: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        houdini_settings = cast(HoudiniSubmitterSettings, settings)
        rop = self._require_rop()

        # The step graph (per-step frames, dependencies, wedges) is derived from
        # the live ROP network by _get_job_template and cannot be reconstructed
        # from settings; the job name/description are settings-driven so a
        # consumer's edits are honored (matching deadline-cloud-for-maya).
        job_template = _get_job_template(rop, host_requirements)

        if houdini_settings.job_name:
            job_template["name"] = houdini_settings.job_name
        if houdini_settings.description:
            job_template["description"] = houdini_settings.description
        elif "description" in job_template and not houdini_settings.description:
            # An explicitly-cleared description should not fall back to the node.
            del job_template["description"]

        # Reconcile the adaptor-wheels override with settings so the template's
        # AdaptorWheels parameterDefinition/jobEnvironment stays consistent with
        # the AdaptorWheels *value* get_parameter_values emits (which is
        # settings-driven). _get_job_template added the override based on the
        # node; a headless consumer that edited the wheels settings could
        # otherwise desync (definition without value, or value without
        # definition -> CreateJob reject).
        wheels_enabled = houdini_settings.include_adaptor_wheels and os.path.exists(
            houdini_settings.adaptor_wheels
        )
        _apply_adaptor_wheels_override(job_template, wheels_enabled)

        return job_template

    def get_parameter_values(
        self,
        settings: BaseSubmitterSettings,
        queue_parameters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        houdini_settings = cast(HoudiniSubmitterSettings, settings)
        rop = self._require_rop()

        # Scalar job parameters come from settings (honoring consumer edits); the
        # ROP is still consulted for its per-node queue parameter values and hip
        # file, which have no settings equivalent.
        parameter_values: list[dict[str, Any]] = [
            {"name": "deadline:priority", "value": houdini_settings.priority},
            {"name": "deadline:targetTaskRunStatus", "value": houdini_settings.initial_status},
            {
                "name": "deadline:maxFailedTasksCount",
                "value": houdini_settings.max_failed_tasks_count,
            },
            {"name": "deadline:maxRetriesPerTask", "value": houdini_settings.max_retries_per_task},
            {"name": "HipFile", "value": _get_hip_file()},
            *get_queue_parameter_values_as_openjd(rop),
        ]
        if houdini_settings.include_adaptor_wheels and os.path.exists(
            houdini_settings.adaptor_wheels
        ):
            parameter_values.append(
                {"name": "AdaptorWheels", "value": houdini_settings.adaptor_wheels}
            )

        # Merge caller-supplied queue_parameters with caller-wins precedence.
        # The node already emits its own queue parameters (CondaPackages,
        # CondaChannels, ...) via get_queue_parameter_values_as_openjd, and a
        # headless consumer (e.g. AYON) passes the SAME queue parameters here as
        # its resolved/edited values -- so the two sets overlap by design. The
        # caller's value supersedes the node's on a name collision (mirroring how
        # the GUI's set_queue_parameter_values_from_openjd writes dialog edits
        # back onto the node before the bundle is read). A plain de-dup also
        # avoids the duplicate parameterValues that CreateJob rejects.
        #
        # An empty/None caller value is treated as "not provided": consumers such
        # as AYON build this list from get_queue_parameters(), which resolves
        # every parameter to its queue default -- and a queue whose CondaPackages
        # default is empty would otherwise clobber the node's computed value
        # (e.g. "houdini=20.5.* houdini-openjd=0.x.*"). We only let a caller value
        # win when it actually carries a value, so a bare queue default never
        # blanks a populated node parameter. Falsy-but-real values (0, False) are
        # preserved.
        caller_values = {
            param["name"]: param["value"]
            for param in queue_parameters
            if "value" in param and param["value"] not in (None, "")
        }
        merged: list[dict[str, Any]] = [
            {"name": param["name"], "value": caller_values.get(param["name"], param["value"])}
            for param in parameter_values
        ]
        existing_names = {param["name"] for param in merged}
        merged.extend(
            {"name": name, "value": value}
            for name, value in caller_values.items()
            if name not in existing_names
        )
        return merged

    def get_asset_references(self, settings: BaseSubmitterSettings) -> AssetReferences:
        rop = self._require_rop()
        # Scan the scene for the authoritative asset references rather than
        # reading the ROP's (possibly unparsed) Job Attachments multiparms, so a
        # headless consumer gets real input/output paths without a prior
        # "Parse Files" pass. Returns the typed AssetReferences per the
        # BaseSubmitter contract; callers serialize with .to_dict().
        return _get_scene_asset_references(rop)


def _get_houdini_version() -> str:
    return hou.applicationVersionString()


def _get_wedge_render_node(node: hou.Node):
    """Return ROP set as input or parameter to a wedge node

    This ROP may have a network of input ROP nodes that will also be modified
    by the wedge node
    """
    rendernode = None
    if len(node.inputs()) > 0:
        rendernode = node.inputs()[0]
        if rendernode:
            renderpath = rendernode.path()
    if rendernode is None:
        renderpath = node.parm("driver").eval()
        rendernode = node.node(renderpath)
    return rendernode


def _get_steps(node: hou.Node, separate_steps: int):
    """Convert a network of ROP nodes into a list of steps

    Return a list of wedged steps if all the inputs terminate in a valid wedge
    node.
    """
    wedged_steps = _get_wedge_steps(node)
    if wedged_steps is not None:
        # valid wedged network detected
        rop_steps = wedged_steps
    else:
        # standard network
        rop_steps = _get_rop_steps(node)

    if not separate_steps:
        # render the node connected to the deadline cloud node
        # and all its input nodes. The opposite of splitting it
        # up each node by step

        try:
            connected_node = rop_steps[-1]
        except IndexError:
            return []

        # remove deps, as only 1 step
        connected_node.pop("dependency_names", None)
        # remove dependency info from name
        connected_node["name"] = connected_node["rop"]
        rop_steps = [connected_node]

    return rop_steps


def _get_wedge_steps(rop: hou.Node):
    """Convert ancestors of a wedge node into a list of wedged steps"""
    wedge_nodes = []
    # all inputs nodes must be wedge type
    for input_node in rop.inputs():
        if input_node.type().name() != "wedge":
            return None
        else:
            wedge_nodes.append(input_node)
    # wedge inputs must have no ancestor wedge nodes
    for wedge_node in wedge_nodes:
        for node in wedge_node.inputAncestors():
            if node.type().name() == "wedge":
                print("Nested wedge nodes not supported")
                return None
    wedged_steps: list[dict[str, Any]] = []
    for wedge_node in wedge_nodes:
        # houdini uses a prefix to name separate wedge sets
        # NOTE: a prefix name clash will produce invalid job description
        prefix = wedge_node.parm("prefix").eval()
        # compute wedge set
        hm = wedge_node.hdaModule()
        allwedge, stashedparms, errormsg = hm.getwedges(wedge_node)
        rop_node = _get_wedge_render_node(wedge_node)
        # get all the steps driven by the wedge
        rop_steps = _get_rop_steps(rop_node)
        # wedge each list of steps with prefix-wedgenum
        wedgenum = 0
        for _ in allwedge:
            for rop_step in rop_steps:
                wedge = dict(**rop_step)
                # add wedge node and num to use in adaptor
                wedge["wedge_node"] = wedge_node.path()
                wedge["wedgenum"] = str(wedgenum)
                # append wedge suffix to name and dependency names
                suffix = f"{prefix}-{wedgenum}"
                wedge["name"] = f"{rop_step['name']}-{suffix}"
                if "dependency_names" in wedge:
                    dependency_names = [
                        f"{dependency_name}-{suffix}"
                        for dependency_name in wedge["dependency_names"]
                    ]
                    wedge["dependency_names"] = dependency_names
                wedged_steps.append(wedge)
            wedgenum += 1
    return wedged_steps


def _get_rop_steps(rop: hou.Node):
    """
    Parse hscript render command output to steps
    https://www.sidefx.com/docs/houdini/commands/render.html

    Format:
    ```
    <<id>> [ <<dependencies>> ] <<node>> ( <<frames>> )
    ```
    """
    cmd = f"render -p -c -F {rop.path()}"
    out, err = hou.hscript(cmd)
    # hscript emits non-fatal warnings on stderr (e.g. a transient
    # "Local variable 'pdg_input' not found." on the first render evaluation) while still
    # returning a valid step list in `out`. Treat stderr as informational and judge success by
    # whether we got usable output; only fail when the command produced nothing.
    if err and not out.strip():
        raise Exception(f"hscript render: failed to list steps\n\n{str(err)}")
    if err:
        print(f"hscript render reported warnings while listing steps:\n{err}")
    rop_steps: list[dict[str, Any]] = []
    deadline_node_seen = False

    for n in out.split("\n"):
        if not n.strip():
            continue
        # two parts: rops and frame notation
        parts = n.split("\t")
        # id [ deps ] node
        rop_str = parts[0]
        # frames
        frange_part = parts[1]
        frange = frange_part.replace("( ", "")
        frange = frange.replace(" )", "")
        range_parts = frange.split(" ")
        range_ints = [int(f) for f in range_parts]
        # handle single frame
        if len(range_ints) == 1:
            frame = range_ints[0]
            range_ints = [frame, frame, 1]
        rop_parts = rop_str.split(" ")
        # first token is the int id generated by hscript render
        _id = rop_parts[0]
        # full path to rop
        path = rop_parts[-2]
        # section after id lists the dependencies between [ ]
        deps: list[str] = []
        for d in rop_parts[1:-2]:
            if d in ["[", "]"]:
                continue
            # id this depends on
            deps.append(d)

        node = hou.node(path)

        # we only want to skip the single root submission node
        if node.type().name() in ("deadline", "deadline_cloud"):
            if deadline_node_seen:
                raise RuntimeError(
                    "The selected network contains multiple Deadline Cloud nodes. Only a single Deadline Cloud node should be used."
                )
            deadline_node_seen = True
            continue

        step_dict = {
            "id": _id,
            "name": f"{path}-{_id}",
            "dependency_ids": deps,
            "rop": path,
            "wedgenum": "",
            "wedge_node": "",
            "start": range_ints[0],
            "end": range_ints[1],
            "step": range_ints[2],
            "render_strategy": _get_render_strategy_for_node(node),
        }
        rop_steps.append(step_dict)
    # expand full dependency names once the list is complete
    id_steps = {n["id"]: n for n in rop_steps}
    for rop in rop_steps:
        if rop["dependency_ids"]:
            names = [id_steps[n]["name"] for n in rop["dependency_ids"]]
            rop["dependency_names"] = names
    return rop_steps


# .
def _get_render_strategy_for_node(node: hou.Node) -> RenderStrategy:
    """Any changes to this function should be reflected in the user guide: /docs/user-guide.md"""
    render_strategy = RenderStrategy.PARALLEL

    if (
        node.type().nameWithCategory() in ["Driver/geometry", "Sop/rop_geometry"]
        and node.parm("initsim")
        and node.parm("initsim").eval()
    ):
        render_strategy = RenderStrategy.SEQUENTIAL

    if node.parm("deadline_cloud_render_strategy"):
        strategy_string = node.parm("deadline_cloud_render_strategy").evalAsString()
        if strategy_string.upper() == RenderStrategy.SEQUENTIAL.value:
            render_strategy = RenderStrategy.SEQUENTIAL
        elif strategy_string.upper() == RenderStrategy.PARALLEL.value:
            render_strategy = RenderStrategy.PARALLEL
        else:
            raise ValueError(
                f'The node "{node.path()}" has an unexpected value "{strategy_string}" for its "deadline_cloud_render_strategy" parameter. Ensure the value is {RenderStrategy.PARALLEL.value} or {RenderStrategy.SEQUENTIAL.value}'
            )
    return render_strategy


def read_ui_settings_from_node(node: hou.Node) -> HoudiniSubmitterUISettings:
    """Populate a HoudiniSubmitterUISettings from the Deadline Cloud ROP node's parameters.

    This lets the shared SubmitJobToDeadlineDialog (used by the Python panel) open with the
    node's configured values instead of pure dataclass defaults. Parameter names match those
    defined in the HDA DialogScript and read elsewhere in this module (e.g. get_settings).

    Note the deliberate name remapping: the node exposes ``failed_tasks_limit`` /
    ``task_retry_limit``, but deadline-cloud's SharedJobPropertiesWidget reads
    ``max_failed_tasks_count`` / ``max_retries_per_task`` -- the dataclass uses the latter so the
    shared widget picks the values up rather than silently falling back to its own defaults.
    """
    settings = HoudiniSubmitterUISettings()

    settings.name = node.parm("name").evalAsString()
    settings.description = node.parm("description").evalAsString()

    settings.priority = node.parm("priority").eval()
    settings.initial_status = node.parm("initial_status").evalAsString()
    settings.max_failed_tasks_count = node.parm("failed_tasks_limit").eval()
    settings.max_retries_per_task = node.parm("task_retry_limit").eval()

    settings.separate_steps = bool(node.parm("separate_steps").eval())
    settings.auto_unlock_rops = bool(node.parm("auto_unlock_rops").eval())
    settings.auto_parse_hip = bool(node.parm("auto_parse_hip").eval())
    settings.auto_save_hip = bool(node.parm("auto_save_hip").eval())

    settings.include_adaptor_wheels = bool(node.parm("include_adaptor_wheels").eval())
    settings.adaptor_wheels_dir = node.parm("adaptor_wheels").evalAsString()

    return settings


def write_ui_settings_to_node(node: hou.Node, settings: HoudiniSubmitterUISettings) -> None:
    """Persist the shared dialog's job settings back onto the Deadline Cloud ROP node.

    Inverse of read_ui_settings_from_node. Used by the Python panel's submit callback (Approach A)
    so the panel's edits save with the scene and drive the generated bundle, which is still built
    by reading the node's parameters. As in the reader, the dialog's ``max_failed_tasks_count`` /
    ``max_retries_per_task`` map to the node's ``failed_tasks_limit`` / ``task_retry_limit``.

    Frame-range/take parms (``trange``/``f``/``take``) are intentionally NOT written here: those are
    managed directly by the panel's frame controls, and ``settings`` only carries default values
    for them, so writing them would clobber the user's frame selection.
    """
    node.parm("name").set(settings.name)
    node.parm("description").set(settings.description)

    node.parm("priority").set(settings.priority)
    node.parm("initial_status").set(settings.initial_status)
    node.parm("failed_tasks_limit").set(settings.max_failed_tasks_count)
    node.parm("task_retry_limit").set(settings.max_retries_per_task)

    node.parm("separate_steps").set(int(settings.separate_steps))
    node.parm("auto_unlock_rops").set(int(settings.auto_unlock_rops))
    node.parm("auto_parse_hip").set(int(settings.auto_parse_hip))
    node.parm("auto_save_hip").set(int(settings.auto_save_hip))

    node.parm("include_adaptor_wheels").set(int(settings.include_adaptor_wheels))
    node.parm("adaptor_wheels").set(settings.adaptor_wheels_dir or "")


def _follow_fetch_nodes(node: hou.Node) -> hou.Node:
    """Follow a chain of fetch nodes to find the final target node.

    Args:
        node: The starting node, which may or may not be a fetch node

    Returns:
        The final non-fetch node in the chain
    """
    current_node = node
    visited_nodes = set()  # Prevent infinite loops

    while (
        current_node
        and current_node.type().nameWithCategory() == "Driver/fetch"
        and current_node.path() not in visited_nodes
    ):
        visited_nodes.add(current_node.path())
        inner_node = current_node.node(current_node.parm("source").eval())
        if not inner_node:
            break
        current_node = inner_node

    return current_node


def _is_node_locked(rop_path: str) -> bool:
    """Check rop path lineage for any locked nodes.

    Locked nodes can not be driven by the adaptor and must be unlocked before
    submission.
    """
    path_parts = rop_path.split("/")
    for i, n in enumerate(path_parts):
        node_path = "/".join(path_parts[0:i])
        node = hou.node(node_path)
        if not node:
            continue
        if node.isLockedHDA():
            return True
    return False


def _unlock_node(rop_path: str) -> bool:
    """Unlock the first locked node in the path lineage"""
    path_parts = rop_path.split("/")
    for i, _ in enumerate(path_parts):
        node_path = "/".join(path_parts[0:i])
        node = hou.node(node_path)
        if not node:
            continue
        if node.isLockedHDA():
            try:
                node.allowEditingOfContents(propagate=True)
                return True
            except Exception as exc:
                print(f"Failed to unlock: {node_path}")
                print(str(exc))
                return False
    return False


def _get_job_template(
    rop: hou.Node, host_requirements: Optional[Dict[str, Any]] = None
) -> dict[str, Any]:
    separate_steps = rop.parm("separate_steps").eval()
    rop_steps = _get_steps(rop, separate_steps)
    ignore_input_nodes = bool(separate_steps)
    queue_parameter_definitions_json = rop.userData("queue_parameter_definitions")
    parameter_definitions: list[dict[str, Any]] = (
        json.loads(queue_parameter_definitions_json)
        if queue_parameter_definitions_json is not None
        else []
    )
    parameter_definitions.append(
        {
            "name": "HipFile",
            "type": "PATH",
            "objectType": "FILE",
            "dataFlow": "IN",
            "default": _get_hip_file(),
        }
    )
    steps: list[dict[str, Any]] = []
    for node in rop_steps:
        steps.append(_get_step_template(node, ignore_input_nodes))
    job_template = {
        "specificationVersion": "jobtemplate-2023-09",
        "name": rop.parm("name").evalAsString(),
        "parameterDefinitions": parameter_definitions,
        "steps": steps,
    }

    # If host requirements were configured in the dialog's Host Requirements tab, inject them
    # into every step. The UI sets a single set of requirements for the whole job rather than
    # per-step, so the same block is applied to each step (matching the behavior of the
    # deadline-cloud job bundle/CLI submitters).
    if host_requirements:
        for step in steps:
            step["hostRequirements"] = copy.deepcopy(host_requirements)

    description = rop.parm("description").evalAsString()
    if description:
        job_template["description"] = description

    include_adaptor_wheels = rop.parm("include_adaptor_wheels").eval()
    if include_adaptor_wheels:
        adaptor_wheels = rop.parm("adaptor_wheels").evalAsString()
        if os.path.exists(adaptor_wheels):
            override_file = os.path.join(
                os.path.dirname(__file__), "adaptor_override_environment.yaml"
            )
            with open(override_file) as yaml_file:
                override_environment = yaml.safe_load(yaml_file)
                job_template["parameterDefinitions"].extend(
                    override_environment["parameterDefinitions"]
                )
                if "jobEnvironments" not in job_template:
                    job_template["jobEnvironments"] = []
                job_template["jobEnvironments"].append(override_environment["environment"])

    return job_template


def _load_adaptor_override_environment() -> dict[str, Any]:
    override_file = os.path.join(os.path.dirname(__file__), "adaptor_override_environment.yaml")
    with open(override_file) as yaml_file:
        return yaml.safe_load(yaml_file)


# Names contributed by adaptor_override_environment.yaml, used to detect/strip
# the override so the template can be reconciled with the settings-driven state.
_ADAPTOR_OVERRIDE_PARAM_NAMES = {"AdaptorWheels", "OverrideAdaptorName"}
_ADAPTOR_OVERRIDE_ENV_NAME = "OverrideAdaptor"


def _apply_adaptor_wheels_override(job_template: dict[str, Any], enabled: bool) -> None:
    """Add or remove the adaptor-wheels override on ``job_template`` in place.

    Idempotent: makes the AdaptorWheels ``parameterDefinition``s and the
    ``OverrideAdaptor`` ``jobEnvironment`` present when ``enabled`` and absent
    otherwise, regardless of what the node-driven ``_get_job_template`` already
    added. This keeps the template consistent with the settings-driven
    AdaptorWheels parameter *value* emitted by ``get_parameter_values``.
    """
    already_present = any(
        pd.get("name") in _ADAPTOR_OVERRIDE_PARAM_NAMES
        for pd in job_template.get("parameterDefinitions", [])
    )

    if enabled and not already_present:
        override = _load_adaptor_override_environment()
        job_template.setdefault("parameterDefinitions", []).extend(override["parameterDefinitions"])
        job_template.setdefault("jobEnvironments", []).append(override["environment"])
    elif not enabled and already_present:
        job_template["parameterDefinitions"] = [
            pd
            for pd in job_template.get("parameterDefinitions", [])
            if pd.get("name") not in _ADAPTOR_OVERRIDE_PARAM_NAMES
        ]
        job_template["jobEnvironments"] = [
            env
            for env in job_template.get("jobEnvironments", [])
            if env.get("name") != _ADAPTOR_OVERRIDE_ENV_NAME
        ]
        if not job_template["jobEnvironments"]:
            del job_template["jobEnvironments"]


def _get_step_template(node: Dict, ignore_input_nodes: bool):
    init_data = {
        "scene_file": "{{Param.HipFile}}",
        "render_node": node["rop"],
        "version": _get_houdini_version(),
        "ignore_input_nodes": ignore_input_nodes,
        "wedgenum": node["wedgenum"],
        "wedge_node": node["wedge_node"],
    }
    init_data_attachment = {
        "name": "initData",
        "filename": "init-data.yaml",
        "type": "TEXT",
        # Convert to dict to YAML string using the prettier nested object format
        "data": yaml.safe_dump(init_data, default_flow_style=False),
    }

    environments = get_houdini_environments(init_data_attachment)

    task_data_dict = {"render_node": node["rop"], "ignore_input_nodes": ignore_input_nodes}

    if node["render_strategy"] == RenderStrategy.SEQUENTIAL:
        # Generate a single task that renders the whole frame range
        parameter_space = None
        task_data_dict["frame_range"] = {
            "start": node["start"],
            "end": node["end"],
            "step": node["step"],
        }
    else:
        # Generate one task per frame using step parameters
        range_expression = "{start}-{end}:{step}".format(**node)
        parameter_space = {
            "taskParameterDefinitions": [
                {"name": "Frame", "range": range_expression, "type": "INT"}
            ]
        }
        task_data_dict["frame_range"] = {
            "start": "{{Task.Param.Frame}}",
            "end": "{{Task.Param.Frame}}",
            "step": 1,
        }

    task_data = yaml.safe_dump(task_data_dict, default_flow_style=False)
    # Remove single quotes around the frame parameter so it gets interpreted as a int and not a string
    task_data = task_data.replace("'{{Task.Param.Frame}}'", "{{Task.Param.Frame}}")

    step = {
        "name": node["name"],
        "stepEnvironments": environments,
        "script": {
            "embeddedFiles": [
                {
                    "name": "runData",
                    "filename": "run-data.yaml",
                    "type": "TEXT",
                    "data": task_data,
                },
            ],
            "actions": {
                "onRun": {
                    "command": "houdini-openjd",
                    "args": [
                        "daemon",
                        "run",
                        "--connection-file",
                        "{{ Session.WorkingDirectory }}/connection.json",
                        "--run-data",
                        "file://{{ Task.File.runData }}",
                    ],
                    "cancelation": {
                        "mode": "NOTIFY_THEN_TERMINATE",
                    },
                },
            },
        },
    }

    if parameter_space:
        step["parameterSpace"] = parameter_space

    if "dependency_names" in node:
        deps = [{"dependsOn": d} for d in node["dependency_names"]]
        step["dependencies"] = deps
    return step


def _create_job_bundle(
    rop_node: hou.Node,
    job_bundle_dir: str,
    asset_references: AssetReferences,
    host_requirements: Optional[Dict[str, Any]] = None,
) -> None:
    # Drive the unified HoudiniSubmitter engine so the GUI submit/export path
    # and headless consumers (e.g. AYON) build the template + parameter values
    # through one implementation. Pass the submitter's own get_settings() (read
    # from this ROP) so the settings-driven builders see the node's real values;
    # the produced bundle matches what the node configures.
    # The dialog-provided asset_references are written as-is (they carry the
    # user's Job Attachments edits) rather than re-scanned, so scan_assets=False
    # skips get_settings()'s scene/USD asset walk whose output this path would
    # only discard (the dialog already scanned the scene once when it opened).
    submitter = HoudiniSubmitter(rop_node.path())
    settings = submitter.get_settings(scan_assets=False)
    job_template = submitter.get_job_template(settings, host_requirements)
    parameter_values = submitter.get_parameter_values(settings, [])

    job_bundle_path = Path(job_bundle_dir)
    with open(job_bundle_path / "template.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(job_template, f, indent=1)
    with open(job_bundle_path / "parameter_values.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump({"parameterValues": parameter_values}, f, indent=1)
    with open(job_bundle_path / "asset_references.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(asset_references.to_dict(), f, indent=1)


def _pre_gui_hook_confirm_callback(parent):
    """Choose the confirmation callback for pre-GUI hooks based on the auto_accept setting.

    Returns ``None`` (run hooks without prompting) when ``settings.auto_accept`` is enabled,
    otherwise the standard Qt confirmation dialog from ``qt_hook_confirmation``. Kept as a small
    helper here (rather than inline in the panel) so the auto_accept branch can be unit-tested
    headlessly -- the panel module isn't importable outside Houdini.
    """
    if str2bool(get_setting("settings.auto_accept")):
        return None

    from deadline.client.ui.pre_gui_hooks import qt_hook_confirmation

    return qt_hook_confirmation(parent)


def callback(kwargs):
    """ROP parameter callback wrapper"""
    function_name = f"{kwargs['parm'].name()}_callback"
    globals()[function_name](kwargs)


def parse_files_callback(kwargs):
    node = kwargs["node"]
    _parse_files(node)


def save_bundle_callback(kwargs):
    node = kwargs["node"]
    name = node.parm("name").evalAsString()
    asset_references = _get_evaluated_asset_references(node)
    try:
        job_bundle_dir = create_job_history_bundle_dir("houdini", name)
        _create_job_bundle(node, job_bundle_dir, asset_references)
        print("Saved the submission as a job bundle:")
        print(job_bundle_dir)
        if sys.platform == "win32":
            os.startfile(job_bundle_dir)
        hou.ui.displayMessage(
            f"Saved the submission as a job bundle: {job_bundle_dir}",
            title="Deadline Cloud Job Submission",
        )
    except Exception as exc:
        print("Error saving bundle")
        hou.ui.displayMessage(
            str(exc),
            title="Deadline Cloud Job Submission",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )


def _run_pre_submission_checks(
    node: hou.Node,
    *,
    auto_unlock_rops: Optional[bool] = None,
    auto_parse_hip: Optional[bool] = None,
    auto_save_hip: Optional[bool] = None,
) -> None:
    """Run the interactive pre-submission safety checks shared by every submission entry point.

    Both submission entry points -- the ROP "Submit" button (``submit_callback``) and the Python
    Panel -- open the shared ``SubmitJobToDeadlineDialog`` and submit through the same guarded
    Submit button (see ``_install_pre_submission_gate``). Running these checks from that gate
    guarantees they are enforced consistently by both paths (and on every submission from the
    long-lived panel), rather than only by the ROP button.

    The checks prompt the user to resolve locked input ROPs (for example Karma), refresh the parsed
    file references when the hip file is missing from them, and save unsaved scene changes before
    the job bundle is built and uploaded. Each prompt is skipped when the corresponding
    ``auto_unlock_rops`` / ``auto_parse_hip`` / ``auto_save_hip`` toggle is enabled.

    The ``auto_*`` keyword arguments carry the user's *current* choices from the dialog's Scene
    Settings tab. They must be passed because the dialog only writes those toggles back to the node
    *after* submission begins -- reading them from the node here would use stale values and ignore
    the user's latest changes (e.g. unchecking "Automatically save scene"). When an argument is
    ``None`` the corresponding node parameter is used as a fallback (e.g. for direct/legacy callers
    and tests).

    Raises:
        UserInitiatedCancel: if the render node has no input ROPs, or the user chooses "Cancel" at
            any prompt. Raising (rather than returning a status) lets the shared dialog abort the
            submission gracefully via its own ``UserInitiatedCancel`` handling.
    """
    # Imported lazily: this runs only while the Qt dialog is active, so importing here keeps the
    # module import path free of the deadline-cloud UI dependency for headless hython.
    from deadline.client.exceptions import UserInitiatedCancel

    def _toggle(override: Optional[bool], parm_name: str) -> bool:
        # Prefer the value supplied by the caller (the dialog's current checkbox state); otherwise
        # fall back to the node parameter.
        if override is not None:
            return bool(override)
        return bool(node.parm(parm_name).eval())

    all_inputs = node.inputAncestors()

    if not all_inputs:
        # there are no inputs to the AWS Deadline Cloud render node
        raise UserInitiatedCancel(
            "The AWS Deadline Cloud render node (ROP) must have an input ROP specified to submit a job"
        )

    asset_references = _get_evaluated_asset_references(node)

    # check for locked rops, Karma for example
    locked_rops = []
    for n in all_inputs:

        # Check for and follow any fetch nodes
        n = _follow_fetch_nodes(n)

        node_path = n.path()
        if _is_node_locked(node_path):
            locked_rops.append(node_path)

    if locked_rops:
        auto_unlock = _toggle(auto_unlock_rops, "auto_unlock_rops")
        if not auto_unlock:
            buttons = (
                "Unlock and save",
                "Always unlock and save",
                "Ignore",
                "Cancel",
            )
            unlock_choice = hou.ui.displayMessage(
                "Locked ROPs found in network",
                title="Warning",
                buttons=buttons,
                details="\n".join(locked_rops),
            )
            if unlock_choice == 3:
                raise UserInitiatedCancel("Submission canceled.")
            if unlock_choice == 0:
                for n in locked_rops:
                    _unlock_node(n)
                hou.hipFile.save()
            if unlock_choice == 1:
                for n in locked_rops:
                    _unlock_node(n)
                node.parm("auto_unlock_rops").set(1)
                hou.hipFile.save()
        else:
            for n in locked_rops:
                _unlock_node(n)
            hou.hipFile.save()

    # check hip is listed in input_filenames
    hip_file = _get_hip_file()
    hip_input = hip_file in asset_references.input_filenames
    if not hip_input:
        auto_parse = _toggle(auto_parse_hip, "auto_parse_hip")
        if not auto_parse:
            buttons = (
                "Parse and save",
                "Always parse and save",
                "Ignore",
                "Cancel",
            )
            parse_choice = hou.ui.displayMessage(
                "Hip file not found in file references", title="Warning", buttons=buttons
            )
            if parse_choice == 3:
                raise UserInitiatedCancel("Submission canceled.")
            if parse_choice == 0:
                _parse_files(node)
                hou.hipFile.save()
            if parse_choice == 1:
                _parse_files(node)
                node.parm("auto_parse_hip").set(1)
                hou.hipFile.save()
        else:
            _parse_files(node)
            hou.hipFile.save()

    # check for unsaved changes
    hip_unsaved = hou.hipFile.hasUnsavedChanges()
    if hip_unsaved:
        auto_save = _toggle(auto_save_hip, "auto_save_hip")
        if not auto_save:
            buttons = (
                "Save",
                "Always save",
                "Ignore",
                "Cancel",
            )
            save_choice = hou.ui.displayMessage(
                "Hip file has unsaved changes", title="Warning", buttons=buttons
            )
            if save_choice == 3:
                raise UserInitiatedCancel("Submission canceled.")
            if save_choice == 0:
                hou.hipFile.save()
            if save_choice == 1:
                node.parm("auto_save_hip").set(1)
                hou.hipFile.save()
        else:
            hou.hipFile.save()


def _make_create_bundle_callback(rop_node):
    """
    Create an on_create_job_bundle_callback compatible with SubmitJobToDeadlineDialog's Protocol.

    The returned function captures the ROP node and delegates to _create_job_bundle
    when the dialog invokes it during submission or bundle export.
    """

    def _on_create_job_bundle(
        widget,
        job_bundle_dir: str,
        settings,
        queue_parameters: list,
        asset_references: AssetReferences,
        host_requirements: Optional[Dict[str, Any]] = None,
        *,
        purpose,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        # Approach A: persist the dialog's edits back onto the node so they save with the scene and
        # drive the generated bundle (which is still built by reading the node). Frame range/take
        # are written by the panel's own frame controls, so they're excluded from
        # write_ui_settings_to_node.
        #
        # NOTE: the pre-submission safety checks (locked ROPs / file parse / unsaved-scene save) are
        # deliberately NOT run here. By the time the dialog invokes this callback it has already
        # shown its modal submission-progress dialog, which suppresses the hou.ui.displayMessage
        # prompts (they never appear). The checks instead run when the Submit button is clicked,
        # before on_submit -- see _install_pre_submission_gate.
        write_ui_settings_to_node(rop_node, settings)
        set_queue_parameter_values_from_openjd(rop_node, queue_parameters)
        _create_job_bundle(rop_node, job_bundle_dir, asset_references, host_requirements)
        return None

    return _on_create_job_bundle


def _install_pre_submission_gate(dialog, node) -> None:
    """Rewire the dialog's Submit button so the pre-submission checks run BEFORE submission.

    The shared ``SubmitJobToDeadlineDialog`` wires its Submit button directly to ``on_submit``,
    which immediately shows a modal submission-progress dialog and only then invokes the job-bundle
    callback. Running the interactive checks (locked ROPs / file parse / unsaved-scene save) from
    that callback is too late: the modal progress dialog is already on screen and suppresses the
    ``hou.ui.displayMessage`` prompts, so the user never sees them.

    Instead we intercept the Submit button's ``clicked`` signal (mirroring the login-signal
    rewiring in the Python Panel) so the checks -- and the chance to cancel -- happen before
    ``on_submit`` runs. This gate is shared by both submission entry points: the ROP "Submit" button
    (``submit_callback``) and the Python Panel.
    """
    from deadline.client.exceptions import UserInitiatedCancel

    def _guarded_submit() -> None:
        # Read the user's CURRENT auto_* choices from the dialog's Scene Settings tab. The dialog
        # only writes these toggles back to the node during on_submit (after this gate runs), so
        # reading the node here would use stale values -- e.g. the user unchecking "Automatically
        # save scene" would be ignored. Fall back to the node parms if the widget is missing or its
        # structure changes.
        toggles: Dict[str, Any] = {}
        scene_widget = getattr(dialog, "job_settings", None)
        if scene_widget is not None:
            try:
                toggles = {
                    "auto_unlock_rops": scene_widget.auto_unlock_rops_check.isChecked(),
                    "auto_parse_hip": scene_widget.auto_parse_hip_check.isChecked(),
                    "auto_save_hip": scene_widget.auto_save_hip_check.isChecked(),
                }
            except AttributeError:
                toggles = {}

        try:
            _run_pre_submission_checks(node, **toggles)
        except UserInitiatedCancel:
            # The node has no inputs, or the user chose "Cancel" at a prompt: abort quietly without
            # submitting. The dialog stays open so the user can adjust and try again.
            return
        dialog.on_submit()

    # Disconnect the default Submit->on_submit wiring and route through the guarded wrapper. Fall
    # back to clearing all slots if the bound-method match fails (mirrors the login rewiring).
    try:
        dialog.submit_button.clicked.disconnect(dialog.on_submit)
    except (TypeError, RuntimeError):
        dialog.submit_button.clicked.disconnect()
    dialog.submit_button.clicked.connect(_guarded_submit)
    # Keep a strong reference to the closure on the dialog so PySide does not garbage-collect the
    # connected slot (a known binding gotcha where connections to bare Python closures can silently
    # drop once the local goes out of scope).
    dialog._deadline_guarded_submit = _guarded_submit  # type: ignore[attr-defined]


def submit_callback(kwargs):
    node = kwargs["node"]

    # Early guard: an inputless node cannot produce a job, so fail fast before opening the dialog.
    # _run_pre_submission_checks re-checks this at submission time to also cover the Python Panel
    # path, which opens the dialog directly without going through submit_callback.
    if not node.inputAncestors():
        hou.ui.displayMessage(
            "The AWS Deadline Cloud render node (ROP) must have an input ROP specified to submit a job",
            title="Missing Input Render Node",
            severity=hou.severityType.Warning,
        )
        return

    try:
        # Initialize telemetry client, opt-out is respected
        api.get_deadline_cloud_library_telemetry_client().update_common_details(
            {
                "deadline-cloud-for-houdini-submitter-version": version,
                "houdini-version": _get_houdini_version(),
            }
        )
        farm_id = get_setting("defaults.farm_id")
        if not farm_id:
            hou.ui.displayMessage(
                "Please configure the farm ID in the AWS Deadline Cloud render node (ROP) settings",
                title="Farm ID Required",
                severity=hou.severityType.Warning,
            )
            return

        queue_id = get_setting("defaults.queue_id")
        if not queue_id:
            hou.ui.displayMessage(
                "Please configure the queue ID in the AWS Deadline Cloud render node (ROP) settings",
                title="Queue ID Required",
                severity=hou.severityType.Warning,
            )
            return

        ui_settings = read_ui_settings_from_node(node)

        shared_parameter_values: dict = {
            "CondaPackages": get_default_conda_packages(),
            "RezPackages": get_default_rez_packages(),
        }

        # Lazy import: keep deadline-cloud's Qt/PySide UI out of the module-import path so
        # headless hython (e.g. integ tests importing _create_job_bundle) does not pull in Qt.
        from deadline.client.ui.dialogs.submit_job_to_deadline_dialog import (
            SubmitJobToDeadlineDialog,
        )
        from .houdini_submitter_widget import SceneSettingsWidget
        from qtpy.QtCore import Qt  # type: ignore

        dialog = SubmitJobToDeadlineDialog(
            job_setup_widget_type=SceneSettingsWidget,
            initial_job_settings=ui_settings,
            initial_shared_parameter_values=shared_parameter_values,
            auto_detected_attachments=_get_scene_asset_references(node),
            attachments=AssetReferences(),
            on_create_job_bundle_callback=_make_create_bundle_callback(node),
            submitter_info=SubmitterInfo(
                submitter_name="Houdini",
                submitter_package_name="deadline-cloud-for-houdini",
                submitter_package_version=version,
                host_application_name="Houdini",
                host_application_version=hou.applicationVersionString(),
            ),
            f=Qt.Tool,
            show_host_requirements_tab=True,
            use_deadline_cloud_v2_channel=True,
            parent=hou.qt.mainWindow(),
        )
        # Run the pre-submission checks when Submit is clicked, before the dialog opens its modal
        # progress dialog (which would otherwise hide the hou.ui.displayMessage prompts).
        _install_pre_submission_gate(dialog, node)
        dialog.exec_()
    except Exception as exc:
        api.get_deadline_cloud_library_telemetry_client().record_error(
            event_details={"exception_scope": "caught", "error_operation": "submit_callback"},
            exception_type=str(type(exc)),
            from_gui=True,
        )
        print(str(exc))
        hou.ui.displayMessage(
            str(exc),
            title="Deadline Cloud Job Submission",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )


def settings_callback(kwargs):
    node = kwargs["node"]
    _show_farm_and_queue_as_refreshing(node)
    # Lazy import: keep deadline-cloud's Qt/PySide UI out of the module-import path so
    # headless hython (e.g. integ tests importing _create_job_bundle) does not pull in Qt.
    from deadline.client.ui.dialogs import DeadlineConfigDialog

    DeadlineConfigDialog.configure_settings(parent=hou.qt.mainWindow())
    try:
        _apply_farm_and_queue_settings(node)
    except Exception as exc:
        hou.ui.displayMessage(
            str(exc),
            title="Deadline Cloud",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )


def login_callback(kwargs):
    node = kwargs["node"]
    _show_farm_and_queue_as_refreshing(node)
    # Lazy import: keep deadline-cloud's Qt/PySide UI out of the module-import path so
    # headless hython (e.g. integ tests importing _create_job_bundle) does not pull in Qt.
    from deadline.client.ui.dialogs import DeadlineLoginDialog

    DeadlineLoginDialog.login(parent=hou.qt.mainWindow())
    try:
        _apply_farm_and_queue_settings(node)
    except Exception as exc:
        hou.ui.displayMessage(
            str(exc),
            title="Deadline Cloud",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )


def logout_callback(kwargs):
    node = kwargs["node"]
    node.parm("farm").set(_NONE_SELECTED_TEXT)
    node.parm("queue").set(_NONE_SELECTED_TEXT)
    api.logout()


def update_queue_parameters_callback(kwargs):
    node = kwargs["node"]
    _show_farm_and_queue_as_refreshing(node)
    _apply_farm_and_queue_settings(node)


def _show_farm_and_queue_as_refreshing(node):
    node.parm("farm").set(_REFRESHING_TEXT)
    node.parm("queue").set(_REFRESHING_TEXT)


def _apply_farm_and_queue_settings(node):
    farm_id = get_setting("defaults.farm_id")
    if not farm_id:
        node.parm("farm").set(_NONE_SELECTED_TEXT)
        node.parm("queue").set(_NONE_SELECTED_TEXT)
        return
    deadline = api.get_boto3_client("deadline")
    try:
        farm_response = deadline.get_farm(farmId=farm_id)
        node.parm("farm").set(farm_response["displayName"])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "AccessDeniedException":
            node.parm("farm").set(farm_id)
        else:
            raise
    queue_id = get_setting("defaults.queue_id")
    if not queue_id:
        node.parm("queue").set(_NONE_SELECTED_TEXT)
        return
    queue_response = deadline.get_queue(farmId=farm_id, queueId=queue_id)
    node.parm("queue").set(queue_response["displayName"])
    update_queue_parameters(farm_id, queue_id, node)


# TODO: remove this and swap to default job template
def get_houdini_environments(init_data_attachment: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns a list of environments that set things up to run frame renders
    for the specified DCC.
    """
    return [
        {
            "name": "Houdini",
            "description": "Runs Houdini in the background.",
            "script": {
                "embeddedFiles": [
                    init_data_attachment,
                ],
                "actions": {
                    "onEnter": {
                        "command": "houdini-openjd",
                        "args": [
                            "daemon",
                            "start",
                            "--path-mapping-rules",
                            "file://{{Session.PathMappingRulesFile}}",
                            "--connection-file",
                            "{{ Session.WorkingDirectory }}/connection.json",
                            "--init-data",
                            "file://{{ Env.File.initData }}",
                        ],
                        "cancelation": {
                            "mode": "NOTIFY_THEN_TERMINATE",
                        },
                        "timeout": 400,
                    },
                    "onExit": {
                        "command": "houdini-openjd",
                        "args": [
                            "daemon",
                            "stop",
                            "--connection-file",
                            "{{ Session.WorkingDirectory }}/connection.json",
                        ],
                        "cancelation": {
                            "mode": "NOTIFY_THEN_TERMINATE",
                        },
                        "timeout": 120,
                    },
                },
            },
        }
    ]
