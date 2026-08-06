# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the headless HoudiniSubmitter(BaseSubmitter) engine.

The unified base class (BaseSubmitter) lives in deadline-cloud and is
re-exported from ``deadline.client.api``. These tests import the submitter
module directly (which imports BaseSubmitter at load); they fail until a
deadline release carrying the base is installed and pass once it is.
"""

from __future__ import annotations

from unittest import mock

import pytest

from deadline.client.job_bundle.submission import AssetReferences

from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter


def _parm(value):
    p = mock.MagicMock()
    p.eval.return_value = value
    p.evalAsString.return_value = str(value)
    return p


def _make_rop(frame_tuple=(1, 100, 1)):
    rop = mock.MagicMock()
    rop.path.return_value = "/out/deadline_cloud1"
    parms = {
        "name": _parm("mantra_job"),
        "priority": _parm(50),
        "initial_status": _parm("READY"),
        "failed_tasks_limit": _parm(20),
        "task_retry_limit": _parm(5),
        "description": _parm("my rop"),
    }
    rop.parm.side_effect = lambda n: parms.get(n)
    ftuple = [_parm(frame_tuple[0]), _parm(frame_tuple[1]), _parm(frame_tuple[2])]
    rop.parmTuple.side_effect = lambda n: ftuple if n == "f" else None
    return rop


def test_engine_is_concrete_base_submitter():
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    assert isinstance(api, submitter.BaseSubmitter)


def test_set_rop_node_path_updates_resolved_node():
    api = submitter.HoudiniSubmitter()
    assert api._get_rop_node() is None
    api.set_rop_node_path("/out/deadline_cloud1")
    with mock.patch.object(submitter, "hou") as hou_mock:
        api._get_rop_node()
    hou_mock.node.assert_called_once_with("/out/deadline_cloud1")


def test_get_settings_populates_frame_list_and_scene_refs():
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    refs = AssetReferences(
        input_filenames={"/scene.hip"},
        input_directories={"/proj/geo"},
        output_directories={"/proj/render"},
    )
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_hip_file", return_value="/scene.hip"),
        mock.patch.object(submitter, "_get_scene_asset_references", return_value=refs),
    ):
        hou_mock.hipFile.basename.return_value = "scene.hip"
        hou_mock.getenv.return_value = "/proj"
        hou_mock.playbar.frameRange.return_value = (1.0, 10.0)
        hou_mock.node.return_value = _make_rop((1, 100, 3))
        settings = api.get_settings()

    # ROP frame-range tuple wins over the playbar and preserves the step.
    assert settings.frame_list == "1-100:3"
    assert settings.job_name == "mantra_job"
    # Scene-scanned references flow into the ABC fields (so AYON's publish
    # collector, which reads settings.*, gets real dirs).
    assert settings.input_directories == ["/proj/geo"]
    assert settings.output_directories == ["/proj/render"]
    assert settings.output_path == "/proj/render"


def test_get_settings_scan_assets_false_skips_scene_scan():
    # The GUI submit/export path writes asset_references.yaml from the
    # dialog-provided references and never reads settings.input_*/output_*, so it
    # passes scan_assets=False to skip the (potentially expensive) scene + USD
    # walk. Scalars/frame range are still populated; the asset scan is not run.
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_hip_file", return_value="/scene.hip"),
        mock.patch.object(submitter, "_get_scene_asset_references") as scan,
    ):
        hou_mock.hipFile.basename.return_value = "scene.hip"
        hou_mock.getenv.return_value = "/proj"
        hou_mock.playbar.frameRange.return_value = (1.0, 10.0)
        hou_mock.node.return_value = _make_rop((1, 100, 3))
        settings = api.get_settings(scan_assets=False)

    # The expensive scene/USD scan was not performed...
    scan.assert_not_called()
    # ...but scalar + frame-range settings are still collected from the ROP.
    assert settings.frame_list == "1-100:3"
    assert settings.job_name == "mantra_job"
    # No scanned refs; falls back to the hip file only.
    assert settings.input_filenames == ["/scene.hip"]
    assert settings.input_directories == []
    assert settings.output_directories == []


def test_get_job_template_delegates_to_node_builder_and_overrides_from_settings():
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    rop = _make_rop()
    # The node builder returns the ROP-derived template (step graph, node name);
    # the engine then overrides settings-driven fields.
    node_template = {
        "specificationVersion": "jobtemplate-2023-09",
        "name": "node_name",
        "steps": [],
    }
    settings = submitter.HoudiniSubmitterSettings(job_name="Edited Job", description="Edited desc")
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_job_template", return_value=node_template) as build,
    ):
        hou_mock.node.return_value = rop
        result = api.get_job_template(settings, {"amounts": []})
    # step graph still comes from the node builder (host_requirements forwarded)...
    build.assert_called_once_with(rop, {"amounts": []})
    # ...but the settings-driven scalars win.
    assert result["name"] == "Edited Job"
    assert result["description"] == "Edited desc"


def test_get_parameter_values_settings_driven_and_merges_queue_params():
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    settings = submitter.HoudiniSubmitterSettings(
        priority=99, initial_status="SUSPENDED", max_failed_tasks_count=7, max_retries_per_task=2
    )
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_hip_file", return_value="/scene.hip"),
        mock.patch.object(submitter, "get_queue_parameter_values_as_openjd", return_value=[]),
    ):
        hou_mock.node.return_value = _make_rop()
        result = api.get_parameter_values(
            settings,
            [{"name": "deadline:maxWorkerCount", "value": 3}],  # disjoint -> appended
        )
    by_name = {p["name"]: p["value"] for p in result}
    # scalar deadline:* values come from settings, not the node
    assert by_name["deadline:priority"] == 99
    assert by_name["deadline:targetTaskRunStatus"] == "SUSPENDED"
    assert by_name["deadline:maxFailedTasksCount"] == 7
    assert by_name["deadline:maxRetriesPerTask"] == 2
    # disjoint caller queue parameter is appended
    assert by_name["deadline:maxWorkerCount"] == 3


def test_get_parameter_values_queue_params_caller_wins_on_collision():
    # Realistic headless flow: the node emits its own CondaPackages via
    # get_queue_parameter_values_as_openjd, and the caller (e.g. AYON) passes the
    # SAME queue parameter as its resolved value. The caller's value must win
    # (de-dup, no duplicate parameterValues, no error).
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_hip_file", return_value="/scene.hip"),
        mock.patch.object(
            submitter,
            "get_queue_parameter_values_as_openjd",
            return_value=[
                {"name": "CondaPackages", "value": "houdini"},  # node's value
                {"name": "CondaChannels", "value": "deadline-cloud"},
            ],
        ),
    ):
        hou_mock.node.return_value = _make_rop()
        result = api.get_parameter_values(
            settings=submitter.HoudiniSubmitterSettings(),
            queue_parameters=[
                {"name": "CondaPackages", "value": "houdini py311"},  # caller override
                {"name": "deadline:maxWorkerCount", "value": 3},  # disjoint -> appended
            ],
        )
    names = [p["name"] for p in result]
    by_name = {p["name"]: p["value"] for p in result}
    # caller's value supersedes the node's, exactly once (no duplicate)
    assert names.count("CondaPackages") == 1
    assert by_name["CondaPackages"] == "houdini py311"
    # node-only queue param preserved; disjoint caller param appended
    assert by_name["CondaChannels"] == "deadline-cloud"
    assert by_name["deadline:maxWorkerCount"] == 3


def test_get_parameter_values_empty_caller_value_does_not_clobber_node():
    # Regression: AYON's create path builds queue_parameters from
    # get_queue_parameters(), which resolves EVERY parameter to its queue
    # default -- including a CondaPackages whose queue default is empty. That
    # empty value must NOT override the node's computed CondaPackages (e.g.
    # "houdini=20.5.* houdini-openjd=0.x.*"); an empty/None caller value is
    # treated as "not provided" so caller-wins only applies to real overrides.
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_hip_file", return_value="/scene.hip"),
        mock.patch.object(
            submitter,
            "get_queue_parameter_values_as_openjd",
            return_value=[
                # node's computed value, populated by update_queue_parameters
                {"name": "CondaPackages", "value": "houdini=20.5.* houdini-openjd=0.48.*"},
            ],
        ),
    ):
        hou_mock.node.return_value = _make_rop()
        result = api.get_parameter_values(
            settings=submitter.HoudiniSubmitterSettings(),
            queue_parameters=[
                {"name": "CondaPackages", "value": ""},  # empty queue default
                {"name": "CondaChannels", "value": None},  # unset -> ignored
            ],
        )
    by_name = {p["name"]: p["value"] for p in result}
    names = [p["name"] for p in result]
    # node's computed value survives; empty caller default did not clobber it
    assert by_name["CondaPackages"] == "houdini=20.5.* houdini-openjd=0.48.*"
    assert names.count("CondaPackages") == 1
    # an empty/None caller-only parameter is not emitted as a blank value
    assert "CondaChannels" not in by_name


def test_get_job_template_adaptor_wheels_consistent_with_settings(tmp_path):
    # Template's AdaptorWheels definition/env must track the settings-driven
    # value in get_parameter_values, not the node. Node builder returns a
    # template WITHOUT the override; settings enable it (existing path) -> the
    # engine adds the AdaptorWheels definition + OverrideAdaptor jobEnvironment.
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    wheels_dir = str(tmp_path)  # exists, so wheels are "enabled"
    settings = submitter.HoudiniSubmitterSettings(
        include_adaptor_wheels=True, adaptor_wheels=wheels_dir
    )
    node_template = {
        "specificationVersion": "jobtemplate-2023-09",
        "name": "n",
        "parameterDefinitions": [{"name": "HipFile"}],
        "steps": [],
    }
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_job_template", return_value=node_template),
    ):
        hou_mock.node.return_value = _make_rop()
        tmpl = api.get_job_template(settings)
    def_names = {pd["name"] for pd in tmpl["parameterDefinitions"]}
    env_names = {env["name"] for env in tmpl.get("jobEnvironments", [])}
    assert "AdaptorWheels" in def_names
    assert "OverrideAdaptor" in env_names


def test_get_job_template_strips_adaptor_wheels_when_settings_disable():
    # Node builder returns a template WITH the override, but settings disable
    # wheels -> the engine strips the definition/env so the template carries no
    # AdaptorWheels definition without a value.
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    settings = submitter.HoudiniSubmitterSettings(include_adaptor_wheels=False)
    node_template = {
        "specificationVersion": "jobtemplate-2023-09",
        "name": "n",
        "parameterDefinitions": [{"name": "HipFile"}, {"name": "AdaptorWheels"}],
        "jobEnvironments": [{"name": "OverrideAdaptor"}],
        "steps": [],
    }
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_job_template", return_value=node_template),
    ):
        hou_mock.node.return_value = _make_rop()
        tmpl = api.get_job_template(settings)
    def_names = {pd["name"] for pd in tmpl["parameterDefinitions"]}
    assert "AdaptorWheels" not in def_names
    assert "jobEnvironments" not in tmpl  # only entry removed -> key dropped


def test_get_asset_references_returns_typed_scene_scan():
    api = submitter.HoudiniSubmitter("/out/deadline_cloud1")
    refs = AssetReferences(input_filenames={"/scene.hip"})
    with (
        mock.patch.object(submitter, "hou") as hou_mock,
        mock.patch.object(submitter, "_get_scene_asset_references", return_value=refs) as scan,
    ):
        hou_mock.node.return_value = _make_rop()
        result = api.get_asset_references(submitter.HoudiniSubmitterSettings())
    assert result is refs
    assert isinstance(result, AssetReferences)
    scan.assert_called_once()


def test_missing_rop_raises_runtimeerror():
    api = submitter.HoudiniSubmitter()  # no path -> _get_rop_node returns None
    with pytest.raises(RuntimeError, match="Cannot find ROP node"):
        api.get_job_template(submitter.HoudiniSubmitterSettings())
