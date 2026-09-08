"""Pure-Python tests for SIB retargeting (implementation_plan.md §7 Stage 4, §3.3). Builds
:class:`~warptap.icl_model.SibNode` trees directly (not via ``sib_plan.build_sib_plan``, which
also supports nested networks now via ``HierarchySpec``) so ``open_path_to``/
``stage_open_sequence`` can be exercised against hand-built shapes independent of the
planning API's own tests."""

from __future__ import annotations

import pytest

from warptap.icl_model import InstrumentNode, PhysicalGraph, SibNode
from warptap.sib_retarget import (
    SibRetargetError,
    open_path_to,
    path_to_sib_name,
    stage_open_sequence,
)


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
    assert open_path_to(graph, "b") == ("sib_b",)


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
        assert path == (expected,)


def test_nested_sib_path_includes_every_ancestor_root_to_leaf():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    graph = PhysicalGraph(chain=(outer,))
    assert open_path_to(graph, "deep") == ("sib_outer", "sib_inner")


def test_unrelated_top_level_sib_not_included_when_reaching_nested_instrument():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    sibling = SibNode("sib_sibling", _instrument("shallow"))
    graph = PhysicalGraph(chain=(outer, sibling))
    assert open_path_to(graph, "deep") == ("sib_outer", "sib_inner")


def test_unknown_instrument_raises_named_error():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    with pytest.raises(SibRetargetError, match="ghost"):
        open_path_to(graph, "ghost")


def test_empty_network_raises():
    graph = PhysicalGraph(chain=())
    with pytest.raises(SibRetargetError):
        open_path_to(graph, "anything")


def test_path_to_sib_name_resolves_a_hierarchy_node_directly():
    """Unlike open_path_to, this can target a hierarchy SIB itself -- no instrument name
    needed -- e.g. to check a whole nested subtree's reachability, not one leaf inside it."""
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    graph = PhysicalGraph(chain=(outer,))
    assert path_to_sib_name(graph, "sib_outer") == ("sib_outer",)
    assert path_to_sib_name(graph, "sib_inner") == ("sib_outer", "sib_inner")


def test_path_to_sib_name_unknown_name_raises():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    with pytest.raises(SibRetargetError, match="ghost"):
        path_to_sib_name(graph, "ghost")


def test_stage_open_sequence_empty_target_is_a_single_all_closed_round():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    assert stage_open_sequence(graph, frozenset()) == [frozenset()]


def test_stage_open_sequence_top_level_target_is_a_single_round():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    assert stage_open_sequence(graph, frozenset({"sib_a"})) == [frozenset({"sib_a"})]


def test_stage_open_sequence_nested_target_needs_one_round_per_depth():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    graph = PhysicalGraph(chain=(outer,))
    assert stage_open_sequence(graph, frozenset({"sib_inner"})) == [
        frozenset({"sib_outer"}),
        frozenset({"sib_outer", "sib_inner"}),
    ]


def test_stage_open_sequence_multi_branch_target_merges_independent_paths():
    """A top-level leaf (depth 1) and an unrelated nested leaf (depth 2) targeted together --
    round 1 opens everything reachable at depth 1 (both the leaf and the nested target's
    outer ancestor); round 2 additionally opens the nested target itself."""
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    sibling = SibNode("sib_sibling", _instrument("shallow"))
    graph = PhysicalGraph(chain=(outer, sibling))
    rounds = stage_open_sequence(graph, frozenset({"sib_inner", "sib_sibling"}))
    assert rounds == [
        frozenset({"sib_outer", "sib_sibling"}),
        frozenset({"sib_outer", "sib_inner", "sib_sibling"}),
    ]
