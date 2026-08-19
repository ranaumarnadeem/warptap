"""Pure-Python tests for SIB retargeting (implementation_plan.md §7 Stage 4, §3.3). Builds
:class:`~warptap.icl_model.SibNode` trees directly (not via ``sib_plan.build_sib_plan``, which
never produces a nested network in v1) so the genuinely-recursive ``open_path_to`` can be
exercised against both the flat case sib_plan.py actually emits and the nested case it's
designed to support later without rewriting."""

from __future__ import annotations

import pytest

from warptap.icl_model import InstrumentNode, PhysicalGraph, SibNode
from warptap.sib_retarget import SibRetargetError, open_path_to


def _instrument(name: str) -> InstrumentNode:
    return InstrumentNode(name=name, width=1, capture_value=0)


def test_directly_gated_instrument_returns_exactly_its_own_sib():
    graph = PhysicalGraph(
        chain=(
            SibNode("sib_a", _instrument("a")),
            SibNode("sib_b", _instrument("b")),
            SibNode("sib_c", _instrument("c")),
        )
    )
    assert open_path_to(graph, "b") == frozenset({"sib_b"})


def test_no_cross_talk_between_sibling_sibs():
    """The single most important property: opening the path to one instrument must
    never name a sibling SIB that gates a different, unrelated instrument."""
    graph = PhysicalGraph(
        chain=(
            SibNode("sib_a", _instrument("a")),
            SibNode("sib_b", _instrument("b")),
        )
    )
    for target, expected in (("a", "sib_a"), ("b", "sib_b")):
        path = open_path_to(graph, target)
        assert path == frozenset({expected})


def test_nested_sib_path_includes_every_ancestor():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    graph = PhysicalGraph(chain=(outer,))
    assert open_path_to(graph, "deep") == frozenset({"sib_outer", "sib_inner"})


def test_unrelated_top_level_sib_not_included_when_reaching_nested_instrument():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    sibling = SibNode("sib_sibling", _instrument("shallow"))
    graph = PhysicalGraph(chain=(outer, sibling))
    assert open_path_to(graph, "deep") == frozenset({"sib_outer", "sib_inner"})


def test_unknown_instrument_raises_named_error():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    with pytest.raises(SibRetargetError, match="ghost"):
        open_path_to(graph, "ghost")


def test_empty_network_raises():
    graph = PhysicalGraph(chain=())
    with pytest.raises(SibRetargetError):
        open_path_to(graph, "anything")
