# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the job settings reader, bundle writer, and wheels override the submitters share."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from hypothesis import given
from hypothesis import strategies as st

from deadline.client.api import BaseSubmitterSettings, SubmissionContext
from deadline.client.job_bundle.submission import AssetReferences

from deadline.houdini_submitter.python.deadline_cloud_for_houdini._job_bundle import (
    _apply_adaptor_wheels_override,
    read_common_job_settings,
    write_job_bundle,
)

# The job parms the ROP declares, with values the reader accepts.
_ROP_PARMS: dict[str, Any] = {
    "name": "shot_010",
    "priority": 75,
    "initial_status": "SUSPENDED",
    "failed_tasks_limit": 5,
    "task_retry_limit": 3,
    "description": "lighting pass",
    "include_adaptor_wheels": 0,
    "adaptor_wheels": "",
}


class _FakeParm:
    """A parm that returns one fixed value, as Houdini's ``eval`` and ``evalAsString`` do."""

    def __init__(self, value: Any) -> None:
        self._value: Any = value

    def eval(self) -> Any:
        """The value as stored."""
        return self._value

    def evalAsString(self) -> str:
        """The value as a string."""
        return str(self._value)


class _FakeNode:
    """A node that has only the parms it's given, so ``parm`` returns None for any other name."""

    def __init__(self, parms: dict[str, Any]) -> None:
        self._parms: dict[str, Any] = parms

    def parm(self, name: str) -> Optional[_FakeParm]:
        """The named parm, or None when the node doesn't declare it."""
        return _FakeParm(self._parms[name]) if name in self._parms else None


@given(st.integers(min_value=-1000, max_value=1000))
def test_a_node_with_max_worker_count_sets_the_worker_limit(count: int) -> None:
    """The scheduler node's Max Worker Count must reach the settings, or the job ignores it."""
    # Read a node that has the ROP's parms plus Max Worker Count.
    node: _FakeNode = _FakeNode({**_ROP_PARMS, "max_worker_count": count})
    settings: BaseSubmitterSettings = BaseSubmitterSettings()

    read_common_job_settings(node, settings)

    assert settings.max_worker_count == count


@given(
    st.text(min_size=1),
    st.integers(),
    st.sampled_from(["READY", "SUSPENDED"]),
    st.integers(),
    st.integers(),
    st.text(),
    st.booleans(),
    st.text(),
)
def test_a_node_with_the_rop_parms_sets_each_shared_setting(
    name: str,
    priority: int,
    status: str,
    failed: int,
    retries: int,
    description: str,
    include_wheels: bool,
    wheels: str,
) -> None:
    """Each shared parm must land on its own setting, or the job ignores what the artist set."""
    # Read a node that declares every shared parm, with arbitrary values.
    node: _FakeNode = _FakeNode(
        {
            "name": name,
            "priority": priority,
            "initial_status": status,
            "failed_tasks_limit": failed,
            "task_retry_limit": retries,
            "description": description,
            "include_adaptor_wheels": int(include_wheels),
            "adaptor_wheels": wheels,
        }
    )
    settings: BaseSubmitterSettings = BaseSubmitterSettings()

    read_common_job_settings(node, settings)

    # Compare each setting with the parm it comes from.
    assert settings.job_name == name
    assert settings.priority == priority
    assert settings.initial_status == status
    assert settings.max_failed_tasks_count == failed
    assert settings.max_retries_per_task == retries
    assert settings.description == description
    assert getattr(settings, "include_adaptor_wheels") is include_wheels
    assert getattr(settings, "adaptor_wheels") == wheels


def test_a_node_with_no_job_parms_leaves_every_setting_unchanged() -> None:
    """A node that declares none of the parms must not touch the settings, or reading it fails."""
    # Keep a copy of the defaults, then read a node with no parms at all.
    node: _FakeNode = _FakeNode({})
    settings: BaseSubmitterSettings = BaseSubmitterSettings()
    before: dict[str, Any] = dict(vars(settings))

    read_common_job_settings(node, settings)

    assert vars(settings) == before


def _template() -> dict[str, Any]:
    """A job template with one parameter and one environment, and no wheels override."""
    return {
        "specificationVersion": "jobtemplate-2023-09",
        "parameterDefinitions": [{"name": "HipFile", "type": "PATH"}],
        "jobEnvironments": [{"name": "Houdini"}],
    }


def test_enabling_the_wheels_override_twice_adds_it_once() -> None:
    """A second call must keep the override, so a template built twice still has the wheels."""
    template: dict[str, Any] = _template()

    _apply_adaptor_wheels_override(template, True)
    _apply_adaptor_wheels_override(template, True)

    # Each override name appears once, next to the template's own entries.
    names: list[str] = [pd["name"] for pd in template["parameterDefinitions"]]
    environments: list[str] = [env["name"] for env in template["jobEnvironments"]]
    assert names.count("AdaptorWheels") == 1
    assert names.count("OverrideAdaptorName") == 1
    assert environments == ["Houdini", "OverrideAdaptor"]


def _bundle_context() -> SubmissionContext:
    """A context whose template keys are out of order and whose script spans two lines."""
    return SubmissionContext(
        settings=BaseSubmitterSettings(),
        job_template={
            "specificationVersion": "jobtemplate-2023-09",
            "name": "shot_010",
            "jobEnvironments": [{"name": "Houdini", "script": {"data": "line one\nline two\n"}}],
        },
        parameter_values=[
            {"name": "deadline:priority", "value": 75},
            {"name": "HipFile", "value": "/projects/shot_010.hip"},
        ],
        asset_references=AssetReferences(
            input_filenames={"/projects/tex/b.exr", "/projects/tex/a.exr"},
            output_directories={"/projects/render"},
        ),
    )


def test_the_bundle_holds_the_contexts_template_values_and_asset_references(
    tmp_path: Path,
) -> None:
    """Each bundle file must hold its own part of the context, or the job runs with wrong data."""
    context: SubmissionContext = _bundle_context()

    write_job_bundle(context, str(tmp_path))

    # Exactly the three bundle files, each loading back to the part it was written from.
    assert sorted(os.listdir(tmp_path)) == [
        "asset_references.yaml",
        "parameter_values.yaml",
        "template.yaml",
    ]
    template: dict[str, Any] = yaml.safe_load((tmp_path / "template.yaml").read_text("utf8"))
    values: dict[str, Any] = yaml.safe_load((tmp_path / "parameter_values.yaml").read_text("utf8"))
    assets: dict[str, Any] = yaml.safe_load((tmp_path / "asset_references.yaml").read_text("utf8"))
    assert template == context.job_template
    assert values == {"parameterValues": context.parameter_values}
    assert assets == {
        "assetReferences": {
            "inputs": {
                "directories": [],
                "filenames": ["/projects/tex/a.exr", "/projects/tex/b.exr"],
            },
            "outputs": {"directories": ["/projects/render"]},
            "referencedPaths": [],
        }
    }


def test_the_template_keeps_its_key_order_and_writes_a_multiline_script_as_a_block(
    tmp_path: Path,
) -> None:
    """The template must keep the client library's YAML style, or the ROP's bundle changes."""
    write_job_bundle(_bundle_context(), str(tmp_path))

    # Keys in the order the template has them, and the two-line script as a literal block.
    text: str = (tmp_path / "template.yaml").read_text("utf8")
    assert text.index("specificationVersion:") < text.index("name: shot_010")
    assert text.index("name: shot_010") < text.index("jobEnvironments:")
    assert "data: |" in text
