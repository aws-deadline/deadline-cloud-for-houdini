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
from typing import Dict, FrozenSet, List, Mapping, Optional, Pattern, Sequence, Set, Tuple

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

# Prefixes that resolve on a worker: $HIP, $PDG_DIR, $PDG_TEMP, and PDG's own
# tokens. Path mapping rewrites literal paths, not Houdini variables, so any other
# authored prefix points at the workstation. A variable counts only as a whole
# token: ``$HIPNAME`` and ``$HIP_OLD`` are other variables.
_PORTABLE_PREFIX: Pattern[str] = re.compile(
    r"\$(?:HIP|PDG_DIR|PDG_TEMP)(?!\w)|\$\{(?:HIP|PDG_DIR|PDG_TEMP)\}|__PDG_DIR__|__PDG_TEMP__"
)

# ``$NAME`` or ``${NAME}``: group 1 holds the braced name, group 2 the bare one.
_VARIABLE: Pattern[str] = re.compile(r"\$\{(\w+)\}|\$(\w+)")


@dataclass(frozen=True)
class SceneRead:
    """One file-reference parm that a step's tasks evaluate."""

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

    ``$NAME`` resolves only on the worker, where exported attributes become
    environment variables. PDG records the path on the workstation, where it
    resolves to nothing, so the recorded and written paths differ. The fix is
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
class DiscardedRoot:
    """A non-output read left unmatched because a producer's output root was discarded.

    Records the step, the discarded root, and the read, so validation can advise
    rather than refuse: the read may still be delivered.
    """

    step: str
    root: str
    read: SceneRead


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
    #: Non-output reads left unmatched because a producer's output root was discarded
    #: for sitting at or above the working directory. Empty when no discard lost a match.
    #: Each (step, root, read) appears once.
    discarded_roots: Tuple[DiscardedRoot, ...] = ()


def match_reads(
    snapshot: GraphSnapshot, reads: Sequence[SceneRead], working_dir: str
) -> ReadReport:
    """Attribute each read to the step whose outputs cover it.

    A plain read takes an exact output directory over the deepest enclosing root,
    with every test stopping at a path separator, and ignores a root at or above
    ``working_dir``, which would cover everything. A read that depends on a work
    item is matched by its static prefix instead, against every step's files.
    """
    producers: Tuple[_Producer, ...] = _output_roots(snapshot, working_dir)
    existing: Dict[str, Set[str]] = {step.name: set(step.dependencies) for step in snapshot.steps}

    matches: List[ReadMatch] = []
    edges: Dict[str, Set[str]] = {}
    # Non-output reads left unmatched only because a producer's output root was
    # discarded. A discard that changes no match is not recorded.
    discarded: List[DiscardedRoot] = []
    for read in reads:
        if read.is_output:
            # A write is not a read. Two File Copy nodes writing one sequence at
            # different frame offsets would otherwise depend on each other.
            continue
        if _depends_on_work_item(read.raw):
            # Evaluated outside any work item, its path means nothing, so it is
            # matched by its static prefix. A prefix that matches no step adds no
            # match at all, so no check judges the meaningless path.
            prefix_matches: List[ReadMatch] = _prefix_matches(
                read, producers, existing, working_dir
            )
            for match in prefix_matches:
                matches.append(match)
                if match.verdict in (ReadVerdict.COVERED, ReadVerdict.EDGE_ADDED):
                    edges.setdefault(read.step, set()).add(match.producer)
            # A prefix read the working-directory guard left unmatched, where a
            # producer writes under the prefix in the same subtree as the read, is
            # noted: the guard, not the scene, is why it has no producer. The
            # matching is left as it is.
            if not prefix_matches:
                discarded_prefix: str = _prefix_discarded_root(read, producers, working_dir)
                if discarded_prefix:
                    discarded.append(
                        DiscardedRoot(step=read.step, root=discarded_prefix, read=read)
                    )
            continue
        # A plain read has one producer, or none, and a verdict from how the
        # reading step relates to it.
        producer, candidates = _producer_of(read.path, producers, prefer=read.step)
        verdict: ReadVerdict = _verdict(read.step, producer, existing)
        # Covered matches count too, so an existing edge that carries data is kept.
        if verdict in (ReadVerdict.COVERED, ReadVerdict.EDGE_ADDED):
            edges.setdefault(read.step, set()).add(producer)
        # A read with no producer that a discarded root would have covered is noted:
        # the match is incomplete, not wrong, and left as it is.
        if verdict is ReadVerdict.EXTERNAL:
            for root in _discarded_roots_covering(read.path, producers, working_dir):
                discarded.append(DiscardedRoot(step=read.step, root=root, read=read))
        matches.append(
            ReadMatch(
                read=read,
                producer=producer,
                verdict=verdict,
                ambiguous_with=tuple(s for s in candidates if s != producer),
            )
        )

    # Each (step, root, read) is kept once, in first-seen order, so two producers with
    # one discarded root, or one read listed twice, give one entry and one advisory.
    unique_discarded: Tuple[DiscardedRoot, ...] = tuple(dict.fromkeys(discarded))
    return ReadReport(
        matches=tuple(matches),
        data_edges={step: frozenset(found) for step, found in sorted(edges.items())},
        unportable=_unportable(reads, working_dir),
        attribute_variables=_attribute_variables(reads, snapshot),
        discarded_roots=unique_discarded,
    )


def _discarded_roots_covering(path: str, producers: Sequence[_Producer], working: str) -> List[str]:
    """The discarded output roots whose discard is likely why ``path`` went unmatched.

    A discarded root sits at or above the working directory, so it covers nearly
    every read under the working directory. To report only the discards that plausibly
    lost a match, this keeps a producer's discarded root only when the read's directory
    and one of that producer's output directories share a common directory strictly below
    the working directory, so the read and the producer's outputs sit in the same subtree.
    An exact output directory is excluded, since it would have matched regardless.
    Two producers with the same root each return it; the caller removes duplicates.
    """
    directory: str = os.path.dirname(os.path.normpath(path)) if path else ""
    if not directory:
        return []
    working_norm: str = os.path.normpath(working) if working else ""
    found: List[str] = []
    for producer in producers:
        if not producer.discarded_root:
            continue
        # An exact output directory is matched directly, so its being covered by a
        # discarded root is not why the read went unmatched.
        if directory in producer.directories:
            continue
        # Keep the root only when the read and some output directory share a common
        # directory strictly below the working directory, the case where the discard
        # is really why the read went unmatched.
        if any(
            _shares_dir_below(directory, output_dir, working_norm)
            for output_dir in producer.directories
        ):
            found.append(producer.discarded_root)
    return found


def _shares_dir_below(first: str, second: str, working: str) -> bool:
    """Whether two directories share a common directory strictly below ``working``.

    ``working`` is already normalized. The common directory must be under ``working``
    and not ``working`` itself, so two reads in unrelated top-level subdirectories of
    the working directory do not count.
    """
    if not first or not second or not working:
        return False
    try:
        common: str = os.path.commonpath([first, second])
    except ValueError:  # absolute and relative mixed
        return False
    # Below the working directory and not the working directory itself.
    return _is_below(common, working) and common != working


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
    #: at or above the working directory.
    root: str
    #: The root ``root`` would have been, kept only when it was discarded for sitting
    #: at or above the working directory, so a read it leaves unmatched can be reported.
    discarded_root: str = ""


def _resolve(path: str, working_dir: str) -> str:
    """A declared path with PDG's working-directory token and variables expanded, normalized."""
    return os.path.normpath(os.path.expandvars(path.replace(PDG_DIR_TOKEN, working_dir)))


def _is_below(path: str, ancestor: str) -> bool:
    """Whether ``path`` is ``ancestor`` or under it. ``/a/bc`` is not under ``/a/b``."""
    if not path or not ancestor:
        return False
    if path == ancestor:
        return True
    return path.startswith(ancestor.rstrip(os.sep) + os.sep)


def _output_roots(snapshot: GraphSnapshot, working_dir: str) -> Tuple[_Producer, ...]:
    """Each step's output files and directories, and the root that identifies the step.

    The root lets a read evaluated at one wedge or frame match outputs declared
    for every wedge: ``geo/cache/wedge0..N`` share the root ``geo/cache``.
    """
    working: str = os.path.normpath(working_dir) if working_dir else ""
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
        root, discarded_root = _root(directories, working)
        producers.append(
            _Producer(step.name, frozenset(directories), frozenset(files), root, discarded_root)
        )
    return tuple(producers)


def _root(directories: Set[str], working: str) -> Tuple[str, str]:
    """The deepest directory holding all of ``directories``, and any root discarded.

    The first value is the usable root, or ``""`` when paths mix absolute and relative
    or when the root would hold the working directory. The second is the root discarded
    for sitting at or above the working directory, or ``""`` when nothing was discarded.
    """
    try:
        root: str = os.path.commonpath(sorted(directories))
    except ValueError:  # absolute and relative mixed
        return "", ""
    # A root at or above the working directory would cover every path, so it is
    # discarded, and kept separately so a read it leaves unmatched can be reported.
    if working and _is_below(working, root):
        return "", root
    return root, ""


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


def _normalize_prefix(prefix: str) -> Tuple[str, str]:
    """The prefix with its directory part normalized, and that directory.

    The last component is kept as typed, because it is where the expression
    starts, so it may be a partial file name such as ``cache.``; a trailing
    separator is kept too. Both are ``""`` when the prefix has no directory.
    """
    # Only the part before the last separator is a directory that can be normalized.
    head, tail = os.path.split(prefix)
    if not head:
        return "", ""
    directory: str = os.path.normpath(head)
    return os.path.join(directory, tail), directory


def _prefix_matches(
    read: SceneRead,
    producers: Sequence[_Producer],
    existing: Mapping[str, Set[str]],
    working_dir: str,
) -> List[ReadMatch]:
    """One match for each step with a declared output path that starts with the read's static prefix.

    The test is on the string, not separator-bounded, so a prefix may end inside a
    directory or a file name: ``$HIP/geo/$OS/wedge`@wedgeindex`/...`` has the prefix
    ``<hip>/geo/pyro_cache/wedge``, which covers ``wedge0`` to ``wedgeN``. A prefix
    whose directory is at or above the working directory would cover every step,
    so it matches nothing. When the reading step writes there itself, the read is
    its own.
    """
    # A prefix with no directory, or one that holds the working directory, would
    # match nothing useful or everything, so it matches nothing.
    prefix, directory = _normalize_prefix(read.static_prefix)
    working: str = os.path.normpath(working_dir) if working_dir else ""
    if not directory or (working and _is_below(working, directory)):
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


def _prefix_discarded_root(
    read: SceneRead, producers: Sequence[_Producer], working_dir: str
) -> str:
    """The prefix directory the ``working_dir`` guard discarded, when that discard lost a match.

    Returns the guarded directory only when some producer writes a file under the
    prefix and one of that producer's output directories shares a common directory
    strictly below the working directory with the directory of the read's evaluated
    path, the same subtree test a plain read gets. The guarded directory is at or
    above the working directory, so it cannot place the read in a subtree itself.
    Otherwise it returns ``""`` and nothing is reported, so a texture elsewhere in the
    project is not blamed on an unrelated step.
    """
    # Recompute the same prefix and guard _prefix_matches used, so the two agree on
    # when the guard fires.
    prefix, directory = _normalize_prefix(read.static_prefix)
    working: str = os.path.normpath(working_dir) if working_dir else ""
    if not directory or not (working and _is_below(working, directory)):
        return ""
    # The evaluated path is not used for matching, but its directory says which
    # subtree of the working directory the read sits in.
    read_dir: str = os.path.dirname(os.path.normpath(read.path)) if read.path else ""
    # Only a producer that writes under the prefix, in the read's subtree, lost a
    # match to the guard; otherwise the guard is not why the read has no producer.
    for producer in producers:
        if any(f.startswith(prefix) for f in producer.files) and any(
            _shares_dir_below(read_dir, output_dir, working) for output_dir in producer.directories
        ):
            return directory
    return ""


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
    """Write parms that spell one of the same step's exported attributes as ``$NAME``.

    Scoped per step: a variable is judged only against its own step's attributes.
    """
    # Each step's attribute names, keyed case-folded so ``$WEDGEINDEX`` finds
    # ``wedgeindex``, with PDG's own spelling as the value.
    by_step: Dict[str, Dict[str, str]] = {
        step.name: {name.casefold(): name for name in step.exported_attribute_names}
        for step in snapshot.steps
    }
    found: List[AttributeVariableUse] = []
    for read in reads:
        # Only writes are judged: a read that names a variable is not recorded.
        index: Optional[Dict[str, str]] = by_step.get(read.step) if read.is_output else None
        if not index:
            continue
        for braced, bare in _VARIABLE.findall(read.raw or ""):
            variable: str = braced or bare
            attribute: Optional[str] = index.get(variable.casefold())
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
