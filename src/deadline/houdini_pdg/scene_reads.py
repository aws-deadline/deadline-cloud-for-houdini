# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Attribute the files a step's scene reads to the steps that write them.

A Houdini task opens files named by parms inside the scene, and the TOP graph
does not know about them. Matching each read to the step whose outputs cover it
yields the data edges the TOP wires miss.

A read whose path is driven by ``@attr`` or an expression cannot be evaluated
outside a work item. It is matched by its static prefix, the authored text before
the first expression, against the files each step writes. A read that is only an
expression, such as `` `@pdg_input` ``, stays invisible. The matching favors
precision over recall.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from types import ModuleType
from typing import (
    AbstractSet,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
)

from .graph import PDG_DIR_TOKEN, GraphSnapshot

#: File-reference parms that are not data reads: debug artifacts, labels, and
#: pre/post scripts.
IGNORE_PARMS: FrozenSet[str] = frozenset(
    {
        "descriptivelabel",
        "prerender",
        "preframe",
        "postframe",
        "postwrite",
        "postrender",
        "prerender_script",
        "taskgraphfile",
        "pdg_workingdir",
        "soho_program",
        "perffile",
        "debughip",
    }
)

#: Values that name something inside Houdini rather than a file on disk. The
#: submitter's attachment scan skips the same prefixes.
IGNORE_VALUE_PREFIXES: Tuple[str, ...] = ("opdef:", "oplib:", "temp:", "op:")

#: Parms that name where a node writes. Matching a write as a read would invent a
#: dependency, so these are never matched; the path checks still examine them.
OUTPUT_PARMS: FrozenSet[str] = frozenset(
    {
        "destfile",  # File Copy
        "destfolder",  # File Copy
        "outputfile",  # JSON Output
        "outputpath",  # Invoke
        "copoutput",  # Composite ROP
        "sopoutput",  # Geometry ROP
        "dopoutput",  # Dynamics ROP
        "lopoutput",  # USD ROP
        "outputfilepath",  # ImageMagick, FFmpeg Encode Video
        "outputimage",
        "output",  # Heightfield Output
        "vm_picture",  # Mantra
        "picture",  # Karma, OpenGL
        "vm_dcmfilename",
        "vm_deepresolver",
        "ar_picture",  # Arnold
        "RS_outputFileNamePrefix",  # Redshift
    }
)

#: Write parms whose name other node types use for a read, such as Geometry
#: Import's ``filepath``. They count as writes only on these node types.
OUTPUT_PARMS_BY_NODE_TYPE: Mapping[str, FrozenSet[str]] = {
    "textoutput": frozenset({"filepath"}),
    "csvoutput": frozenset({"filepath", "csvfilepath"}),
}


def is_output_parm(node_type: str, parm: str) -> bool:
    """Whether ``parm`` names where a node of type ``node_type`` writes."""
    return parm in OUTPUT_PARMS or parm in OUTPUT_PARMS_BY_NODE_TYPE.get(node_type, frozenset())


# Prefixes that resolve on a worker: $HIP, $PDG_DIR, $PDG_TEMP, and PDG's own
# tokens. Path mapping rewrites literal paths, not Houdini variables, so any other
# authored prefix points at the workstation. A variable counts only as a whole
# token: ``$HIPNAME`` and ``$HIP_OLD`` are other variables.
_PORTABLE_PREFIX: Pattern[str] = re.compile(
    r"\$(?:HIP|PDG_DIR|PDG_TEMP)(?!\w)|\$\{(?:HIP|PDG_DIR|PDG_TEMP)\}|__PDG_DIR__|__PDG_TEMP__"
)

# ``$NAME`` or ``${NAME}``: group 1 holds the braced name, group 2 the bare one.
# Shared so a read and its check agree on which text is a variable.
VARIABLE_PATTERN: Pattern[str] = re.compile(r"\$\{(\w+)\}|\$(\w+)")


@dataclass(frozen=True)
class SceneRead:
    """One file-reference parm that a TOP node's work evaluates."""

    #: The TOP node whose walk reached the parm. Usually a step, but a node that
    #: maps to no step can be walked too.
    step: str
    node_path: str
    parm: str
    #: As authored, tokens intact.
    raw: str
    #: As evaluated on the workstation.
    path: str
    is_output: bool = False
    #: When ``raw`` depends on a work item, the authored text before the first
    #: expression, evaluated. ``""`` when it cannot be evaluated.
    static_prefix: str = ""
    #: The ``$NAME`` variables in ``raw`` this Houdini session doesn't define, as
    #: spelled. Houdini expands each one to nothing.
    undefined_variables: Tuple[str, ...] = ()
    #: Whether the parm is a File Cache's ``file`` while its write uses it. Only the
    #: attribute check counts it as a write, so steps that load the cache keep their edge.
    is_file_cache_write: bool = False


@dataclass(frozen=True)
class SceneReads:
    """Every step's scene reads, as the scene walk collected them."""

    reads: Tuple[SceneRead, ...] = ()
    #: Scene nodes visited, by step.
    walked: Mapping[str, int] = field(default_factory=dict)
    #: False when the walk failed, so validation can say the path checks did not run.
    available: bool = True


class ReadVerdict(Enum):
    """How a read relates to the steps in the job."""

    #: The step's own output.
    SELF = "self"
    #: Produced by a step this one already depends on.
    COVERED = "covered"
    #: Produced by a step this one did not depend on: a data edge adds it.
    EDGE_ADDED = "edge_added"
    #: No step produces it. Job attachments carry it, or nothing does.
    EXTERNAL = "external"


@dataclass(frozen=True)
class ReadMatch:
    """A read, attributed to its producing step."""

    read: SceneRead
    #: ``""`` when no step produces the file.
    producer: str
    verdict: ReadVerdict
    #: Other steps whose outputs also cover the path: steps that write into the
    #: same directory, or, for a read matched by its static prefix, the other
    #: steps that write under that prefix. A prefix read the reading step writes
    #: under itself is one ``SELF`` match that names no other step.
    ambiguous_with: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AttributeVariableUse:
    """A write parm that spells a work item attribute as a Houdini variable.

    ``$NAME`` expands to nothing on the workstation, where PDG records the path.
    It resolves on the worker only when the attribute is exported, so the recorded
    and written paths differ, or every work item writes one file. The fix is
    `` `@name` ``.
    """

    step: str
    node_path: str
    parm: str
    raw: str
    #: As the parm spells it, for example ``WEDGEINDEX``.
    variable: str
    #: As PDG spells it, for example ``wedgeindex``.
    attribute: str


@dataclass(frozen=True)
class ReadReport:
    """Everything the reads say about one snapshot."""

    matches: Tuple[ReadMatch, ...] = ()
    #: By consuming step. Includes covered matches, so existing edges that carry
    #: data are protected too.
    data_edges: Mapping[str, FrozenSet[str]] = field(default_factory=dict)
    #: Reads that resolve inside the working directory but are authored with a
    #: workstation path.
    unportable: Tuple[SceneRead, ...] = ()
    attribute_variables: Tuple[AttributeVariableUse, ...] = ()
    #: Every step read that is not a write, so a check can ask which steps read a
    #: given file.
    step_reads: Tuple[SceneRead, ...] = ()
    #: Reads by TOP nodes that map to no step, each matched like a step's read.
    #: They add no data edge.
    node_reads: Tuple[ReadMatch, ...] = ()
    #: Write parms of TOP nodes that map to no step.
    node_writes: Tuple[SceneRead, ...] = ()


def match_reads(
    snapshot: GraphSnapshot,
    reads: Sequence[SceneRead],
    working_dir: str,
    *,
    output_directories: Sequence[str] = (),
) -> ReadReport:
    """Attribute each read to the step whose outputs cover it.

    A plain read takes an exact output directory over the deepest enclosing root,
    with every test stopping at a path separator, and ignores a root at or above
    ``working_dir`` or one of ``output_directories``, which would cover every step
    writing there. A read that depends on a work item is matched by its static
    prefix instead, against every step's files. Reads by nodes that map to no step
    are matched the same way, but kept apart in ``node_reads`` and ``node_writes``.
    """
    # The job's folders, which no root or prefix may hold.
    folders: Tuple[str, ...] = job_folders(working_dir, output_directories)
    producers: Tuple[_Producer, ...] = _output_roots(snapshot, working_dir, folders)
    existing: Dict[str, Set[str]] = {step.name: set(step.dependencies) for step in snapshot.steps}
    # A node that maps to no step adds no edge, so its reads are set aside here.
    skipped: Set[str] = set(snapshot.skipped_nodes)
    own: List[SceneRead] = [read for read in reads if read.step not in skipped]
    others: List[SceneRead] = [read for read in reads if read.step in skipped]

    matches: List[ReadMatch] = []
    edges: Dict[str, Set[str]] = {}
    for read in own:
        if read.is_output:
            # A write is not a read. Two File Copy nodes writing one sequence at
            # different frame offsets would otherwise depend on each other.
            continue
        # Covered matches count too, so an existing edge that carries data is kept.
        for match in _match_read(read, producers, existing, folders):
            matches.append(match)
            if match.verdict in (ReadVerdict.COVERED, ReadVerdict.EDGE_ADDED):
                edges.setdefault(read.step, set()).add(match.producer)

    # Reads by nodes that are not steps are matched the same way, but kept apart.
    node_reads: List[ReadMatch] = [
        match
        for read in others
        if not read.is_output
        for match in _match_read(read, producers, existing, folders)
    ]
    return ReadReport(
        matches=tuple(matches),
        data_edges={step: frozenset(found) for step, found in sorted(edges.items())},
        unportable=_unportable(own, working_dir),
        attribute_variables=_attribute_variables(own, snapshot),
        step_reads=tuple(read for read in own if not read.is_output),
        node_reads=tuple(node_reads),
        node_writes=tuple(read for read in others if read.is_output),
    )


def job_folders(
    working_dir: str, output_directories: Sequence[str], paths: ModuleType = os.path
) -> Tuple[str, ...]:
    """The folders the job uploads: the working directory, then each extra output folder, normalized."""
    return tuple(paths.normpath(folder) for folder in (working_dir, *output_directories) if folder)


def _path_key(path: str, paths: ModuleType = os.path) -> str:
    """``path`` spelled one way, for comparing it with another path.

    It is normalized, so ``/`` and ``\\`` are one separator on Windows, and its case
    is folded where the filesystem ignores case. ``paths`` is ``os.path``, or
    ``ntpath`` in a test of Windows rules.
    """
    return paths.normcase(paths.normpath(path))


def reads_opening(
    files: AbstractSet[str],
    reads: Sequence[SceneRead],
    working_dir: str,
    paths: ModuleType = os.path,
    *,
    output_directories: Sequence[str] = (),
) -> Tuple[SceneRead, ...]:
    """The reads that open one of ``files``, with both sides compared by ``_path_key``.

    A plain read matches by its path. A read that depends on a work item matches
    when a file starts with its static prefix, with the same guard on the job's
    folders as producer matching. Houdini gives ``/`` paths on Windows too, so they
    match the same files spelled with ``\\``.
    """
    # Both sides are spelled one way, so "/" and "\" paths match on Windows.
    keys: Set[str] = {_path_key(path, paths) for path in files if path}
    folders: List[str] = [
        _path_key(folder, paths) for folder in job_folders(working_dir, output_directories, paths)
    ]
    found: List[SceneRead] = []
    for read in reads:
        if not _depends_on_work_item(read.raw):
            if read.path and _path_key(read.path, paths) in keys:
                found.append(read)
            continue
        # A prefix with no directory, or one that holds one of the job's folders,
        # would match everything, so it matches nothing.
        prefix, directory = _normalize_prefix(read.static_prefix, paths)
        if not directory or any(
            _is_below(folder, paths.normcase(directory), paths) for folder in folders
        ):
            continue
        folded: str = paths.normcase(prefix)
        if any(key.startswith(folded) for key in keys):
            found.append(read)
    return tuple(found)


def _match_read(
    read: SceneRead,
    producers: Sequence[_Producer],
    existing: Mapping[str, Set[str]],
    folders: Sequence[str],
) -> List[ReadMatch]:
    """The read's matches: by static prefix when it depends on a work item, else one by its path."""
    if _depends_on_work_item(read.raw):
        # Evaluated outside any work item, its path means nothing, so it is
        # matched by its static prefix. A prefix that matches no step adds no
        # match at all, so no check judges the meaningless path.
        return _prefix_matches(read, producers, existing, folders)
    # A plain read has one producer, or none, and a verdict from how the
    # reading node relates to it.
    producer, candidates = _producer_of(read.path, producers, prefer=read.step)
    return [
        ReadMatch(
            read=read,
            producer=producer,
            verdict=_verdict(read.step, producer, existing),
            ambiguous_with=tuple(s for s in candidates if s != producer),
        )
    ]


def _depends_on_work_item(raw: str) -> bool:
    """Whether an authored value holds a backtick expression or any ``@``, so only a work item can evaluate it.

    Any ``@`` counts: PDG expands ``@name`` without backticks, and telling an
    attribute from a literal ``@`` would need the item's attribute names.
    """
    return "`" in raw or "@" in raw


def _verdict(reader: str, producer: str, existing: Mapping[str, Set[str]]) -> ReadVerdict:
    """How the reading step relates to the producer: none, itself, a dependency, or a new edge."""
    # Checked in this order, so a step's own output is SELF whatever its dependencies.
    if not producer:
        return ReadVerdict.EXTERNAL
    if producer == reader:
        return ReadVerdict.SELF
    if producer in existing.get(reader, set()):
        return ReadVerdict.COVERED
    return ReadVerdict.EDGE_ADDED


@dataclass(frozen=True)
class _Producer:
    step: str
    #: Every directory the step declares an output in.
    directories: FrozenSet[str]
    #: Every output path the step declares, resolved like ``directories``.
    files: FrozenSet[str]
    #: The deepest directory holding all of them, or ``""`` when that would sit
    #: at or above one of the job's folders.
    root: str


def _resolve(path: str, working_dir: str) -> str:
    """A declared path with PDG's working-directory token and variables expanded, normalized."""
    return os.path.normpath(os.path.expandvars(path.replace(PDG_DIR_TOKEN, working_dir)))


def _is_below(path: str, ancestor: str, paths: ModuleType = os.path) -> bool:
    """Whether ``path`` is ``ancestor`` or under it. ``/a/bc`` is not under ``/a/b``.

    ``paths`` is ``os.path``, or ``ntpath`` in a test of Windows rules.
    """
    if not path or not ancestor:
        return False
    # Fold case where the platform ignores it, so C:\Work is under c:\work on Windows.
    child: str = paths.normcase(path)
    parent: str = paths.normcase(ancestor)
    if child == parent:
        return True
    # Compare against the ancestor plus a separator, so a sibling whose name only
    # starts with the ancestor's is not counted as under it.
    return child.startswith(parent.rstrip(paths.sep) + paths.sep)


def _has_root(path: str, paths: ModuleType = os.path) -> bool:
    """Whether ``path`` starts at a root, so the current folder can't change where it points.

    On Windows a root is a leading ``/`` or ``\\``, which lands on the current drive, or
    one after a drive, as in ``C:/a``. ``paths`` is ``os.path``, or ``ntpath`` in a test.
    """
    # Python 3.13's isabs no longer counts a leading separator with no drive, so
    # that case is checked here, and the answer is the same on every Python.
    return path[:1] in (paths.sep, paths.altsep) or paths.isabs(path)


def _output_roots(
    snapshot: GraphSnapshot, working_dir: str, folders: Sequence[str]
) -> Tuple[_Producer, ...]:
    """Each step's output files and directories, and the root that identifies the step.

    The root lets a read evaluated at one wedge or frame match outputs declared
    for every wedge: ``geo/cache/wedge0..N`` share the root ``geo/cache``.
    """
    producers: List[_Producer] = []
    for step in snapshot.steps:
        # Only outputs with a directory count. A bare file name says nothing
        # about where the file lands.
        resolved: Set[str] = {
            _resolve(declared.path, working_dir)
            for item in step.items
            for declared in item.declared_outputs
        }
        files: Set[str] = {path for path in resolved if os.path.dirname(path)}
        if not files:
            continue
        directories: Set[str] = {os.path.dirname(path) for path in files}
        root: str = _root(directories, folders)
        producers.append(_Producer(step.name, frozenset(directories), frozenset(files), root))
    return tuple(producers)


def _root(directories: Set[str], folders: Sequence[str]) -> str:
    """The deepest directory holding all of ``directories``, or ``""``.

    It is ``""`` when the directories mix absolute and relative paths, or when the
    root would hold one of the job's folders and so cover every read in it.
    """
    try:
        root: str = os.path.commonpath(sorted(directories))
    except ValueError:  # absolute and relative mixed
        return ""
    return "" if any(_is_below(folder, root) for folder in folders) else root


def _producer_of(
    path: str, producers: Sequence[_Producer], prefer: str
) -> Tuple[str, Tuple[str, ...]]:
    """The producing step, and every step that matched equally well.

    The reading step is tried first. When two steps write into one directory,
    directory matching cannot tell them apart, and attributing a step's own
    directory to a sibling would invent a dependency. The cost, a genuine read of
    a sibling's file in a shared directory going unattributed, is recorded in
    ``ambiguous_with``.
    """
    directory: str = os.path.dirname(os.path.normpath(path)) if path else ""
    if not directory:
        return "", ()
    ordered: List[_Producer] = sorted(producers, key=lambda p: p.step != prefer)

    # An exact output directory wins outright.
    exact: Tuple[str, ...] = tuple(p.step for p in ordered if directory in p.directories)
    if exact:
        return exact[0], exact

    # Otherwise the deepest root that holds the directory wins, and roots at the
    # same depth tie.
    best_depth: int = -1
    at_best: List[str] = []
    for producer in ordered:
        if not producer.root or not _is_below(directory, producer.root):
            continue
        depth: int = producer.root.rstrip(os.sep).count(os.sep)
        if depth > best_depth:
            best_depth, at_best = depth, [producer.step]
        elif depth == best_depth:
            at_best.append(producer.step)
    return (at_best[0], tuple(at_best)) if at_best else ("", ())


def _normalize_prefix(prefix: str, paths: ModuleType = os.path) -> Tuple[str, str]:
    """The prefix with its directory part normalized, and that directory.

    The last component is kept as typed, because it is where the expression
    starts, so it may be a partial file name such as ``cache.``; a trailing
    separator is kept too. Both are ``""`` when the prefix has no directory.
    """
    # Only the part before the last separator is a directory that can be normalized.
    head, tail = paths.split(prefix)
    if not head:
        return "", ""
    directory: str = paths.normpath(head)
    return paths.join(directory, tail), directory


def _prefix_matches(
    read: SceneRead,
    producers: Sequence[_Producer],
    existing: Mapping[str, Set[str]],
    folders: Sequence[str],
) -> List[ReadMatch]:
    """One match for each step with a declared output path that starts with the read's static prefix.

    The test is on the string, not separator-bounded, so a prefix may end inside a
    directory or a file name: ``$HIP/geo/$OS/wedge`@wedgeindex`/...`` has the prefix
    ``<hip>/geo/pyro_cache/wedge``, which covers ``wedge0`` to ``wedgeN``. A prefix
    whose directory is at or above one of the job's folders would cover every step
    writing there, so it matches nothing. When the reading step writes there
    itself, the read is its own.
    """
    # A prefix with no directory, or one that holds one of the job's folders, would
    # match nothing useful or everything, so it matches nothing.
    prefix, directory = _normalize_prefix(read.static_prefix)
    if not directory or any(_is_below(folder, directory) for folder in folders):
        return []
    # A step writing under the prefix itself is most likely reading its own output,
    # so that one SELF match stands alone and names no other step.
    found: List[str] = [p.step for p in producers if any(f.startswith(prefix) for f in p.files)]
    if read.step in found:
        return [ReadMatch(read=read, producer=read.step, verdict=ReadVerdict.SELF)]
    # Every producer gets its own match and edge, since the read can open files
    # from any of them, and each names the others.
    return [
        ReadMatch(
            read=read,
            producer=producer,
            verdict=_verdict(read.step, producer, existing),
            ambiguous_with=tuple(s for s in found if s != producer),
        )
        for producer in found
    ]


def _unportable(reads: Sequence[SceneRead], working_dir: str) -> Tuple[SceneRead, ...]:
    """Reads that resolve inside the working directory but are not authored relative to it.

    The working directory moves on a worker and a literal path does not, so such
    a parm names this workstation. Paths outside the working directory are a
    different problem, for job attachments.
    """
    if not working_dir:
        return ()
    root: str = os.path.normpath(working_dir)
    return tuple(
        read
        for read in reads
        if not _PORTABLE_PREFIX.match((read.raw or "").strip())
        and _is_below(_resolve(read.path, working_dir), root)
    )


def _attribute_variables(
    reads: Sequence[SceneRead], snapshot: GraphSnapshot
) -> Tuple[AttributeVariableUse, ...]:
    """Write parms that spell one of the same step's attributes as ``$NAME``.

    An exported attribute always counts. Any other attribute counts when this
    session doesn't define ``$NAME``, so ``$HIP`` is never one, though ``hip`` is an
    attribute. Scoped per step: a variable is judged only against its own step's attributes.
    """
    # Each step's attribute names, keyed case-folded so ``$WEDGEINDEX`` finds
    # ``wedgeindex``, with PDG's own spelling as the value.
    exported: Dict[str, Dict[str, str]] = {
        step.name: {name.casefold(): name for name in step.exported_attribute_names}
        for step in snapshot.steps
    }
    carried: Dict[str, Dict[str, str]] = {
        step.name: {name.casefold(): name for name in step.attribute_names}
        for step in snapshot.steps
    }
    found: List[AttributeVariableUse] = []
    for read in reads:
        # Only writes are judged: a read that names a variable is not recorded.
        if not (read.is_output or read.is_file_cache_write) or read.step not in exported:
            continue
        for braced, bare in VARIABLE_PATTERN.findall(read.raw or ""):
            variable: str = braced or bare
            attribute: Optional[str] = exported[read.step].get(variable.casefold())
            # A carried attribute counts only when the session expands ``$NAME`` to nothing.
            if attribute is None and variable in read.undefined_variables:
                attribute = carried[read.step].get(variable.casefold())
            if attribute is not None:
                found.append(
                    AttributeVariableUse(
                        step=read.step,
                        node_path=read.node_path,
                        parm=read.parm,
                        raw=read.raw,
                        variable=variable,
                        attribute=attribute,
                    )
                )
    return tuple(found)
