# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Attribute the files a step's scene reads to the steps that write them.

A Houdini task opens files named by parms inside the scene, and the TOP graph
does not know about them. Matching each read to the step whose outputs cover it
yields the data edges the TOP wires miss.

Reads driven by ``@attr`` or Python expressions are invisible. The matching
favors precision over recall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Mapping, Sequence, Tuple

from .graph import GraphSnapshot

#: File-reference parms that are not data reads: debug artifacts, labels, and
#: pre/post scripts.
IGNORE_PARMS = frozenset(
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
IGNORE_VALUE_PREFIXES = ("opdef:", "oplib:", "temp:", "op:")

#: Parms that name where a node writes. Matching a write as a read would invent a
#: dependency, so these are never matched; the path checks still examine them.
OUTPUT_PARMS = frozenset(
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

# Prefixes that resolve on a worker. Path mapping rewrites literal paths, not
# Houdini variables, so any other authored prefix points at the workstation.
_PORTABLE_PREFIXES = ("$HIP", "$PDG_DIR", "$PDG_TEMP", "__PDG_DIR__", "__PDG_TEMP__")


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
    #: Other steps whose outputs also cover the path. Non-empty only when two
    #: steps write into one directory.
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


def match_reads(
    snapshot: GraphSnapshot, reads: Sequence[SceneRead], working_dir: str
) -> ReadReport:
    """Attribute each read to the step whose output directory covers it.

    An exact output directory wins over the deepest enclosing one, and every
    prefix test stops at a path separator. A step output root at or above
    ``working_dir`` is ignored, because it would cover everything.
    """
    raise NotImplementedError
