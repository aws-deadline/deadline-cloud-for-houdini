# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import sys
import yaml
import json
from typing import Any
from pathlib import Path

from deadline.client.job_bundle._yaml import deadline_yaml_dump
from deadline.client import api
from deadline.client.job_bundle.submission import AssetReferences
from deadline.client.job_bundle import create_job_history_bundle_dir
from deadline.client.job_bundle.parameters import JobParameter
from deadline.client.config import get_setting
from deadline.client.config.config_file import str2bool
from deadline.client.ui.dialogs.submit_job_progress_dialog import SubmitJobProgressDialog
from deadline.client.ui.dialogs import DeadlineConfigDialog, DeadlineLoginDialog
from deadline.job_attachments.upload import S3AssetManager
from deadline.job_attachments.models import JobAttachmentS3Settings

from .queue_parameters import update_queue_parameters, get_queue_parameter_values_as_openjd
from ._assets import _get_hip_file, _get_asset_references, _parse_files

# For temporary backwards compatibility
from ._assets import (
    _IGNORE_REF_PARMS as IGNORE_REF_PARMS,  # noqa
    _IGNORE_REF_VALUES as IGNORE_REF_VALUES,  # noqa
)
from ._version import version

import hou


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


def _get_steps(node: hou.Node):
    """Convert a network of ROP nodes into a list of steps

    Return a list of wedged steps if all the inputs terminate in a valid wedge
    node.
    """
    wedged_steps = _get_wedge_steps(node)
    if wedged_steps is not None:
        # valid wedged network detected
        return wedged_steps
    else:
        # standard network
        return _get_rop_steps(node)


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
                wedge["wedgenum"] = wedgenum
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
    if err:
        raise Exception(f"hscript render: failed to list steps\n\n{str(err)}")
    rop_steps: list[dict[str, Any]] = []
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
        # skip deadline and deadline_cloud rops
        if hou.node(path).type().name() in ("deadline", "deadline_cloud"):
            continue
        step_dict = {
            "id": _id,
            "name": f"{path}-{_id}",
            "dependency_ids": deps,
            "rop": path,
            "wedgenum": "",
            "wedge_node": "",
            "start": range_ints[0],
            "stop": range_ints[1],
            "step": range_ints[2],
        }
        rop_steps.append(step_dict)
    # expand full dependency names once the list is complete
    id_steps = {n["id"]: n for n in rop_steps}
    for rop in rop_steps:
        if rop["dependency_ids"]:
            names = [id_steps[n]["name"] for n in rop["dependency_ids"]]
            rop["dependency_names"] = names
    return rop_steps


def _get_parameter_values(node: hou.Node) -> dict[str, Any]:
    priority = node.parm("priority").eval()
    initial_status = node.parm("initial_status").evalAsString()
    failed_tasks_limit = node.parm("failed_tasks_limit").eval()
    task_retry_limit = node.parm("task_retry_limit").eval()
    parameter_values = [
        {"name": "deadline:priority", "value": priority},
        {"name": "deadline:targetTaskRunStatus", "value": initial_status},
        {"name": "deadline:maxFailedTasksCount", "value": failed_tasks_limit},
        {"name": "deadline:maxRetriesPerTask", "value": task_retry_limit},
        {"name": "HipFile", "value": _get_hip_file()},
        *get_queue_parameter_values_as_openjd(node),
    ]

    if node.parm("include_adaptor_wheels").eval():
        parameter_values.append(
            {"name": "AdaptorWheels", "value": node.parm("adaptor_wheels").evalAsString()}
        )

    return {"parameterValues": parameter_values}


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


def _get_job_template(rop: hou.Node) -> dict[str, Any]:
    job_name = rop.parm("name").evalAsString()
    job_description = rop.parm("description").evalAsString()
    separate_steps = rop.parm("separate_steps").eval()
    rop_steps = _get_steps(rop)
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
    ignore_input_nodes = "true"
    if not separate_steps:
        # render the node connected to the deadline cloud node
        # and all its input nodes. The opposite of splitting it
        # up each node by step
        connected_node = rop_steps[-1]
        # remove deps, as only 1 step
        connected_node.pop("dependency_names", None)
        # remove dependency info from name
        connected_node["name"] = connected_node["rop"]
        rop_steps = [connected_node]
        ignore_input_nodes = "false"
    for node in rop_steps:
        # init data
        init_data_contents = []
        init_data_contents.append("scene_file: '{{Param.HipFile}}'\n")
        init_data_contents.append(f"render_node: '{node['rop']}'\n")
        init_data_contents.append(f"version: {_get_houdini_version()}\n")
        init_data_contents.append(f"ignore_input_nodes: {ignore_input_nodes}\n")
        init_data_contents.append(f"wedgenum: '{node['wedgenum']}'\n")
        init_data_contents.append(f"wedge_node: '{node['wedge_node']}'\n")
        init_data_attachment = {
            "name": "initData",
            "filename": "init-data.yaml",
            "type": "TEXT",
            "data": "".join(init_data_contents),
        }
        # environments
        environments = get_houdini_environments(init_data_attachment)
        # task run data
        task_data_contents = []
        task_data_contents.append(f"render_node: {node['rop']}\n")
        task_data_contents.append("frame: {{Task.Param.Frame}}\n")
        task_data_contents.append(f"ignore_input_nodes: {ignore_input_nodes}\n")
        # step
        frame_range = "{start}-{stop}:{step}".format(**node)
        step = {
            "name": node["name"],
            "parameterSpace": {
                "taskParameterDefinitions": [{"name": "Frame", "range": frame_range, "type": "INT"}]
            },
            "stepEnvironments": environments,
            "script": {
                "embeddedFiles": [
                    {
                        "name": "runData",
                        "filename": "run-data.yaml",
                        "type": "TEXT",
                        "data": "".join(task_data_contents),
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
        if "dependency_names" in node:
            deps = [{"dependsOn": d} for d in node["dependency_names"]]
            step["dependencies"] = deps
        steps.append(step)
    job_template = {
        "specificationVersion": "jobtemplate-2023-09",
        "name": job_name,
        "parameterDefinitions": parameter_definitions,
        "steps": steps,
    }
    if job_description:
        job_template["description"] = job_description
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


def _create_job_bundle(
    rop_node: hou.Node, job_bundle_dir: str, asset_references: AssetReferences
) -> None:
    job_bundle_path = Path(job_bundle_dir)
    job_template = _get_job_template(rop_node)
    parameter_values = _get_parameter_values(rop_node)
    with open(job_bundle_path / "template.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(job_template, f, indent=1)
    with open(job_bundle_path / "parameter_values.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(parameter_values, f, indent=1)
    with open(job_bundle_path / "asset_references.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(asset_references.to_dict(), f, indent=1)


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
    asset_references = _get_asset_references(node)
    try:
        job_bundle_dir = create_job_history_bundle_dir("houdini", name)
        _create_job_bundle(node, job_bundle_dir, asset_references)
        print("Saved the submission as a job bundle:")
        print(job_bundle_dir)
        if sys.platform == "win32":
            os.startfile(job_bundle_dir)
        hou.ui.displayMessage(
            f"Saved the submission as a job bundle: {job_bundle_dir}",
            title="Houdini Job Submission",
        )
    except Exception as exc:
        print("Error saving bundle")
        hou.ui.displayMessage(
            str(exc), title="Houdini Job Submission", severity=hou.severityType.Warning
        )


def submit_callback(kwargs):
    node = kwargs["node"]
    all_inputs = node.inputAncestors()

    if not all_inputs:
        # there are no inputs to the AWS Deadline Cloud render node
        hou.ui.displayMessage(
            "The AWS Deadline Cloud render node (ROP) must have an input ROP specified to submit a job",
            title="Missing Input Render Node",
            severity=hou.severityType.Warning,
        )
        return

    name = node.parm("name").evalAsString()
    # TODO: Populate from queue environment so that parameters can be overridden.
    queue_parameters: list[JobParameter] = []
    asset_references = _get_asset_references(node)

    # check for locked rops, Karma for example
    locked_rops = []
    for n in all_inputs:
        node_path = n.path()
        if _is_node_locked(node_path):
            locked_rops.append(node_path)

    if locked_rops:
        auto_unlock = node.parm("auto_unlock_rops").eval()
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
                print("user Cancelled")
                return
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
        auto_parse = node.parm("auto_parse_hip").eval()
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
                print("user Cancelled")
                return
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
        auto_save = node.parm("auto_save_hip").eval()
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
                print("user Cancelled")
                return
            if save_choice == 0:
                hou.hipFile.save()
            if save_choice == 1:
                node.parm("auto_save_hip").set(1)
                hou.hipFile.save()
        else:
            hou.hipFile.save()

    try:
        # Initialize telemetry client, opt-out is respected
        api.get_deadline_cloud_library_telemetry_client().update_common_details(
            {
                "deadline-cloud-for-houdini-submitter-version": version,
                "houdini-version": _get_houdini_version(),
            }
        )
        deadline = api.get_boto3_client("deadline")

        job_bundle_dir = create_job_history_bundle_dir("houdini", name)
        _create_job_bundle(node, job_bundle_dir, asset_references)

        farm_id = get_setting("defaults.farm_id")
        queue_id = get_setting("defaults.queue_id")
        storage_profile_id = get_setting("settings.storage_profile_id")

        storage_profile = None
        if storage_profile_id:
            storage_profile = api.get_storage_profile_for_queue(
                farm_id, queue_id, storage_profile_id, deadline
            )

        queue = deadline.get_queue(farmId=farm_id, queueId=queue_id)

        queue_role_session = api.get_queue_user_boto3_session(
            deadline=deadline,
            farm_id=farm_id,
            queue_id=queue_id,
            queue_display_name=queue["displayName"],
        )

        asset_manager = S3AssetManager(
            farm_id=farm_id,
            queue_id=queue_id,
            job_attachment_settings=JobAttachmentS3Settings(**queue["jobAttachmentSettings"]),
            session=queue_role_session,
        )

        job_progress_dialog = SubmitJobProgressDialog(parent=hou.qt.mainWindow())
        job_progress_dialog.start_submission(
            farm_id,
            queue_id,
            storage_profile,
            job_bundle_dir,
            queue_parameters,
            asset_manager,
            deadline,
            auto_accept=str2bool(get_setting("settings.auto_accept")),
        )
    except Exception as exc:
        print(str(exc))
        hou.ui.displayMessage(
            str(exc), title="Houdini Job Submission", severity=hou.severityType.Warning
        )


def settings_callback(kwargs):
    node = kwargs["node"]
    node.parm("farm").set("<refreshing>")
    node.parm("queue").set("<refreshing>")
    DeadlineConfigDialog.configure_settings(parent=hou.qt.mainWindow())
    deadline = api.get_boto3_client("deadline")
    farm_id = get_setting("defaults.farm_id")
    farm_response = deadline.get_farm(farmId=farm_id)
    node.parm("farm").set(farm_response["displayName"])
    queue_id = get_setting("defaults.queue_id")
    queue_response = deadline.get_queue(farmId=farm_id, queueId=queue_id)
    node.parm("queue").set(queue_response["displayName"])
    update_queue_parameters(farm_id, queue_id, node)


def login_callback(kwargs):
    node = kwargs["node"]
    node.parm("farm").set("<refreshing>")
    node.parm("queue").set("<refreshing>")
    DeadlineLoginDialog.login(parent=hou.qt.mainWindow())
    deadline = api.get_boto3_client("deadline")
    farm_id = get_setting("defaults.farm_id")
    farm_response = deadline.get_farm(farmId=farm_id)
    node.parm("farm").set(farm_response["displayName"])
    queue_id = get_setting("defaults.queue_id")
    queue_response = deadline.get_queue(farmId=farm_id, queueId=queue_id)
    node.parm("queue").set(queue_response["displayName"])
    update_queue_parameters(farm_id, queue_id, node)


def logout_callback(kwargs):
    node = kwargs["node"]
    node.parm("farm").set("")
    node.parm("queue").set("")
    api.logout()


def update_queue_parameters_callback(kwargs):
    node = kwargs["node"]
    farm_id = get_setting("defaults.farm_id")
    queue_id = get_setting("defaults.queue_id")
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
                    },
                },
            },
        }
    ]
