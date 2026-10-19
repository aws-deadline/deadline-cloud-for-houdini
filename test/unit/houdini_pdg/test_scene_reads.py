# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for attributing a step's scene reads to the steps that write the files.

The module is pure, so every test builds its input value types directly: there is
no fake, mock, or patch. The property tests check against simple models written
from the matching rules, such as string prefixes over generated paths, not
against a copy of the code under test.
"""

from __future__ import annotations

# Standard library, hypothesis for the "for any reads" properties, and the public
# surface of ``graph`` and ``scene_reads`` only.
import os
from typing import Dict, FrozenSet, List, Sequence, Set, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deadline.houdini_pdg.graph import DeclaredFile, GraphSnapshot, StepSpec, WorkItemSpec
from deadline.houdini_pdg.scene_reads import (
    OUTPUT_PARMS,
    AttributeVariableUse,
    ReadMatch,
    ReadReport,
    ReadVerdict,
    SceneRead,
    match_reads,
)

WORK: str = "/proj"


def _step(
    name: str,
    outputs: Sequence[str] = (),
    dependencies: Sequence[str] = (),
    attributes: Tuple[str, ...] = (),
) -> StepSpec:
    """A step with one work item per declared output, or one item with none."""
    items: Tuple[WorkItemSpec, ...] = tuple(
        WorkItemSpec(
            name=f"{name}_{i}",
            task_index=i,
            pdg_index=i,
            item_id=i,
            node_name=name,
            declared_outputs=(DeclaredFile(path, "file/geo"),),
        )
        for i, path in enumerate(outputs)
    ) or (WorkItemSpec(name=f"{name}_0", task_index=0, pdg_index=0, item_id=0, node_name=name),)
    return StepSpec(
        name=name,
        dependencies=tuple(dependencies),
        items=items,
        exported_attribute_names=attributes,
    )


def _read(step: str, path: str, *, raw: str = "", parm: str = "file") -> SceneRead:
    """A literal read, flagged as a write when ``parm`` is an output parm."""
    return SceneRead(
        step=step,
        node_path=f"/obj/{step}/node",
        parm=parm,
        raw=raw or path,
        path=path,
        is_output=parm in OUTPUT_PARMS,
    )


def _pyro() -> GraphSnapshot:
    """Pyro Bonfire's shape: wedged pyro and sparks caches, and a karma step wired only to sparks."""
    return GraphSnapshot(
        steps=(
            _step(
                "pyro",
                [
                    f"__PDG_DIR__/geo/pyro_cache/wedge{w}/p.{f}.vdb"
                    for w in range(3)
                    for f in range(2)
                ],
            ),
            _step(
                "sparks", [f"__PDG_DIR__/geo/sparks_cache/wedge{w}/s.1.bgeo.sc" for w in range(3)]
            ),
            _step("karma", ["__PDG_DIR__/render/karma/k.1.exr"], dependencies=["sparks"]),
        )
    )


def _verdicts(matches: Sequence[ReadMatch]) -> List[Tuple[str, ReadVerdict, Tuple[str, ...]]]:
    """Each match as ``(producer, verdict, ambiguous_with)``, for comparing all of them at once."""
    return [(m.producer, m.verdict, m.ambiguous_with) for m in matches]


# -- matching -----------------------------------------------------------------


def test_karma_reading_pyro_becomes_a_data_edge() -> None:
    """A read under another step's root with no TOP wire adds a data edge, so the worker syncs the cache."""
    reads: List[SceneRead] = [_read("karma", f"{WORK}/geo/pyro_cache/wedge7/p.1.vdb")]
    report: ReadReport = match_reads(_pyro(), reads, WORK)
    # One match, from pyro, and it becomes karma's data edge.
    (match,) = report.matches
    assert (match.producer, match.verdict) == ("pyro", ReadVerdict.EDGE_ADDED)
    assert report.data_edges == {"karma": frozenset({"pyro"})}


def test_a_read_covered_by_an_existing_edge_is_still_a_data_edge() -> None:
    """A covered read still lands in ``data_edges``, so the existing edge that carries the file is kept."""
    reads: List[SceneRead] = [_read("karma", f"{WORK}/geo/sparks_cache/wedge0/s.1.bgeo.sc")]
    report: ReadReport = match_reads(_pyro(), reads, WORK)
    # The verdict is COVERED, and the edge is still requested.
    assert report.matches[0].verdict is ReadVerdict.COVERED
    assert report.data_edges == {"karma": frozenset({"sparks"})}


def test_a_step_reading_its_own_output_is_self() -> None:
    """A step's read of its own output is SELF and adds no edge, which would be a self-loop."""
    reads: List[SceneRead] = [_read("karma", f"{WORK}/render/karma/k.1.exr")]
    report: ReadReport = match_reads(_pyro(), reads, WORK)
    # SELF, and no edge at all.
    assert report.matches[0].verdict is ReadVerdict.SELF
    assert report.data_edges == {}


def test_a_read_no_step_produces_is_external() -> None:
    """A read outside every step's outputs, or with no path, is EXTERNAL with no producer."""
    reads: List[SceneRead] = [_read("karma", "/library/hdri/sky.exr"), _read("karma", "")]
    report: ReadReport = match_reads(_pyro(), reads, WORK)
    # Both reads are EXTERNAL, and an EXTERNAL match names no producer.
    assert [m.verdict for m in report.matches] == [ReadVerdict.EXTERNAL] * 2
    assert report.matches[0].producer == ""


def test_writes_are_never_matched() -> None:
    """A write parm is not a read: matching it would make writers into one directory depend on each other."""
    reads: List[SceneRead] = [
        _read("karma", f"{WORK}/geo/pyro_cache/wedge0/x.vdb", parm="sopoutput")
    ]
    report: ReadReport = match_reads(_pyro(), reads, WORK)
    # No match and no edge for the write.
    assert report.matches == ()
    assert report.data_edges == {}


def test_a_sibling_directory_with_a_shared_prefix_does_not_match() -> None:
    """``pyro_cache_old`` is not under ``pyro_cache``: a plain read's root test stops at a separator."""
    reads: List[SceneRead] = [_read("karma", f"{WORK}/geo/pyro_cache_old/wedge0/p.1.vdb")]
    report: ReadReport = match_reads(_pyro(), reads, WORK)
    # Matching by string prefix would wrongly give pyro.
    assert report.matches[0].verdict is ReadVerdict.EXTERNAL


def test_an_exact_directory_beats_a_deeper_root() -> None:
    """An exact output directory wins over a deeper root, and otherwise the deepest root wins."""
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(
            _step("outer", ["/out/a/x.1", "/out/a/b/c/x.2"]),
            # The bare name says nothing about where it lands, so it must not
            # erase inner's root by mixing a relative path into it.
            _step("inner", ["/out/a/b/y.1", "/out/a/b/z/y.2", "y.3"]),
            _step("reader"),
        )
    )
    exact: ReadMatch = match_reads(snapshot, [_read("reader", "/out/a/x.9")], WORK).matches[0]
    # ``/out/a`` is an exact directory of outer; ``/out/a/b/q`` is only under inner's deeper root.
    assert exact.producer == "outer"
    deepest: ReadMatch = match_reads(snapshot, [_read("reader", "/out/a/b/q/y.9")], WORK).matches[0]
    assert deepest.producer == "inner"


def test_a_root_at_or_above_working_dir_matches_only_exact_directories() -> None:
    """A root that covers the working directory would claim every read, so only exact directories match.

    ``wide``'s root is the working directory itself, and ``spanning``'s root is
    ``/``, above it. Keeping either root would make every read in the project
    depend on that step.
    """
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(
            _step("wide", [f"{WORK}/a/x.1", f"{WORK}/b/x.1"]),
            _step("spanning", [f"{WORK}/geo/x", "/other/y"]),
            _step("reader"),
        )
    )
    report: ReadReport = match_reads(
        snapshot,
        [
            _read("reader", f"{WORK}/c/x.1"),
            _read("reader", f"{WORK}/tex/t.png"),
            _read("reader", f"{WORK}/a/x.2"),
            _read("reader", f"{WORK}/geo/x.2"),
        ],
        WORK,
    )
    # ``c`` and ``tex`` are under the discarded roots only; ``a`` and ``geo`` are
    # output directories of their steps.
    assert _verdicts(report.matches) == [
        ("", ReadVerdict.EXTERNAL, ()),
        ("", ReadVerdict.EXTERNAL, ()),
        ("wide", ReadVerdict.EDGE_ADDED, ()),
        ("spanning", ReadVerdict.EDGE_ADDED, ()),
    ]


def test_a_shared_directory_is_attributed_to_the_reader_and_the_ambiguity_kept() -> None:
    """Two steps writing one directory: the reader's own output wins, and the sibling is recorded."""
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(
            _step("fetch_eroded", [f"{WORK}/out/eroded.exr"]),
            _step("fetch_uneroded", [f"{WORK}/out/uneroded.exr"]),
        )
    )
    reads: List[SceneRead] = [_read("fetch_uneroded", f"{WORK}/out/uneroded.exr")]
    # The reader is preferred, and the other writer of the directory is kept.
    (match,) = match_reads(snapshot, reads, WORK).matches
    assert (match.producer, match.verdict) == ("fetch_uneroded", ReadVerdict.SELF)
    assert match.ambiguous_with == ("fetch_eroded",)


def test_two_roots_at_the_same_depth_are_ambiguous() -> None:
    """Two roots at the same depth tie: the first step wins and the other is in ``ambiguous_with``."""
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(
            _step("a", ["/d/x/1/f", "/d/x/2/f"]),
            _step("b", ["/d/x/3/g", "/d/x/4/g"]),
            _step("reader"),
        )
    )
    # Both roots are ``/d/x``: the first wins and the tie is recorded.
    (match,) = match_reads(snapshot, [_read("reader", "/d/x/9/f")], WORK).matches
    assert match.producer == "a"
    assert match.ambiguous_with == ("b",)


def test_steps_with_mixed_relative_and_absolute_outputs_match_exactly() -> None:
    """Relative and absolute outputs have no common root, so only exact directories match, without raising."""
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(_step("mixed", ["rel/x.1", "/abs/x.1"]), _step("reader"))
    )
    report: ReadReport = match_reads(
        snapshot, [_read("reader", "/abs/x.2"), _read("reader", "/x/y")], WORK
    )
    assert [m.producer for m in report.matches] == ["mixed", ""]


_SEGMENT: st.SearchStrategy[str] = st.sampled_from(["a", "b", "ab", "c"])


@settings(max_examples=200)
@given(
    outputs=st.lists(st.lists(_SEGMENT, min_size=1, max_size=4), min_size=1, max_size=4),
    reads=st.lists(
        st.tuples(
            st.sampled_from(["s0", "s1", "s2"]),
            st.lists(_SEGMENT, min_size=1, max_size=4),
            st.booleans(),
        ),
        max_size=6,
    ),
    depends=st.booleans(),
)
def test_matching_rules_hold_for_any_reads(
    outputs: List[List[str]], reads: List[Tuple[str, List[str], bool]], depends: bool
) -> None:
    """For any literal reads: writes are skipped, own directories are SELF, verdicts follow dependencies.

    ``s1`` writes into a sibling whose name extends one of ``s0``'s directories, so
    a prefix test that is not separator-bounded would misattribute reads.
    """
    snapshot: GraphSnapshot = GraphSnapshot(
        steps=(
            _step("s0", ["/r/" + "/".join(o) + "/f" for o in outputs]),
            _step("s1", ["/r/" + "/".join(outputs[0]) + "x/f"]),
            _step("s2", dependencies=["s0"] if depends else []),
        )
    )
    scene: List[SceneRead] = [
        _read(step, "/r/" + "/".join(path) + "/f", parm="sopoutput" if write else "file")
        for step, path, write in reads
    ]
    report: ReadReport = match_reads(snapshot, scene, "/w")
    # Every read gets exactly one match, and no write gets one.
    assert len(report.matches) == sum(1 for r in scene if not r.is_output)
    deps: Dict[str, Set[str]] = {s.name: set(s.dependencies) for s in snapshot.steps}
    for match in report.matches:
        assert not match.read.is_output
        directory: str = os.path.dirname(match.read.path)
        own: Set[str] = {
            os.path.dirname(f.path)
            for i in snapshot.step(match.read.step).items  # type: ignore[union-attr]
            for f in i.declared_outputs
        }
        if directory in own:
            assert match.verdict is ReadVerdict.SELF
        if match.verdict is ReadVerdict.COVERED:
            assert match.producer in deps[match.read.step]
        if match.verdict is ReadVerdict.EDGE_ADDED:
            assert match.producer not in deps[match.read.step]
            assert match.producer != match.read.step
    # ``data_edges`` holds exactly the covered and edge-added producers.
    expected: Dict[str, Set[str]] = {}
    for match in report.matches:
        if match.verdict in (ReadVerdict.COVERED, ReadVerdict.EDGE_ADDED):
            expected.setdefault(match.read.step, set()).add(match.producer)
    assert report.data_edges == {k: frozenset(v) for k, v in expected.items()}


# -- unportable paths ---------------------------------------------------------


def _unportable(
    raw: str, path: str, *, parm: str = "file", working_dir: str = WORK
) -> Tuple[SceneRead, ...]:
    """The unportable reads ``match_reads`` reports for one read authored as ``raw``."""
    snapshot: GraphSnapshot = GraphSnapshot(steps=(_step("s"),))
    return match_reads(snapshot, [_read("s", path, raw=raw, parm=parm)], working_dir).unportable


def test_a_workstation_variable_under_the_working_dir_is_unportable() -> None:
    """``$DATA`` resolves on the workstation only, so a path it names inside the project breaks on a worker."""
    assert len(_unportable("$DATA/images/x.png", f"{WORK}/images/x.png")) == 1


def test_a_hardcoded_absolute_path_under_the_working_dir_is_unportable() -> None:
    """A literal workstation path inside the project does not move with the working directory."""
    assert len(_unportable(f"{WORK}/geo/x.bgeo.sc", f"{WORK}/geo/x.bgeo.sc")) == 1


def test_portable_prefixes_are_accepted() -> None:
    """Every prefix a worker resolves, braced or not, and with leading blanks, is portable."""
    for raw in (
        "$HIP/geo/x",
        "${HIP}/geo/x",
        "$HIP",
        "$PDG_DIR/x",
        "${PDG_DIR}/x",
        "$PDG_TEMP/x",
        "__PDG_DIR__/x",
        "__PDG_TEMP__/x",
        "  $HIP/x",
    ):
        assert _unportable(raw, f"{WORK}/geo/x") == (), raw


def test_hip_matches_only_as_a_whole_token() -> None:
    """``$HIPNAME`` and ``$HIP_OLD`` are other variables, so treating them as ``$HIP`` would hide a broken path."""
    for raw in ("$HIPNAME/x", "$HIP_OLD/x", "${HIPFILE}/x", "$HIPX"):
        assert len(_unportable(raw, f"{WORK}/x")) == 1, raw


def test_a_path_outside_the_working_dir_is_not_unportable() -> None:
    """Paths outside the project, including a sibling that shares its name as a prefix, are for attachments."""
    assert _unportable("/elsewhere/x", "/elsewhere/x") == ()
    assert _unportable(f"{WORK}x/y", f"{WORK}x/y") == ()


def test_no_working_dir_reports_no_unportable_reads() -> None:
    """Without a working directory there is nothing to be inside, so nothing is reported."""
    assert _unportable("/proj/x", "/proj/x", working_dir="") == ()


def test_a_prefix_read_is_still_judged_for_portability() -> None:
    """A read that depends on a work item is matched by its prefix, but its authored text is still judged.

    Wedged parms are the ones most often authored with a workstation path, so
    skipping them would hide exactly the paths that break on a worker.
    """
    read: SceneRead = SceneRead(
        step="s",
        node_path="/obj/s/node",
        parm="file",
        raw=f"{WORK}/geo/w`@wedgeindex`/x.bgeo.sc",
        path=f"{WORK}/geo/w/x.bgeo.sc",
        static_prefix=f"{WORK}/geo/w",
    )
    report: ReadReport = match_reads(GraphSnapshot(steps=(_step("s"),)), [read], WORK)
    assert report.unportable == (read,)


def test_writes_and_reads_are_both_checked_for_portability() -> None:
    """A write parm is judged too, since a write to a workstation path loses the output."""
    found: Tuple[SceneRead, ...] = _unportable("$JOB/out.exr", f"{WORK}/out.exr", parm="picture")
    assert len(found) == 1 and found[0].is_output


@given(
    prefix=st.sampled_from(
        ["$HIP", "${HIP}", "$PDG_DIR", "__PDG_DIR__", "$JOB", "/proj", "$HIPNAME"]
    ),
    inside=st.booleans(),
    working_dir=st.sampled_from(["", WORK]),
)
def test_unportable_iff_not_portably_authored_and_inside(
    prefix: str, inside: bool, working_dir: str
) -> None:
    """For any authored prefix: unportable exactly when not portable and inside a non-empty working dir."""
    path: str = f"{WORK}/geo/x" if inside else "/other/geo/x"
    found: Tuple[SceneRead, ...] = _unportable(prefix + "/geo/x", path, working_dir=working_dir)
    portable: bool = prefix in ("$HIP", "${HIP}", "$PDG_DIR", "__PDG_DIR__")
    # The model: only the four portable prefixes are portable.
    assert bool(found) == (bool(working_dir) and inside and not portable)


# -- attributes written as variables ------------------------------------------


EROSION: Tuple[str, ...] = ("TERRAIN_OFFSET", "wedgeindex", "wedgenum")


def _attribute_uses(
    reads: Sequence[SceneRead], *steps: StepSpec
) -> Tuple[AttributeVariableUse, ...]:
    """The attribute-as-variable uses ``match_reads`` reports for ``reads`` over ``steps``."""
    return match_reads(GraphSnapshot(steps=steps), reads, WORK).attribute_variables


def test_an_attribute_written_as_a_variable_is_reported() -> None:
    """``$TERRAIN_OFFSET`` in a write resolves to nothing on the workstation, so PDG records the wrong path."""
    reads: List[SceneRead] = [
        _read("s", "", raw="$HIP/out/eroded_$TERRAIN_OFFSET.exr", parm="output")
    ]
    # One use, quoting the variable, the attribute, and where it was written.
    (use,) = _attribute_uses(reads, _step("s", attributes=EROSION))
    assert (use.variable, use.attribute, use.parm) == ("TERRAIN_OFFSET", "TERRAIN_OFFSET", "output")
    assert use.raw == "$HIP/out/eroded_$TERRAIN_OFFSET.exr"
    assert use.node_path == "/obj/s/node"


def test_a_case_mismatch_is_reported_in_pdgs_casing() -> None:
    """``$WEDGEINDEX`` names ``wedgeindex``: the report quotes both, so the remedy names the real attribute."""
    reads: List[SceneRead] = [
        _read("s", "", raw="$HIP/render/$HIPNAME.$OS.$WEDGEINDEX.exr", parm="vm_picture")
    ]
    (use,) = _attribute_uses(reads, _step("s", attributes=EROSION))
    assert (use.variable, use.attribute) == ("WEDGEINDEX", "wedgeindex")


def test_the_braced_form_and_several_attributes_are_reported() -> None:
    """``${name}`` counts like ``$name``, and every attribute in one value is reported."""
    reads: List[SceneRead] = [
        _read("s", "", raw="$HIP/${wedgenum}/$TERRAIN_OFFSET.bgeo.sc", parm="sopoutput")
    ]
    uses: Tuple[AttributeVariableUse, ...] = _attribute_uses(reads, _step("s", attributes=EROSION))
    assert {u.attribute for u in uses} == {"wedgenum", "TERRAIN_OFFSET"}


def test_a_write_that_depends_on_a_work_item_is_still_judged_for_attribute_variables() -> None:
    """A write holding a backtick expression is still checked, since a wedged write is where ``$NAME`` goes wrong."""
    write: SceneRead = SceneRead(
        step="s",
        node_path="/obj/s/node",
        parm="picture",
        raw="$HIP/render/$WEDGEINDEX/`@frame`.exr",
        path=f"{WORK}/render//.exr",
        is_output=True,
        static_prefix=f"{WORK}/render/",
    )
    (use,) = _attribute_uses([write], _step("s", attributes=EROSION))
    assert (use.variable, use.attribute, use.raw) == (
        "WEDGEINDEX",
        "wedgeindex",
        "$HIP/render/$WEDGEINDEX/`@frame`.exr",
    )


def test_houdini_and_pdg_variables_are_not_reported() -> None:
    """Houdini's own variables are not attributes, so a write using them is not refused."""
    reads: List[SceneRead] = [
        _read("s", "", raw="$HIP/$HIPNAME.$OS.$F4.$PDG_INDEX.exr", parm="picture")
    ]
    assert _attribute_uses(reads, _step("s", attributes=EROSION)) == ()


def test_reads_are_never_reported() -> None:
    """Only writes are judged: a read naming an attribute as a variable is not refused."""
    reads: List[SceneRead] = [_read("s", "", raw="$HIP/geo/$TERRAIN_OFFSET.bgeo.sc", parm="file")]
    assert _attribute_uses(reads, _step("s", attributes=EROSION)) == ()


def test_attributes_are_scoped_to_the_step() -> None:
    """A variable is judged only against its own step's attributes, and a step not in the job has none."""
    reads: List[SceneRead] = [
        _read("has_not", "", raw="$HIP/geo/$TERRAIN_OFFSET.bgeo.sc", parm="sopoutput"),
        _read("has", "", raw="$HIP/geo/$TERRAIN_OFFSET.bgeo.sc", parm="sopoutput"),
        _read("ghost", "", raw="$HIP/geo/$TERRAIN_OFFSET.bgeo.sc", parm="sopoutput"),
    ]
    (use,) = _attribute_uses(
        reads, _step("has", attributes=("TERRAIN_OFFSET",)), _step("has_not", attributes=("x",))
    )
    assert use.step == "has"


@given(
    attribute=st.sampled_from(["wedgeindex", "TERRAIN_OFFSET", "curlblend"]),
    spelled=st.sampled_from(["lower", "upper", "same"]),
    braced=st.booleans(),
    is_output=st.booleans(),
    other_step_attributes=st.booleans(),
)
def test_offenders_are_exactly_writes_naming_their_own_attributes(
    attribute: str, spelled: str, braced: bool, is_output: bool, other_step_attributes: bool
) -> None:
    """For any spelling: a write naming its step's attribute is one offender, a read none, other steps irrelevant."""
    name: str = {"lower": attribute.lower(), "upper": attribute.upper(), "same": attribute}[spelled]
    raw: str = f"$HIP/geo/{'${' + name + '}' if braced else '$' + name}.bgeo.sc"
    reads: List[SceneRead] = [_read("s", "", raw=raw, parm="sopoutput" if is_output else "file")]
    steps: List[StepSpec] = [_step("s", attributes=(attribute,))]
    if other_step_attributes:
        steps.append(_step("t", attributes=("other",)))
    uses: Tuple[AttributeVariableUse, ...] = _attribute_uses(reads, *steps)
    # Writes give exactly one use with both spellings, and reads none.
    assert len(uses) == (1 if is_output else 0)
    if uses:
        assert uses[0].attribute == attribute and uses[0].variable == name


# -- reads that depend on a work item -----------------------------------------


def _wedged(step: str, prefix: str, *, raw: str = "") -> SceneRead:
    """A read authored with ``@wedgeindex``, whose evaluated path is meaningless, and its static prefix."""
    return SceneRead(
        step=step,
        node_path=f"/obj/{step}/cache",
        parm="file",
        raw=raw or "$HIP/geo/$OS/wedge`@wedgeindex`/$OS.w`@wedgeindex`.$F4.bgeo.sc",
        # Evaluated outside a work item: the attribute is empty.
        path=f"{WORK}/geo/pyro_cache/wedge/pyro_cache.w.0050.bgeo.sc",
        static_prefix=prefix,
    )


def _flat() -> GraphSnapshot:
    """A cache step that varies only the file name, ``cache/c.<wedge>.bgeo.sc``, and a render step."""
    return GraphSnapshot(
        steps=(
            _step("cache", [f"__PDG_DIR__/geo/cache/c.{w}.bgeo.sc" for w in range(3)]),
            _step("render", ["__PDG_DIR__/render/r.1.exr"]),
        )
    )


@pytest.mark.parametrize("raw", ["`@pdg_input`", "$HIP/`@wedgeindex`.png", "@pdg_output"])
def test_a_read_with_no_static_prefix_is_not_matched(raw: str) -> None:
    """A read that is only an expression has no prefix, and its evaluated path must not be judged.

    The path below sits in a step's output directory, so judging it would invent
    an edge, and outside one it would look like a missing asset.
    """
    read: SceneRead = SceneRead(
        step="montage",
        node_path="/obj/topnet1/montage",
        parm="inputfilepath",
        raw=raw,
        path=f"{WORK}/geo/pyro_cache/wedge0/p.1.vdb",
    )
    report: ReadReport = match_reads(_pyro(), [read], WORK)
    # No match at all, so no edge and no EXTERNAL verdict.
    assert report.matches == () and report.data_edges == {}


def test_a_wedged_read_matches_by_its_static_prefix() -> None:
    """Karma reads the pyro cache through ``@wedgeindex`` with no TOP wire, and still gets the edge."""
    report: ReadReport = match_reads(
        _pyro(), [_wedged("karma", f"{WORK}/geo/pyro_cache/wedge")], WORK
    )
    # One EDGE_ADDED match from pyro, and the edge.
    (match,) = report.matches
    assert (match.producer, match.verdict) == ("pyro", ReadVerdict.EDGE_ADDED)
    assert report.data_edges == {"karma": frozenset({"pyro"})}


def test_a_static_prefix_covered_by_an_existing_edge_is_covered() -> None:
    """A prefix read from a step already depended on is COVERED, and still protects the edge."""
    report: ReadReport = match_reads(_pyro(), [_wedged("karma", f"{WORK}/geo/sparks_cache/")], WORK)
    # COVERED from sparks, and the edge is still requested.
    (match,) = report.matches
    assert (match.producer, match.verdict) == ("sparks", ReadVerdict.COVERED)
    assert report.data_edges == {"karma": frozenset({"sparks"})}


def test_a_step_reading_its_own_wedged_output_is_self() -> None:
    """A prefix the reading step writes under is its own output, with no edge."""
    report: ReadReport = match_reads(
        _pyro(), [_wedged("pyro", f"{WORK}/geo/pyro_cache/wedge")], WORK
    )
    # SELF, and no edge.
    (match,) = report.matches
    assert (match.producer, match.verdict) == ("pyro", ReadVerdict.SELF)
    assert report.data_edges == {}


def test_a_static_prefix_matching_several_steps_adds_an_edge_to_each() -> None:
    """A prefix over two steps' outputs gives one match per step, each naming the other, and both edges."""
    report: ReadReport = match_reads(_pyro(), [_wedged("karma", f"{WORK}/geo/")], WORK)
    assert _verdicts(report.matches) == [
        ("pyro", ReadVerdict.EDGE_ADDED, ("sparks",)),
        ("sparks", ReadVerdict.COVERED, ("pyro",)),
    ]
    assert report.data_edges == {"karma": frozenset({"pyro", "sparks"})}


def test_a_self_prefix_match_names_no_other_step() -> None:
    """When the reader writes under the prefix, the one SELF match has an empty ``ambiguous_with`` and no edge.

    ``sparks`` also writes under the prefix, so a match per producer, or a SELF
    that kept the other producers, would differ.
    """
    report: ReadReport = match_reads(_pyro(), [_wedged("pyro", f"{WORK}/geo/")], WORK)
    assert _verdicts(report.matches) == [("pyro", ReadVerdict.SELF, ())]
    assert report.data_edges == {}


@pytest.mark.parametrize("prefix", ["", f"{WORK}/", WORK, "/", f"{WORK}/geo/other", "geo/"])
def test_a_static_prefix_at_or_above_the_working_dir_or_elsewhere_matches_nothing(
    prefix: str,
) -> None:
    """A prefix that is empty, covers the working dir, or matches no output adds no match, not even EXTERNAL.

    An EXTERNAL match would get its meaningless evaluated path judged as a
    missing file.
    """
    report: ReadReport = match_reads(_pyro(), [_wedged("karma", prefix)], WORK)
    assert report.matches == () and report.data_edges == {}


@pytest.mark.parametrize(
    "prefix",
    [
        f"{WORK}/geo/cache/c.",  # ends inside the file name
        f"{WORK}/geo/cache/",  # ends at a separator
        f"{WORK}/geo/ca",  # ends inside a directory name
        f"{WORK}//geo/./cache/c.",  # not normalized
        f"{WORK}/tmp/../geo/cache/c",  # climbs out of a directory
    ],
)
def test_a_prefix_matches_the_files_a_step_writes(prefix: str) -> None:
    """A prefix is compared with each declared file, so it may stop anywhere in the path, normalized first.

    A step that varies only the file name, ``cache.`@wedgeindex`.bgeo.sc``, would
    otherwise lose its data edge, because no output directory carries the file
    name or a trailing separator.
    """
    report: ReadReport = match_reads(_flat(), [_wedged("render", prefix)], WORK)
    assert _verdicts(report.matches) == [("cache", ReadVerdict.EDGE_ADDED, ())]
    assert report.data_edges == {"render": frozenset({"cache"})}


def test_a_prefix_must_match_the_file_name_it_ends_in() -> None:
    """``cache/d.`` names no file the cache step writes, so matching by directory alone would invent an edge."""
    report: ReadReport = match_reads(_flat(), [_wedged("render", f"{WORK}/geo/cache/d.")], WORK)
    assert report.matches == () and report.data_edges == {}


def test_any_at_sign_makes_a_read_a_prefix_read() -> None:
    """A literal ``@`` is treated as an attribute, so the evaluated path is never judged on its own.

    The evaluated path sits in ``sparks``'s output directory, so a plain match
    would be COVERED. With no static prefix there must be no match, and with one
    it matches by the prefix.
    """
    raw: str = "$HIP/geo/sparks_cache/wedge0/s@2x.bgeo.sc"
    path: str = f"{WORK}/geo/sparks_cache/wedge0/s@2x.bgeo.sc"
    unprefixed: SceneRead = SceneRead("karma", "/obj/karma/tex", "file", raw, path)
    # With no prefix there is no match; with one, sparks matches by the prefix.
    assert match_reads(_pyro(), [unprefixed], WORK).matches == ()
    prefixed: SceneRead = SceneRead(
        "karma", "/obj/karma/tex", "file", raw, path, static_prefix=f"{WORK}/geo/sparks_cache/w"
    )
    assert _verdicts(match_reads(_pyro(), [prefixed], WORK).matches) == [
        ("sparks", ReadVerdict.COVERED, ())
    ]


def test_a_prefix_write_is_never_matched() -> None:
    """A wedged write parm is a write, so its prefix must not make the writer depend on another step."""
    write: SceneRead = SceneRead(
        "karma",
        "/obj/karma/rop",
        "sopoutput",
        "$HIP/geo/pyro_cache/wedge`@wedgeindex`/x.vdb",
        "",
        is_output=True,
        static_prefix=f"{WORK}/geo/pyro_cache/wedge",
    )
    report: ReadReport = match_reads(_pyro(), [write], WORK)
    # No match and no edge for the write.
    assert report.matches == () and report.data_edges == {}


_PREFIX_SEGMENT: st.SearchStrategy[str] = st.sampled_from(["a", "b", "ab"])
_PREFIX_STEPS: Tuple[str, ...] = ("s0", "s1", "s2")
# The working directory and every directory at or above it.
_PREFIX_WORK: str = "/r/a"
_AT_OR_ABOVE_WORK: FrozenSet[str] = frozenset({"/", "/r", "/r/a"})


def _prefix_model(
    files: Dict[str, List[str]], prefix: str, reader: str, dependencies: Sequence[str]
) -> List[Tuple[str, ReadVerdict, Tuple[str, ...]]]:
    """The matches the rules give, as ``(producer, verdict, ambiguous_with)``, by string slicing.

    The prefix's directory is everything before its last separator. A directory at
    or above the working directory gives no match, a reader among the writers one
    SELF, and otherwise each writer one match naming the others.
    """
    # A prefix whose directory holds the working directory matches nothing.
    directory: str = prefix[: prefix.rfind("/")] or "/"
    if directory in _AT_OR_ABOVE_WORK:
        return []
    # The writers are the steps with a declared file that begins with the prefix.
    found: List[str] = [
        name for name in _PREFIX_STEPS if any(path.startswith(prefix) for path in files[name])
    ]
    if reader in found:
        return [(reader, ReadVerdict.SELF, ())]
    return [
        (
            name,
            ReadVerdict.COVERED if name in dependencies else ReadVerdict.EDGE_ADDED,
            tuple(other for other in found if other != name),
        )
        for name in found
    ]


@settings(max_examples=300)
@given(
    outputs=st.lists(
        st.tuples(
            st.sampled_from(_PREFIX_STEPS),
            st.lists(_PREFIX_SEGMENT, max_size=3),
            st.sampled_from(["f.1", "f.2", "g.1"]),
        ),
        min_size=1,
        max_size=6,
    ),
    reader=st.sampled_from(_PREFIX_STEPS + ("s3",)),
    source=st.integers(0, 6),
    cut=st.integers(1, 20),
    depends=st.sets(st.sampled_from(_PREFIX_STEPS)),
)
def test_prefix_matching_rules_hold_for_any_prefix(
    outputs: List[Tuple[str, List[str], str]],
    reader: str,
    source: int,
    cut: int,
    depends: Set[str],
) -> None:
    """For any static prefix: matches are exactly the steps with a file starting with it, unless guarded or SELF.

    The model is plain string slicing over the generated paths. The prefix is a
    cut of a declared path, or of a path no step writes, at any character, so it
    ends inside directory and file names and at separators alike.
    """
    # Each step's declared files, as absolute paths with no tokens.
    files: Dict[str, List[str]] = {name: [] for name in _PREFIX_STEPS}
    for step, segments, leaf in outputs:
        files[step].append("/r/" + "/".join(segments + [leaf]))
    # The reader is one of the writers or a fourth step with no outputs, and only
    # it has dependencies.
    dependencies: List[str] = sorted(depends - {reader})
    steps: List[StepSpec] = [
        _step(name, files[name], dependencies=dependencies if name == reader else ())
        for name in _PREFIX_STEPS
    ]
    if reader == "s3":
        steps.append(_step("s3", dependencies=dependencies))
    snapshot: GraphSnapshot = GraphSnapshot(steps=tuple(steps))
    # The prefix is cut from a declared path or from one no step writes.
    candidates: List[str] = [path for name in _PREFIX_STEPS for path in files[name]]
    candidates.append("/r/b/zz/f.1")
    prefix: str = candidates[source % len(candidates)][:cut]

    report: ReadReport = match_reads(snapshot, [_wedged(reader, prefix)], _PREFIX_WORK)

    expected: List[Tuple[str, ReadVerdict, Tuple[str, ...]]] = _prefix_model(
        files, prefix, reader, dependencies
    )
    # The matches follow the model, and every non-SELF match is an edge.
    assert _verdicts(report.matches) == expected
    edged: Set[str] = {name for name, verdict, _ in expected if verdict is not ReadVerdict.SELF}
    assert report.data_edges == ({reader: frozenset(edged)} if edged else {})
