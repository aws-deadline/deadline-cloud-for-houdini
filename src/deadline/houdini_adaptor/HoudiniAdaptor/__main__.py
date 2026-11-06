# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json as _json
import logging as _logging
import os as _os
import sys as _sys
from pathlib import Path as _Path
from typing import List as _List
from typing import Mapping as _Mapping

import yaml as _yaml
from openjd.adaptor_runtime import EntryPoint as _EntryPoint

from .adaptor import HoudiniAdaptor

__all__ = ["main"]
_logger = _logging.getLogger(__name__)

# The run data key for each of the task's DEADLINE_* IDs.
_PDG_TASK_ID_VARIABLES: _Mapping[str, str] = {
    "farm_id": "DEADLINE_FARM_ID",
    "queue_id": "DEADLINE_QUEUE_ID",
    "job_id": "DEADLINE_JOB_ID",
    "step_id": "DEADLINE_STEP_ID",
    "task_id": "DEADLINE_TASK_ID",
}


def _copy_task_ids_into_run_data(argv: _List[str], env: _Mapping[str, str]) -> _List[str]:
    """
    Returns the arguments with the task's IDs added to the ``pdg`` object of ``--run-data``.
    Run data without a ``pdg`` object, or that cannot be read, is left for the runtime to handle.
    """
    # The daemon started before this task, so it can't see the task's DEADLINE_* IDs.
    # The runtime reads --run-data from sys.argv, so this copies them into that argument.
    result: _List[str] = list(argv)
    index: int
    prefix: str
    value: str
    for i, arg in enumerate(argv):
        # The run data is either the argument after --run-data or the value after "--run-data=".
        if arg == "--run-data" and i + 1 < len(argv):
            index, prefix, value = i + 1, "", argv[i + 1]
        elif arg.startswith("--run-data="):
            index, prefix, value = i, "--run-data=", arg[len("--run-data=") :]
        else:
            continue
        # Read it the way the runtime does: a file:// path, or inline JSON or YAML.
        try:
            text: str = (
                _Path(value[len("file://") :]).read_text(encoding="utf-8")
                if value.startswith("file://")
                else value
            )
            run_data: object = _yaml.safe_load(text)
        except (OSError, _yaml.YAMLError):
            break
        # Add each ID the session has, and write the run data back inline as JSON.
        if isinstance(run_data, dict) and isinstance(run_data.get("pdg"), dict):
            for key, variable in _PDG_TASK_ID_VARIABLES.items():
                if env.get(variable):
                    run_data["pdg"][key] = env[variable]
            result[index] = prefix + _json.dumps(run_data)
        break
    return result


def main(reentry_exe=None) -> int:
    """
    Entry point for the Houdini Adaptor
    """
    _logger.info("About to start the HoudiniAdaptor")

    package_name = vars(_sys.modules[__name__])["__package__"]
    if not package_name:
        raise RuntimeError(f"Must be run as a module. Do not run {__file__} directly")

    _sys.argv = _copy_task_ids_into_run_data(_sys.argv, _os.environ)

    try:
        _EntryPoint(HoudiniAdaptor).start(reentry_exe=reentry_exe)
    except Exception as e:
        _logger.error(f"Entrypoint failed: {e}")
        return 1

    _logger.info("Done HoudiniAdaptor main")
    return 0


if __name__ == "__main__":
    _sys.exit(main())
