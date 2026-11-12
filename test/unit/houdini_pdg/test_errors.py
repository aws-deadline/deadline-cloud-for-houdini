# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The text an artist reads for a refused graph."""

from __future__ import annotations


from deadline.houdini_pdg.errors import GraphRefusedError
from deadline.houdini_pdg.validation import Refusal, RefusalScenario, Verdict


def _refusal(subject: str, detail: str = "is wrong.", remedy: str = "Fix it.") -> Refusal:
    """A refusal with the given text."""
    return Refusal(
        scenario=RefusalScenario.ROP_BATCH, subject=subject, detail=detail, remedy=remedy
    )


def test_each_refusal_is_on_its_own_line_after_a_header() -> None:
    """Each refusal is a "- " bullet on its own line, so the artist can tell where one ends."""
    verdict: Verdict = Verdict(refusals=(_refusal("a", "x.", "y."), _refusal("b", "p.", "q.")))
    assert str(GraphRefusedError(verdict)).split("\n") == [
        "The graph was refused (2 refusal(s)):",
        "- a: x. y.",
        "- b: p. q.",
    ]
