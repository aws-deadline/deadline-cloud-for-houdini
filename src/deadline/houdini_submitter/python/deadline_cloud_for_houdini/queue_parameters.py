# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
from typing import Any, Optional, Union

import hou

from ._version import version_tuple as adaptor_version_tuple
from deadline.client.api._queue_parameters import get_queue_parameter_definitions
from deadline.client.job_bundle.parameters import JobParameter


def _get_queue_parameter_groups(
    queue_parameter_definitions: list[JobParameter],
) -> tuple[dict[str, list[JobParameter]], list[JobParameter]]:  # pragma: no cover
    groups: dict[str, list[JobParameter]] = {}
    no_group: list[JobParameter] = []
    for definition in queue_parameter_definitions:
        if "userInterface" in definition and "groupLabel" in definition["userInterface"]:
            if definition["userInterface"]["groupLabel"] not in groups:
                groups[definition["userInterface"]["groupLabel"]] = [definition]
            else:
                groups[definition["userInterface"]["groupLabel"]].append(definition)
        else:
            no_group.append(definition)

    return groups, no_group


_QUEUE_ENVIRONMENT_NAME_PREFIX = "queue_env_do_not_use_"


def _get_prefixed_name(name: str) -> str:  # pragma: no cover
    return f"{_QUEUE_ENVIRONMENT_NAME_PREFIX}{name}"


def _get_name_without_prefix(prefixed_name: str) -> str:  # pragma: no cover
    if not prefixed_name.startswith(_QUEUE_ENVIRONMENT_NAME_PREFIX):
        raise ValueError(
            f"Prefixed name {prefixed_name} does not start with {_QUEUE_ENVIRONMENT_NAME_PREFIX}"
        )
    return prefixed_name[len(_QUEUE_ENVIRONMENT_NAME_PREFIX) :]


def _is_param_hidden(param: JobParameter) -> bool:  # pragma: no cover
    if "userInterface" in param and "control" in param["userInterface"]:
        control = param["userInterface"]["control"]
        if control == "HIDDEN":
            return True
    return False


def _get_menu_items(param: JobParameter) -> tuple[Union[int, float, str], ...]:  # pragma: no cover
    if "userInterface" in param and "control" in param["userInterface"]:
        control = param["userInterface"]["control"]
        if control == "DROPDOWN_LIST":
            if "allowedValues" not in param:
                raise ValueError(
                    f"Queue Parameter {param['name']} has a control type of DROPDOWN_LIST, but allowedValues was not provided."
                )
            if param["type"] == "INT":
                return tuple(map(int, param["allowedValues"]))
            elif param["type"] == "FLOAT":
                return tuple(map(float, param["allowedValues"]))
            return tuple(param["allowedValues"])
    return ()


_CHECKBOX_ALLOWED_VALUES = {
    tuple(sorted(["true", "false"])): "true,false",
    tuple(sorted(["yes", "no"])): "yes,no",
    tuple(sorted(["on", "off"])): "on,off",
    tuple(sorted(["1", "0"])): "1,0",
}

_TRUTHY = {
    "true",
    "yes",
    "on",
    "1",
}

_BOOL_STRINGS = {
    "true",
    "false",
    "yes",
    "no",
    "off",
    "on",
    "1",
    "0",
}


def _bool_string_from_allowed(
    allowed_bool_strings: str, value: Union[bool, int, str]
) -> str:  # pragma: no cover
    if isinstance(value, bool):
        value_as_bool = value
    elif isinstance(value, int):
        value_as_bool = value != 0
    elif isinstance(value, str):
        value_as_bool = value in _TRUTHY
    else:
        raise ValueError(f"Unknown value type: {type(value)}")

    if allowed_bool_strings == "true,false":
        return "true" if value_as_bool else "false"
    elif allowed_bool_strings == "yes,no":
        return "yes" if value_as_bool else "no"
    elif allowed_bool_strings == "on,off":
        return "on" if value_as_bool else "off"
    elif allowed_bool_strings == "1,0":
        return "1" if value_as_bool else "0"
    else:
        raise ValueError(f"Unknown set of allowed bool strings: {allowed_bool_strings}")


def _get_checkbox(param: JobParameter) -> bool:  # pragma: no cover
    if "userInterface" in param and "control" in param["userInterface"]:
        control = param["userInterface"]["control"]
        if control == "CHECK_BOX":
            if "allowedValues" not in param:
                raise ValueError(
                    f"Queue Parameter {param['name']} has a control type of CHECK_BOX, but allowedValues was not provided."
                )
            if tuple(sorted(param["allowedValues"])) not in _CHECKBOX_ALLOWED_VALUES:
                raise ValueError(
                    f"Queue Parameter {param['name']} has a control type of CHECK_BOX, but allowedValues is not one of the allowed values for a checkbox."
                )
            return True
    return False


def _get_equivalent_bool(original_value: str) -> Optional[bool]:  # pragma: no cover
    if original_value not in _BOOL_STRINGS:
        return None
    return original_value in _TRUTHY


def _get_default_value(
    param: JobParameter,
) -> tuple[Union[str, int, float], ...]:  # pragma: no cover
    houdini_version = ".".join(hou.applicationVersionString().split(".")[:2])
    adaptor_version = ".".join(str(v) for v in adaptor_version_tuple[:2])

    if param["name"] == "RezPackages":
        return (f"houdini-{houdini_version} deadline_cloud_for_houdini",)
    elif param["name"] == "CondaPackages":
        return (f"houdini={houdini_version}.* houdini-openjd={adaptor_version}.*",)
    elif "default" in param:
        return (param["default"],)
    else:
        return ()


def _get_control_for_string_parameter(param: JobParameter) -> hou.ParmTemplate:  # pragma: no cover
    label = (
        param["userInterface"]["label"]
        if "userInterface" in param and "label" in param["userInterface"]
        else param["name"]
    )
    default = _get_default_value(param)
    menu_items = _get_menu_items(param)
    hidden = _is_param_hidden(param)
    help = None if "description" not in param else param["description"]
    string_type = hou.stringParmType.Regular
    checkbox = _get_checkbox(param)

    if param["type"] == "PATH":
        string_type = hou.stringParmType.FileReference

    if checkbox:
        return hou.ToggleParmTemplate(
            label=label,
            name=param["name"],
            default_value=default in {"true", "yes", "on", 1},
            help=help,
            tags={
                "allowed_bool_strings": _CHECKBOX_ALLOWED_VALUES[
                    tuple(sorted(param["allowedValues"]))
                ]
            },
        )

    return hou.StringParmTemplate(
        name=_get_prefixed_name(param["name"]),
        label=label,
        num_components=1,
        default_value=default,
        string_type=string_type,
        menu_items=menu_items,
        is_hidden=hidden,
        is_label_hidden=hidden,
        help=help,
    )


def _get_control_for_int_parameter(param: JobParameter) -> hou.ParmTemplate:  # pragma: no cover
    label = (
        param["userInterface"]["label"]
        if "userInterface" in param and "label" in param["userInterface"]
        else param["name"]
    )
    default = _get_default_value(param)
    min = (int(param["minValue"]),) if "minValue" in param else (0,)
    max = (int(param["maxValue"]),) if "maxValue" in param else (10,)
    min_is_strict = "minValue" in param
    max_is_strict = "maxValue" in param
    hidden = _is_param_hidden(param)
    menu_items = _get_menu_items(param)
    help = None if "description" not in param else param["description"]

    return hou.IntParmTemplate(
        name=_get_prefixed_name(param["name"]),
        label=label,
        num_components=1,
        default_value=default,
        min=min,
        max=max,
        min_is_strict=min_is_strict,
        max_is_strict=max_is_strict,
        menu_items=menu_items,
        is_hidden=hidden,
        is_label_hidden=hidden,
        help=help,
    )


def _get_control_for_float_parameter(param: JobParameter) -> hou.ParmTemplate:  # pragma: no cover
    label = (
        param["userInterface"]["label"]
        if "userInterface" in param and "label" in param["userInterface"]
        else param["name"]
    )
    default = _get_default_value(param)
    min = (float(param["minValue"]),) if "minValue" in param else (0.0,)
    max = (float(param["maxValue"]),) if "maxValue" in param else (10.0,)
    min_is_strict = "minValue" in param
    max_is_strict = "maxValue" in param
    hidden = _is_param_hidden(param)
    help = None if "description" not in param else param["description"]

    return hou.FloatParmTemplate(
        name=_get_prefixed_name(param["name"]),
        label=label,
        num_components=1,
        default_value=default,
        min=min,
        max=max,
        min_is_strict=min_is_strict,
        max_is_strict=max_is_strict,
        is_hidden=hidden,
        is_label_hidden=hidden,
        help=help,
    )


def _get_control_for_parameter(param: JobParameter) -> hou.ParmTemplate:  # pragma: no cover
    if param["type"] == "STRING" or param["type"] == "PATH":
        return _get_control_for_string_parameter(param)
    elif param["type"] == "INT":
        return _get_control_for_int_parameter(param)
    elif param["type"] == "FLOAT":
        return _get_control_for_float_parameter(param)
    else:
        raise ValueError(f"Got Queue Parameter of unknown type {param['type']}")


def _get_folder_for_group(
    group_definitions: list[JobParameter], group_name: str, group_label: str
) -> hou.FolderParmTemplate:  # pragma: no cover
    group_definitions_by_name = {definition["name"]: definition for definition in group_definitions}
    group_folder = hou.FolderParmTemplate(
        name=group_name,
        label=group_label,
        folder_type=hou.folderType.Simple,
    )
    sorted_definition_names = sorted(group_definitions_by_name.keys())
    for definition_name in sorted_definition_names:
        definition = group_definitions_by_name[definition_name]
        group_folder.addParmTemplate(_get_control_for_parameter(definition))
    return group_folder


def _get_queue_parameter_values(
    node: hou.Node, queue_parameter_definitions: list[JobParameter]
) -> dict[str, Union[float, int, str]]:  # pragma: no cover
    existing_values: dict[str, Union[float, int, str]] = {}
    for definition in queue_parameter_definitions:
        prefixed_name = _get_prefixed_name(definition["name"])
        param = node.parm(prefixed_name)
        if param is not None:
            existing_values[definition["name"]] = param.eval()
    return existing_values


def remove_queue_parameters_from_node(node: hou.Node) -> None:  # pragma: no cover
    parm_template_group = node.parmTemplateGroup()
    removed_node_folders = set()
    spare_parms = node.spareParms()
    for spare_parm in spare_parms:
        if spare_parm.name().startswith(_QUEUE_ENVIRONMENT_NAME_PREFIX):
            removed_node_folders.add(spare_parm.containingFolders())
            parm_template_group.remove(spare_parm.name())
    node.setParmTemplateGroup(parm_template_group)
    for folder in removed_node_folders:
        node.removeSpareParmFolder(folder)


def get_queue_parameter_values_as_openjd(
    node: hou.Node,
) -> list[dict[str, Any]]:  # pragma: no cover
    result = []
    spare_parms = node.spareParms()
    for spare_parm in spare_parms:
        if spare_parm.name().startswith(_QUEUE_ENVIRONMENT_NAME_PREFIX):
            name = _get_name_without_prefix(spare_parm.name())
            parm_template = spare_parm.parmTemplate()
            value = spare_parm.eval()
            if (
                isinstance(parm_template, hou.ToggleParmTemplate)
                and "allowed_bool_strings" in parm_template.tags()
            ):
                value = _bool_string_from_allowed(
                    parm_template.tags()["allowed_bool_strings"], value
                )
            result.append({"name": name, "value": value})
    return result


def _rebuild_queue_parameters_ui(
    queue_parameter_definitions: list[JobParameter], node: hou.Node
) -> None:  # pragma: no cover
    (
        queue_parameter_definition_groups,
        queue_parameter_definition_no_group,
    ) = _get_queue_parameter_groups(queue_parameter_definitions)
    sorted_group_names = sorted(queue_parameter_definition_groups.keys())
    for group_name in sorted_group_names:
        group_folder = _get_folder_for_group(
            queue_parameter_definition_groups[group_name],
            _get_prefixed_name(group_name),
            group_name,
        )
        node.addSpareParmTuple(group_folder, ("Shared Job Settings",))

    if len(queue_parameter_definition_no_group) > 0:
        group_folder = _get_folder_for_group(
            queue_parameter_definition_no_group,
            "no_group_queue_environment",
            "Queue Environment",
        )
        node.addSpareParmTuple(group_folder, ("Shared Job Settings",))


def _restore_queue_parameter_values(
    node: hou.Node, existing_values: dict[str, Union[float, int, str]]
) -> None:  # pragma: no cover
    for name, value in existing_values.items():
        parm = node.parm(_get_prefixed_name(name))
        if parm is not None:
            parm_template = parm.parmTemplate()
            if isinstance(parm_template, hou.ToggleParmTemplate):
                if isinstance(value, str):
                    bool_value = _get_equivalent_bool(value)
                    if bool_value is not None:
                        parm.set(bool_value)
            elif isinstance(parm_template, hou.StringParmTemplate):
                if isinstance(value, str):
                    parm.set(value)
            elif isinstance(parm_template, hou.IntParmTemplate):
                if isinstance(value, int):
                    parm.set(value)
            elif isinstance(parm_template, hou.FloatParmTemplate):
                if isinstance(value, float):
                    parm.set(value)


def get_queue_parameter_definitions_from_service(farm_id: str, queue_id: str) -> list[JobParameter]:
    return get_queue_parameter_definitions(farmId=farm_id, queueId=queue_id)


def update_queue_parameters(farm_id: str, queue_id: str, node: hou.Node) -> None:
    queue_parameter_definitions = get_queue_parameter_definitions_from_service(farm_id, queue_id)
    queue_parameter_definitions_json = json.dumps(queue_parameter_definitions)
    node.setUserData("queue_parameter_definitions", queue_parameter_definitions_json)

    # When we rebuild the UI, we want to keep any values for parameters
    # that the user has already entered.
    existing_values = _get_queue_parameter_values(node, queue_parameter_definitions)

    # Remove old queue environment spare parameters
    # Spare parameters can be user defined,
    # so we can't remove all of them
    remove_queue_parameters_from_node(node)

    # Now rebuild the UI
    _rebuild_queue_parameters_ui(queue_parameter_definitions, node)

    # Now put back the users's values if they exist
    # and are of the same type.
    _restore_queue_parameter_values(node, existing_values)
