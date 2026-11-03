# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Job settings, parameter values, environments, and bundle files shared by the submitters.

It imports neither ``hou`` nor ``pxr``, so code outside a Houdini session can use and test it.
A node only needs ``parm(name)`` and ``userData(name)``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import yaml

# Only the client library's settings, YAML writer, and asset types; no Houdini modules.
from deadline.client.api import BaseSubmitterSettings, SubmissionContext
from deadline.client.job_bundle._yaml import deadline_yaml_dump


def read_common_job_settings(node: Any, settings: BaseSubmitterSettings) -> None:
    """Copy the job parms a node shares with the ROP onto ``settings``, skipping absent parms."""
    # Each parm is optional, so a node without one keeps the setting's current value.
    if node.parm("name"):
        settings.job_name = node.parm("name").evalAsString() or settings.job_name
    if node.parm("priority"):
        settings.priority = node.parm("priority").eval()
    if node.parm("initial_status"):
        settings.initial_status = node.parm("initial_status").evalAsString()
    if node.parm("failed_tasks_limit"):
        settings.max_failed_tasks_count = node.parm("failed_tasks_limit").eval()
    if node.parm("task_retry_limit"):
        settings.max_retries_per_task = node.parm("task_retry_limit").eval()
    if node.parm("description"):
        settings.description = node.parm("description").evalAsString()
    # The ROP has no max_worker_count parm, so only nodes that declare one change this.
    if node.parm("max_worker_count"):
        settings.max_worker_count = node.parm("max_worker_count").eval()
    # The base settings class has no wheels fields, so they're set by name.
    if node.parm("include_adaptor_wheels"):
        setattr(
            settings, "include_adaptor_wheels", bool(node.parm("include_adaptor_wheels").eval())
        )
    if node.parm("adaptor_wheels"):
        setattr(settings, "adaptor_wheels", node.parm("adaptor_wheels").evalAsString())


def common_job_parameter_values(settings: BaseSubmitterSettings) -> list[dict[str, Any]]:
    """The ``deadline:*`` values, which the client library passes to ``CreateJob`` as arguments."""
    return [
        {"name": "deadline:priority", "value": settings.priority},
        {"name": "deadline:targetTaskRunStatus", "value": settings.initial_status},
        {"name": "deadline:maxFailedTasksCount", "value": settings.max_failed_tasks_count},
        {"name": "deadline:maxRetriesPerTask", "value": settings.max_retries_per_task},
    ]


def merge_parameter_values(
    values: list[dict[str, Any]], queue_parameters: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge caller queue parameters into ``values``, where a caller value wins on a name collision.

    An empty or None caller value counts as not given, so a queue default can't blank a node value.
    """
    # Keep only the caller values that carry something. Falsy but real values, like 0, still count.
    caller_values: dict[str, Any] = {
        param["name"]: param["value"]
        for param in queue_parameters
        if "value" in param and param["value"] not in (None, "")
    }
    # Replace the node's value on a collision, then add the caller's new names after it.
    # One entry per name avoids the duplicate parameter values that CreateJob rejects.
    merged: list[dict[str, Any]] = [
        {"name": param["name"], "value": caller_values.get(param["name"], param["value"])}
        for param in values
    ]
    existing_names: set[str] = {param["name"] for param in merged}
    merged.extend(
        {"name": name, "value": value}
        for name, value in caller_values.items()
        if name not in existing_names
    )
    return merged


def read_queue_parameter_definitions(node: Any) -> list[dict[str, Any]]:
    """The queue parameter definitions the node stored at its last queue parameter refresh."""
    queue_parameter_definitions_json: Optional[str] = node.userData("queue_parameter_definitions")
    return (
        json.loads(queue_parameter_definitions_json)
        if queue_parameter_definitions_json is not None
        else []
    )


def write_job_bundle(context: SubmissionContext, bundle_dir: str) -> None:
    """Write the context's template, parameter values, and asset references into ``bundle_dir``.

    The files are ``template.yaml``, ``parameter_values.yaml``, and ``asset_references.yaml``.
    """
    # Each file gets the client library's YAML style, which the bundle reader expects.
    job_bundle_path: Path = Path(bundle_dir)
    with open(job_bundle_path / "template.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(context.job_template, f, indent=1)
    with open(job_bundle_path / "parameter_values.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump({"parameterValues": context.parameter_values}, f, indent=1)
    with open(job_bundle_path / "asset_references.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(context.asset_references.to_dict(), f, indent=1)


def get_houdini_environments(init_data_attachment: dict[str, Any]) -> list[dict[str, Any]]:
    """The job environment that starts the Houdini adaptor daemon on enter and stops it on exit."""
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


def _load_adaptor_override_environment() -> dict[str, Any]:
    """Load the parameter definitions and environment that run a job with custom adaptor wheels."""
    override_file: str = os.path.join(
        os.path.dirname(__file__), "adaptor_override_environment.yaml"
    )
    with open(override_file) as yaml_file:
        return yaml.safe_load(yaml_file)


# Names that adaptor_override_environment.yaml adds, so the override can be found and removed.
_ADAPTOR_OVERRIDE_PARAM_NAMES: set[str] = {"AdaptorWheels", "OverrideAdaptorName"}
_ADAPTOR_OVERRIDE_ENV_NAME: str = "OverrideAdaptor"


def _apply_adaptor_wheels_override(job_template: dict[str, Any], enabled: bool) -> None:
    """Add the adaptor wheels override to ``job_template`` when ``enabled``, or else remove it.

    It ignores what the node already added, so the template always matches the AdaptorWheels value.
    """
    already_present: bool = any(
        pd.get("name") in _ADAPTOR_OVERRIDE_PARAM_NAMES
        for pd in job_template.get("parameterDefinitions", [])
    )

    # Add the override's parameters and environment, or strip both and drop an empty list.
    if enabled and not already_present:
        override: dict[str, Any] = _load_adaptor_override_environment()
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
