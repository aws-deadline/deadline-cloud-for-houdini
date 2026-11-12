# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""How the Houdini adaptor runs one PDG work item, which never branches on the node type.

The adaptor's process downloads the item and publishes its outputs, and the Houdini client runs it.
A stock PDG job script cooks in the client's Houdini, and any other command runs through a shell.
"""

from __future__ import annotations

# Only the standard library, so the module imports in Houdini's own Python.
import contextlib
import gzip
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Pattern,
    Sequence,
    Tuple,
)

# In Houdini's Python the adaptor puts only the ``openjd`` and ``deadline`` namespace
# directories on the path, so the packages inside them import as top-level names there.
try:
    from openjd.adaptor_runtime_client import PathMappingRule

    from deadline.houdini_pdg.graph import PDG_DIR_TOKEN
    from deadline.houdini_pdg.ports import OutputFile, TaskAddress, TaskStorage
except ImportError:
    from adaptor_runtime_client import PathMappingRule  # type: ignore[import-not-found,no-redef]
    from houdini_pdg.graph import PDG_DIR_TOKEN  # type: ignore[import-not-found,no-redef]
    from houdini_pdg.ports import (  # type: ignore[import-not-found,no-redef]
        OutputFile,
        TaskAddress,
        TaskStorage,
    )

_logger: logging.Logger = logging.getLogger(__name__)

#: Houdini's Python version, because the Houdini client runs this module in Houdini's Python.
_HOUDINI_PYTHON_VERSION: str = f"{sys.version_info.major}.{sys.version_info.minor}"

#: The client prints this, then the exit code, on a line of its own after every item.
#: The adaptor waits for it.
EXIT_CODE_MARKER: str = "PdgActions: work item exit code "

#: The variables the client sets for every item. An exported attribute never
#: overrides one, because PDG's own work item loader reads them.
_RESERVED_ENV: FrozenSet[str] = frozenset(
    {
        "PDG_ITEM_NAME",
        "PDG_ITEM_ID",
        "PDG_INDEX",
        "PDG_ITEM_DATA_SOURCE",
        "PDG_TEMP",
        "PDG_DIR",
        "PDG_OUTPUT_DIR",
        "PDG_SCRIPTDIR",
        "PDG_RESULT_SERVER",
    }
)

_TOKEN: Pattern[str] = re.compile(r"__PDG_[A-Z0-9_]+?__")

# The command PDG's job scripts are started with: `"__PDG_HYTHON__" "__PDG_SCRIPTDIR__/rop.py" json`.
_STOCK_COMMAND: Pattern[str] = re.compile(
    r'^\s*"?__PDG_HYTHON__"?\s+"?__PDG_SCRIPTDIR__/([A-Za-z_][A-Za-z0-9_]*)\.py"?\s+json\s*$'
)

_RESULT_PREFIX: str = "PDG_RESULT:"

# pdg.attribFlag, as pdgjob/pdgjson.py defines it. WorkItem.environment is the
# attributes with EnvExport and without Internal.
_ENV_EXPORT_FLAG: int = 0x2
_INTERNAL_FLAG: int = 0x8

# pdg.attribType. Values are formatted as PDG formats them, because a wedge value
# often ends up in a file name: a float 0 renders as "0.0", an int 1 as "1".
_INT_ATTRIBUTE: int = 0
_FLOAT_ATTRIBUTE: int = 1


@dataclass(frozen=True)
class WorkItemPayload:
    """A work item as PDG serialized it."""

    data: Mapping[str, Any]

    @property
    def _work_item(self) -> Mapping[str, Any]:
        block: Any = self.data.get("workitem")
        return block if isinstance(block, Mapping) else {}

    @property
    def item_name(self) -> str:
        """``workitem.name``, else ``{nodeName}_{id}``, else the top-level ``name``."""
        # PDG names an unnamed item after its node and ID, as its own loader does.
        work_item: Mapping[str, Any] = self._work_item
        if work_item.get("name"):
            return str(work_item["name"])
        node: Any = work_item.get("nodeName")
        item_id: Any = work_item.get("id")
        if node and item_id is not None:
            return f"{node}_{item_id}"
        return str(self.data.get("name") or "")

    @property
    def command(self) -> str:
        """``workitem.command``, else ``__pdg_commandstring[0]``, else ``command``."""
        work_item: Mapping[str, Any] = self._work_item
        if work_item.get("command"):
            return str(work_item["command"])
        # PDG keeps a node's command line in the first value of this attribute.
        attributes: Any = work_item.get("attributes")
        entry: Any = (
            attributes.get("__pdg_commandstring") if isinstance(attributes, Mapping) else None
        )
        values: Any = entry.get("value") if isinstance(entry, Mapping) else None
        if isinstance(values, list) and values and values[0]:
            return str(values[0])
        return str(self.data.get("command") or "")

    def exported_variables(self) -> Dict[str, str]:
        """The attributes flagged ``EnvExport`` and not ``Internal``, formatted as PDG formats them.

        The payload's own ``environment`` field is always empty, so the attributes are the only source.
        """
        # A serialized environment block wins when it has anything in it.
        variables: Dict[str, str] = {}
        for block in (self._work_item.get("environment"), self.data.get("environment")):
            if isinstance(block, Mapping):
                variables.update({str(k): str(v) for k, v in block.items()})
        # Otherwise the exported attributes are the environment, as PDG builds it.
        if not variables:
            attributes: Any = self._work_item.get("attributes")
            if isinstance(attributes, Mapping):
                for name, attribute in attributes.items():
                    value: Optional[str] = _format_exported_attribute(attribute)
                    if value is not None:
                        variables[str(name)] = value
        # Leave out PDG's own names, and any name or value os.environ refuses.
        return {
            k: v
            for k, v in variables.items()
            if k not in _RESERVED_ENV and k and "=" not in k and "\0" not in k and "\0" not in v
        }

    def map_paths(self, rules: Sequence[PathMappingRule]) -> "WorkItemPayload":
        """The payload with every string rewritten by the first rule that matches it."""
        if not rules:
            return self
        # Each rule's source, and the function that applies it to one path.
        appliers: List[Tuple[str, Callable[..., Tuple[bool, str]]]] = []
        for rule in rules:
            source: str = str(getattr(rule, "source_path", ""))
            if hasattr(rule, "apply"):
                appliers.append((source, rule.apply))
                continue
            # The client's rules are plain data. The runtime's rule has the matching logic.
            from openjd.adaptor_runtime.adaptors import PathMappingRule as RuntimeRule

            runtime_rule: RuntimeRule = RuntimeRule(
                source_path_format=rule.source_path_format,
                source_path=rule.source_path,
                destination_path=rule.destination_path,
                destination_os=rule.destination_os,
            )
            appliers.append((source, runtime_rule.apply))

        # A whole string maps by the first rule that matches it. A string no rule
        # matches may still hold paths inside it, such as a command line.
        def map_value(value: Any) -> Any:
            if isinstance(value, str):
                for _source, apply in appliers:
                    changed: bool
                    mapped: str
                    changed, mapped = apply(path=value)
                    if changed:
                        return mapped
                return _map_embedded_paths(value, appliers)
            if isinstance(value, Mapping):
                return {k: map_value(v) for k, v in value.items()}
            if isinstance(value, list):
                return [map_value(v) for v in value]
            return value

        return WorkItemPayload(map_value(self.data))


class TokenResolver:
    """Replaces PDG's ``__PDG_*__`` tokens in a command with this worker's values.

    An unknown token, or the ID token with no ID, is left as is so the failure names it.
    ``__PDG_ITEM_LABEL__`` defaults to the item name, as a PDG work item's label does.
    """

    # The worker's platform. A subclass can set another one.
    _platform: str = sys.platform

    def __init__(
        self,
        *,
        hfs: str,
        pdg_dir: str,
        pdg_temp: str,
        item_name: str,
        python_version: str,
        item_id: str = "",
        item_label: str = "",
    ) -> None:
        # Houdini's own executables, or bare names that the shell finds on PATH.
        windows: bool = self._platform.startswith("win")
        hython: str = (
            (f"{hfs}/bin/hython.exe" if windows else f"{hfs}/bin/hython") if hfs else "hython"
        )
        python: str = _find_houdini_python(hfs, python_version, self._platform)
        scriptdir: str = f"{hfs}/houdini/python{python_version}libs/pdgjob" if hfs else ""
        self._values: Dict[str, str] = {
            "__PDG_HYTHON__": hython,
            "__PDG_PYTHON__": python,
            "__PDG_SCRIPTDIR__": scriptdir,
            "__PDG_HFS__": hfs,
            "__PDG_DIR__": pdg_dir,
            "__PDG_SHARED_ROOT__": pdg_dir,
            "__PDG_TEMP__": pdg_temp,
            "__PDG_SHARED_TEMP__": pdg_temp,
            "__PDG_ITEM_NAME__": item_name,
            "__PDG_ITEM_LABEL__": item_label or item_name,
            # There is no result server. The scheduler learns outcomes from the farm.
            "__PDG_RESULT_SERVER__": "",
        }
        if item_id:
            self._values["__PDG_ITEM_ID__"] = item_id

    def resolve(self, command: str) -> str:
        """The command with every known token replaced. ``__PDG_RESULT_SERVER__`` becomes empty."""
        return _TOKEN.sub(lambda m: self._values.get(m.group(0), m.group(0)), command)


def _find_houdini_python(hfs: str, python_version: str, platform: str) -> str:
    """Houdini's own Python, as pdg.scheduler finds it. It takes no Houdini license."""
    # With no Houdini the shell finds python on PATH. Each platform installs it in its own place.
    if not hfs:
        return "python"
    if platform.startswith("win"):
        return f"{hfs}/python{python_version.replace('.', '')}/python.exe"
    if platform.startswith("darwin"):
        return (
            f"{hfs}/../../../../Python.framework/Versions/{python_version}"
            f"/bin/python{python_version}"
        )
    return f"{hfs}/python/bin/python"


def download_pdg_work_item(
    storage: TaskStorage,
    address: TaskAddress,
    run_data: Mapping[str, Any],
    rules: Sequence[Any],
) -> Tuple[Dict[str, Any], str]:
    """Fetch the payload, map its paths, and write it where PDG's loader looks. Runs in the adaptor's process.

    ``run_data`` is the task's ``pdg`` run data plus the session's ``pdg_dir``, ``scene_file`` and any
    ``output_directories``, and ``rules`` are the session's path mapping rules. Returns the client's
    arguments and its outputs file.
    """
    # The item's name becomes its payload's file name, so it's checked before anything is written.
    payload: WorkItemPayload = WorkItemPayload(storage.get_payload(address)).map_paths(rules)
    name: str = payload.item_name
    _check_item_name(name)
    # The session's PDG_TEMP wins, as it does for PDG's own schedulers.
    pdg_dir: str = str(run_data["pdg_dir"])
    pdg_temp: str = os.environ.get("PDG_TEMP") or os.path.join(pdg_dir, "pdgtemp")
    os.makedirs(pdg_dir, exist_ok=True)
    os.makedirs(os.path.join(pdg_temp, "data"), exist_ok=True)
    work_item_file: str = _work_item_file(pdg_temp, name)
    with gzip.open(work_item_file, "wt", encoding="utf-8") as f:
        json.dump(payload.data, f)
    # A previous attempt's outputs must not be published for this one.
    outputs_file: str = _outputs_file(pdg_temp, name)
    if os.path.lexists(outputs_file):
        os.remove(outputs_file)
    _logger.info("Wrote work item '%s' to %s", name, work_item_file)
    client_args: Dict[str, Any] = {
        "item_name": name,
        "pdg_dir": pdg_dir,
        "pdg_temp": pdg_temp,
        "scene_file": str(run_data.get("scene_file") or ""),
        "record_outputs": bool(run_data.get("record_outputs")),
    }
    # Each extra output folder as this worker sees it, mapped like the payload, next to
    # the workstation's spelling, which the scheduler knows it by.
    listed: List[str] = [str(folder) for folder in run_data.get("output_directories") or ()]
    if listed:
        mapped: List[str] = WorkItemPayload({"folders": listed}).map_paths(rules).data["folders"]
        client_args["mapped_output_directories"] = [list(pair) for pair in zip(mapped, listed)]
    return client_args, outputs_file


def publish_pdg_outputs(storage: TaskStorage, address: TaskAddress, outputs_file: str) -> None:
    """Publish the outputs the client recorded. Runs in the adaptor's process.

    A failure is logged and raised, because the next step reads a missing record as no outputs.
    """
    # Read the record the client wrote, and publish it at the task's address.
    try:
        with open(outputs_file, encoding="utf-8") as f:
            entries: List[Dict[str, Any]] = json.load(f)["outputs"]
        outputs: List[OutputFile] = [
            OutputFile(str(e["path"]), str(e.get("tag") or "")) for e in entries
        ]
        storage.put_outputs(address, outputs)
    except Exception as e:
        _logger.error("Could not publish the work item's outputs: %s", e)
        raise
    _logger.info("Published %d output(s) of the work item", len(outputs))


@dataclass(frozen=True)
class _DownloadedItem:
    """A work item the adaptor downloaded, and the session values it sent with it."""

    # The mapped payload, and the session's values from the adaptor.
    payload: WorkItemPayload
    pdg_dir: str
    pdg_temp: str
    scene_file: str
    record_outputs: bool
    #: Each extra output folder, as (this worker's path, the workstation's path).
    mapped_output_directories: Tuple[Tuple[str, str], ...] = ()


class PdgWorkItemRunner:
    """Runs PDG work items in the Houdini client, which has no storage.

    Each item runs with the session's environment plus its own values, and nothing is kept between items.
    """

    def __init__(self) -> None:
        self._baseline: Dict[str, str] = dict(os.environ)

    def run_work_item(self, args: Dict[str, Any]) -> None:
        """Run the item the adaptor downloaded, record its outputs, and print its exit code.

        An error prints its traceback and gives exit code 1, and the client stays up for the next item.
        """
        # The run sets both, and so does the error handler below.
        exit_code: int
        outputs: List[OutputFile]
        try:
            item: _DownloadedItem = _read_work_item(args)
            self._set_environment(item)
            # Each folder the scan reads, with its file system's time before the command runs.
            started: Dict[str, Optional[int]] = (
                {
                    folder: _read_file_system_time(folder)
                    for folder in (
                        item.pdg_dir,
                        *(worker for worker, _local in item.mapped_output_directories),
                    )
                }
                if item.record_outputs
                else {}
            )
            exit_code, outputs = self._run(item)
            # Only a step with no declared outputs records them.
            if item.record_outputs and exit_code == 0:
                _record_outputs(item, outputs, started)
        except Exception:
            # An escaped error would end the client and every later task, so only this item fails,
            # with exit code 1, even when it ran and exited 0 but its outputs couldn't be recorded.
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            exit_code, outputs = 1, []
        finally:
            self._restore_environment()
        # The adaptor reads the code only at the start of a line, and a command's output
        # may not end with a newline, so the exit code line starts with one.
        print(f"\n{EXIT_CODE_MARKER}{exit_code}", flush=True)

    def _set_environment(self, item: _DownloadedItem) -> None:
        """Set PDG's ``PDG_*`` variables and the item's exported attributes."""
        # The item's exported attributes, then PDG's own variables, which win.
        hfs: str = self._baseline.get("HFS", "")
        variables: Dict[str, str] = item.payload.exported_variables()
        variables.update(
            {
                "PDG_ITEM_NAME": item.payload.item_name,
                "PDG_ITEM_DATA_SOURCE": "0",
                "PDG_TEMP": item.pdg_temp,
                "PDG_DIR": item.pdg_dir,
                "PDG_OUTPUT_DIR": item.pdg_dir,
                "PDG_SCRIPTDIR": (
                    f"{hfs}/houdini/python{_HOUDINI_PYTHON_VERSION}libs/pdgjob" if hfs else ""
                ),
            }
        )
        # PDG's own ID and index, which differ from the task's index on remapped steps.
        work_item: Mapping[str, Any] = item.payload._work_item
        for variable, key in (("PDG_ITEM_ID", "id"), ("PDG_INDEX", "index")):
            if work_item.get(key) is not None:
                variables[variable] = str(work_item[key])
        if hfs:
            variables["HFS"] = hfs
        # The work item's hip attribute points into $PDG_DIR, so the scene is linked there.
        hip: str = _link_scene(item.scene_file, item.pdg_dir)
        if hip:
            variables["HIP"] = os.path.dirname(hip)
        os.environ.update(variables)
        os.environ.pop("PDG_RESULT_SERVER", None)

    def _run(self, item: _DownloadedItem) -> Tuple[int, List[OutputFile]]:
        """Run the item's command in ``$PDG_DIR``. Returns its exit code and the outputs it reported."""
        command: str = item.payload.command
        if not command:
            raise ValueError("The work item payload carries no command.")
        # A stock job script cooks in this Houdini. Anything else runs through a shell.
        cook: Optional[Callable[[Any], Any]] = _find_stock_cook(command)
        if cook is not None:
            # A job script's cook either succeeds or fails, so its exit code is 0 or 1.
            print(f"PdgActions: cooking '{item.payload.item_name}' in process", flush=True)
            succeeded: bool
            cooked: List[OutputFile]
            succeeded, cooked = _cook_in_process(cook, item.pdg_dir)
            return (0 if succeeded else 1), cooked
        # PDG's ID token needs an ID, and a label PDG serialized goes in __pdg_label.
        work_item: Mapping[str, Any] = item.payload._work_item
        item_id: Any = work_item.get("id")
        attributes: Any = work_item.get("attributes")
        label: Any = attributes.get("__pdg_label") if isinstance(attributes, Mapping) else None
        labels: Any = label.get("value") if isinstance(label, Mapping) else None
        # A shell command gets this worker's values for PDG's tokens.
        resolved: str = TokenResolver(
            hfs=self._baseline.get("HFS", ""),
            pdg_dir=item.pdg_dir,
            pdg_temp=item.pdg_temp,
            item_name=item.payload.item_name,
            python_version=_HOUDINI_PYTHON_VERSION,
            item_id="" if item_id is None or isinstance(item_id, bool) else str(item_id),
            item_label=(
                labels[0]
                if isinstance(labels, list) and labels and isinstance(labels[0], str)
                else ""
            ),
        ).resolve(command)
        print(f"PdgActions: running {resolved}", flush=True)
        exit_code: int
        lines: List[str]
        exit_code, lines = _run_shell(resolved, item.pdg_dir)
        return exit_code, _parse_pdg_results(lines, item.pdg_dir)

    def _restore_environment(self) -> None:
        """Put ``os.environ`` back to the session's baseline, so nothing leaks into the next item."""
        # Only changed keys are touched, because os.environ.clear() would unset HFS and HOUDINI_*
        # for a moment in the running Houdini, and Houdini's own threads can read them at any time.
        for key in [k for k in os.environ if k not in self._baseline]:
            del os.environ[key]
        for key, value in self._baseline.items():
            if os.environ.get(key) != value:
                os.environ[key] = value


def _read_work_item(args: Mapping[str, Any]) -> _DownloadedItem:
    """Read the payload the adaptor wrote, with the session values it sent in ``args``."""
    # The name is checked again, because it becomes a file name here too.
    name: str = str(args.get("item_name") or "")
    _check_item_name(name)
    pdg_temp: str = str(args["pdg_temp"])
    with gzip.open(_work_item_file(pdg_temp, name), "rt", encoding="utf-8") as f:
        payload: WorkItemPayload = WorkItemPayload(json.load(f))
    return _DownloadedItem(
        payload=payload,
        pdg_dir=str(args["pdg_dir"]),
        pdg_temp=pdg_temp,
        scene_file=str(args.get("scene_file") or ""),
        record_outputs=bool(args.get("record_outputs")),
        mapped_output_directories=tuple(
            (str(worker), str(local))
            for worker, local in args.get("mapped_output_directories") or ()
        ),
    )


def _record_outputs(
    item: _DownloadedItem, outputs: Sequence[OutputFile], started: Mapping[str, Optional[int]]
) -> None:
    """Write the outputs for the adaptor to publish, with paths tokenized under ``__PDG_DIR__``.

    A command that reported none gets a scan for the files it wrote since each folder's
    time in ``started``, in ``$PDG_DIR`` and in each extra output folder, which is
    recorded by the workstation's path.
    """
    # The scan leaves out the payloads and the linked scene, which the command didn't write.
    scanned: bool = not outputs
    hip_link: str = os.path.join(item.pdg_dir, os.path.basename(item.scene_file))
    recorded: List[OutputFile] = (
        _scan_outputs(
            item.pdg_dir,
            exclude=(item.pdg_temp, hip_link if item.scene_file else ""),
            since=started[item.pdg_dir],
        )
        if scanned
        else list(outputs)
    )
    folders: Tuple[Tuple[str, str], ...] = item.mapped_output_directories if scanned else ()
    record: List[Dict[str, str]] = [
        {"path": _tokenize(o.path, item.pdg_dir), "tag": o.tag} for o in recorded
    ]
    # A scan also covers each extra output folder, recorded by the workstation's path,
    # which is the one the scheduler and PDG know.
    for worker, local in folders:
        record.extend(
            {
                "path": local.rstrip("/\\")
                + "/"
                + Path(o.path).relative_to(Path(os.path.abspath(worker))).as_posix(),
                "tag": o.tag,
            }
            for o in _scan_outputs(worker, since=started[worker])
        )
    # An error fails the item, since a success with no record gives the next step wrong inputs.
    with open(_outputs_file(item.pdg_temp, item.payload.item_name), "w", encoding="utf-8") as f:
        json.dump({"outputs": record}, f)
    print(f"PdgActions: recorded {len(record)} output(s)", flush=True)


def _work_item_file(pdg_temp: str, name: str) -> str:
    """Where ``pdgjson.getWorkItemJsonPath`` looks when ``PDG_ITEM_DATA_SOURCE`` is 0."""
    return os.path.join(pdg_temp, "data", f"{name}.json.gz")


def _outputs_file(pdg_temp: str, name: str) -> str:
    """Where the client hands the outputs over. Under ``$PDG_TEMP``, so the scan never reports it."""
    return os.path.join(pdg_temp, "data", f"{name}.outputs.json")


def _check_item_name(name: str) -> None:
    """Raise unless the name can be the file name of the item's payload, on any platform."""
    if not name:
        raise ValueError("The work item payload carries no work item name.")
    if name in (".", "..") or any(c in name for c in ("/", "\\", "\0", ":")):
        raise ValueError(f"The work item name {name!r} is not a single file name.")


def _format_exported_attribute(attribute: Any) -> Optional[str]:
    """The attribute's environment value, or None when PDG wouldn't export it."""
    if not isinstance(attribute, Mapping):
        return None
    # Only EnvExport without Internal is exported. A malformed flag exports nothing.
    try:
        flags: int = int(attribute.get("flag") or 0)
    except (TypeError, ValueError):
        return None
    if flags & _INTERNAL_FLAG or not flags & _ENV_EXPORT_FLAG:
        return None
    values: Any = attribute.get("value")
    if not isinstance(values, list) or not values:
        return None
    # A file attribute's values are objects, and they are never an environment value.
    parts: List[str] = []
    kind: Any = attribute.get("type")
    for value in values:
        if isinstance(value, (dict, list)):
            return None
        # Each value as PDG prints it, by the attribute's type.
        try:
            parts.append(
                str(float(value))
                if kind == _FLOAT_ATTRIBUTE
                else str(int(value)) if kind == _INT_ATTRIBUTE else str(value)
            )
        except (TypeError, ValueError, OverflowError):
            # A value that doesn't fit its type is exported as is, so malformed input never raises.
            parts.append(str(value))
    # PDG joins a multi-component attribute with spaces, as hscript does.
    return " ".join(parts)


# A path embedded in a longer string, such as an ImageMagick argument list, starts
# after one of these, and runs to the next quote or whitespace.
_EMBEDDED_START: FrozenSet[str] = frozenset(" \t\n\"'=(,;")
_EMBEDDED_END: Pattern[str] = re.compile(r"[\"'\s]")
# A rule's apply reads repeated slashes and "." segments as one separator, so the prefix check does too.
_EXTRA_SEPARATORS: Pattern[str] = re.compile(r"/(?:\.?/)+")


def _normalize_slashes(path: str) -> str:
    """``path`` with forward slashes, one per separator, and no ``.`` segments, for comparing prefixes."""
    return _EXTRA_SEPARATORS.sub("/", path.replace("\\", "/"))


def _map_embedded_paths(
    value: str, appliers: Sequence[Tuple[str, Callable[..., Tuple[bool, str]]]]
) -> str:
    """``value`` with each path embedded in it mapped by the first rule that matches that path.

    Each candidate still goes through the rule, so a rule for ``/a`` never rewrites ``/ab``.
    A source is found in either slash, because Houdini writes Windows paths with ``/``.
    """
    # Compare normalized copies, so a rule for C:\proj finds C:/proj and C://proj. The case still has to match.
    # A string that holds no rule's source is returned as it is.
    normalized: str = _normalize_slashes(value)
    normalized_rules: List[Tuple[str, Callable[..., Tuple[bool, str]]]] = [
        (_normalize_slashes(source), apply) for source, apply in appliers if source
    ]
    if not any(source in normalized for source, _apply in normalized_rules):
        return value
    # Walk the string, copying each character or the mapped path that starts there.
    pieces: List[str] = []
    piece: str
    i: int = 0
    while i < len(value):
        i, piece = _map_path_at(value, i, normalized_rules)
        pieces.append(piece)
    return "".join(pieces)


def _map_path_at(
    value: str, i: int, normalized_rules: Sequence[Tuple[str, Callable[..., Tuple[bool, str]]]]
) -> Tuple[int, str]:
    """The mapped path that starts at ``i`` and the index after it, else the one character there."""
    # A path can start only after a separator, and it runs to the next quote or space.
    if i > 0 and value[i - 1] in _EMBEDDED_START:
        match: Optional[re.Match[str]] = _EMBEDDED_END.search(value, i)
        j: int = match.start() if match else len(value)
        run: str = _normalize_slashes(value[i:j])
        # The first rule that maps the run wins.
        for source, apply in normalized_rules:
            if not run.startswith(source):
                continue
            # The rule sees the whole run, so a rule for /a never maps /ab.
            changed: bool
            mapped: str
            changed, mapped = apply(path=value[i:j])
            if changed:
                return j, mapped
    return i + 1, value[i]


def _link_scene(scene_file: str, pdg_dir: str) -> str:
    """Link the scene into ``$PDG_DIR``, where the work item's ``hip`` attribute points. Returns the scene, or ``""``."""
    if not os.path.isfile(scene_file):
        return ""
    # A link that can't be made is only a warning, because HIP still names the scene.
    link: str = os.path.join(pdg_dir, os.path.basename(scene_file))
    if not os.path.lexists(link):
        try:
            os.symlink(scene_file, link)
        except OSError as e:
            print(f"WARNING: Could not link {link} to {scene_file}: {e}", flush=True)
    return scene_file


def _find_stock_cook(command: str) -> Optional[Callable[[Any], Any]]:
    """The ``cook`` of the PDG job script the command starts, or None when it starts something else."""
    match: Optional[re.Match[str]] = _STOCK_COMMAND.match(command)
    if not match:
        return None
    # A script PDG doesn't ship, or one with no cook, runs through the shell instead.
    try:
        module: Any = importlib.import_module(f"pdgjob.{match.group(1)}")
    except Exception:
        return None
    cook: Any = getattr(module, "cook", None)
    return cook if callable(cook) else None


def _cook_in_process(cook: Callable[[Any], Any], pdg_dir: str) -> Tuple[bool, List[OutputFile]]:
    """Cook the item as ``pdg.types.cookWorkItem`` does. Returns success and the item's outputs."""
    previous: str = os.getcwd()
    os.chdir(pdg_dir)
    try:
        pdgjson: Any = importlib.import_module("pdgjob.pdgjson")
        # The first item loaded in a process stays PDG's global work item, so every
        # later `@attr` would read it. PDG's own service clients clear it first.
        importlib.import_module("pdg").EvaluationContext.setGlobalWorkItem(None, False)
        work_item: Any = pdgjson.WorkItem.fromJobEnvironment(as_native=True)
        _load_scene(work_item)
        # A job script's cook returns nothing, or whether it succeeded.
        result: Any = cook(work_item)
        succeeded: bool = result is None or bool(result)
        outputs: List[OutputFile] = [
            OutputFile(f.path.replace(PDG_DIR_TOKEN, pdg_dir), f.tag or "")
            for f in getattr(work_item, "outputFiles", None) or []
        ]
        return succeeded, outputs
    except SystemExit as e:
        # PDG's loader and job scripts call exit() on some failures.
        return e.code in (None, 0), []
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        return False, []
    finally:
        os.chdir(previous)


def _load_scene(work_item: Any) -> None:
    """Load and remap the item's ``.hip`` the way the ROP path loads its scene.

    Every item gets a fresh scene, so a simulation cooked for one wedge restarts for the next.
    The stock ``RopCooker`` is then told the scene is loaded.
    """
    # No scene, or a missing one, is left for the job script to report in its own words.
    try:
        hip: str = work_item.localizePath(work_item.fileAttribValue("hip").path).replace("\\", "/")
        modified: float = os.path.getmtime(hip)
    except Exception:
        return
    # Imported here because the handler imports hou, which only the client has.
    try:
        from deadline.houdini_adaptor.HoudiniClient.houdini_handler import HoudiniHandler
    except ImportError:
        from houdini_adaptor.HoudiniClient.houdini_handler import (  # type: ignore[import-not-found,no-redef]
            HoudiniHandler,
        )
    # A scene that fails to load is left to the job script, which loads it itself.
    try:
        HoudiniHandler().set_scene_file({"scene_file": hip})
    except Exception as e:
        print(f"WARNING: Could not load {hip} ahead of the job script: {e}", flush=True)
        return
    # Tell PDG's stock RopCooker the scene is loaded. A Houdini without pdgjob.rop has none.
    with contextlib.suppress(Exception):
        importlib.import_module("pdgjob.rop").RopCooker.hip_modtime = modified


def _run_shell(command: str, cwd: str) -> Tuple[int, List[str]]:
    """Run the command, echoing its output as it arrives. Returns the exit code and the ``PDG_RESULT`` lines."""
    results: List[str] = []
    process: subprocess.Popen = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    # Each line goes to the adaptor's handlers at once, and the result lines are kept.
    # A line that looks like the exit code line is prefixed, so it can't end the task early.
    assert process.stdout is not None
    for line in process.stdout:
        echoed: str = f"> {line}" if line.startswith(EXIT_CODE_MARKER) else line
        print(echoed, end="", flush=True)
        if _RESULT_PREFIX in line:
            results.append(line)
    return process.wait(), results


def _parse_pdg_results(lines: Iterable[str], pdg_dir: str) -> List[OutputFile]:
    """The files named by ``PDG_RESULT: <id>;<subindex>;<path>;<tag>;<hash>`` lines, in order."""
    outputs: List[OutputFile] = []
    for line in lines:
        if _RESULT_PREFIX not in line:
            continue
        # A line with no path names no file, so it adds nothing.
        fields: List[str] = line.split(_RESULT_PREFIX, 1)[1].strip().split(";")
        if len(fields) < 3 or not fields[2]:
            continue
        tag: str = fields[3] if len(fields) > 3 else ""
        outputs.append(OutputFile(fields[2].replace(PDG_DIR_TOKEN, pdg_dir), tag))
    return outputs


def _read_file_system_time(folder: str) -> Optional[int]:
    """The folder's file system time now, in nanoseconds, or None when the folder doesn't exist yet.

    It's the mtime of a hidden file written there, because the file system's clock can lag
    this process's, and the scan compares it with mtimes from the same clock.
    """
    handle: int
    marker: str
    try:
        handle, marker = tempfile.mkstemp(prefix=".pdg-start-", dir=folder)
    except FileNotFoundError:
        return None
    # Removed before the command runs, so no run, even a failed one, leaves it as an output.
    try:
        os.close(handle)
        return os.stat(marker).st_mtime_ns
    finally:
        os.remove(marker)


def _scan_outputs(
    output_dir: str, *, exclude: Sequence[str] = (), since: Optional[int] = None
) -> List[OutputFile]:
    """Every regular file under the directory, for a command that reported none.

    Skips hidden files, the excluded paths, and, with ``since``, files modified before that
    file system time in nanoseconds, so the inputs synced before the task are not reported.
    """
    root: Path = Path(os.path.abspath(output_dir))
    if not root.is_dir():
        return []
    excluded: List[Path] = [Path(os.path.abspath(e)) for e in exclude if e]

    def skipped(path: Path) -> bool:
        # Hidden names, and the excluded paths with everything under them.
        return path.name.startswith(".") or any(path.is_relative_to(e) for e in excluded)

    # Leave out skipped folders before the walk lists them. Folder links aren't followed.
    found: List[Path] = []
    errors: List[OSError] = []
    for folder, folders, files in os.walk(root, onerror=errors.append):
        folders[:] = [name for name in folders if not skipped(Path(folder, name))]
        found.extend(Path(folder, name) for name in files)
    # A folder the walk couldn't list fails the item, instead of recording part of it.
    if errors:
        raise errors[0]
    # Sorted, so the same tree always gives the same list.
    outputs: List[OutputFile] = []
    for path in sorted(found):
        # Skip hidden and excluded files, anything that isn't a regular file, and older files.
        if skipped(path) or not path.is_file():
            continue
        if since is not None and path.stat().st_mtime_ns < since:
            continue
        outputs.append(OutputFile(str(path)))
    return outputs


def _tokenize(path: str, pdg_dir: str) -> str:
    """The path with ``pdg_dir`` replaced by ``__PDG_DIR__``, only where it ends at a path separator.

    The rest of the path uses ``/`` for this OS's separator, so a cook on any OS can resolve it.
    On Windows, ``\\`` and ``/`` match each other.
    """
    root: str = pdg_dir.rstrip("/\\")
    if not root:
        return path
    # Windows takes either separator, and Houdini writes its paths with /, so compare with \ read as /.
    windows: bool = os.sep == "\\"
    seen: str = path.replace("\\", "/") if windows else path
    folder: str = root.replace("\\", "/") if windows else root
    # Match the folder only as a whole path segment, so /work/pdg never tokenizes /work/pdg2.
    if seen == folder:
        return PDG_DIR_TOKEN
    if seen.startswith(folder) and seen[len(folder)] in "/\\":
        return PDG_DIR_TOKEN + seen[len(folder) :]
    return path
