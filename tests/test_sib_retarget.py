"""Pure-Python tests for SIB retargeting (implementation_plan.md §7 Stage 4, §3.3). Builds
:class:`~warptap.icl_model.SibNode` trees directly (not via ``sib_plan.build_sib_plan``, which
also supports nested networks now via ``HierarchySpec``) so ``open_path_to``/
``stage_open_sequence`` can be exercised against hand-built shapes independent of the
planning API's own tests."""

from __future__ import annotations

import pytest

from warptap.icl_model import InstrumentNode, PhysicalGraph, ScanArm, ScanMuxNode, SibNode
from warptap.sib_retarget import (
    PathStep,
    SibRetargetError,
    open_path_to,
    path_to_sib_name,
    stage_open_sequence,
)


def _instrument(name: str) -> InstrumentNode:
    return InstrumentNode(name=name, width=1, capture_value=0)


def _leaf_arm(value: int, instrument_name: str) -> ScanArm:
    return ScanArm(values=(value,), instrument=_instrument(instrument_name))


def test_directly_gated_instrument_returns_exactly_its_own_sib():
    graph = PhysicalGraph(
        chain=(
            SibNode("sib_a", _instrument("a")),
            SibNode("sib_b", _instrument("b")),
            SibNode("sib_c", _instrument("c")),
        )
    )
    assert open_path_to(graph, "b") == (PathStep("sib_b", 1),)


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
        assert path == (PathStep(expected, 1),)


def test_nested_sib_path_includes_every_ancestor_root_to_leaf():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    graph = PhysicalGraph(chain=(outer,))
    assert open_path_to(graph, "deep") == (PathStep("sib_outer", 1), PathStep("sib_inner", 1))


def test_unrelated_top_level_sib_not_included_when_reaching_nested_instrument():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    sibling = SibNode("sib_sibling", _instrument("shallow"))
    graph = PhysicalGraph(chain=(outer, sibling))
    assert open_path_to(graph, "deep") == (PathStep("sib_outer", 1), PathStep("sib_inner", 1))


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
    assert path_to_sib_name(graph, "sib_outer") == (PathStep("sib_outer", 1),)
    assert path_to_sib_name(graph, "sib_inner") == (PathStep("sib_outer", 1), PathStep("sib_inner", 1))


def test_path_to_sib_name_unknown_name_raises():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    with pytest.raises(SibRetargetError, match="ghost"):
        path_to_sib_name(graph, "ghost")


def test_stage_open_sequence_empty_target_is_a_single_all_closed_round():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    assert stage_open_sequence(graph, frozenset()) == [{}]


def test_stage_open_sequence_top_level_target_is_a_single_round():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    assert stage_open_sequence(graph, frozenset({"sib_a"})) == [{"sib_a": 1}]


def test_stage_open_sequence_nested_target_needs_one_round_per_depth():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    graph = PhysicalGraph(chain=(outer,))
    assert stage_open_sequence(graph, frozenset({"sib_inner"})) == [
        {"sib_outer": 1},
        {"sib_outer": 1, "sib_inner": 1},
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
        {"sib_outer": 1, "sib_sibling": 1},
        {"sib_outer": 1, "sib_inner": 1, "sib_sibling": 1},
    ]


def _mux_graph() -> PhysicalGraph:
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(_leaf_arm(1, "arm1_instr"), _leaf_arm(2, "arm2_instr")),
    )
    return PhysicalGraph(chain=(mux,))


def test_open_path_to_an_instrument_directly_gated_by_a_mux_arm():
    graph = _mux_graph()
    assert open_path_to(graph, "arm1_instr") == (PathStep("mux_a", 1),)
    assert open_path_to(graph, "arm2_instr") == (PathStep("mux_a", 2),)


def test_open_path_to_no_cross_talk_between_mux_arms():
    """Targeting arm2's instrument must resolve mux_a's own value to 2, not arm1's value --
    the direct mux analog of test_no_cross_talk_between_sibling_sibs."""
    graph = _mux_graph()
    assert open_path_to(graph, "arm1_instr")[0].value == 1
    assert open_path_to(graph, "arm2_instr")[0].value == 2


def test_open_path_to_an_instrument_nested_inside_a_mux_arm():
    inner = SibNode("sib_inner", _instrument("deep"))
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(ScanArm(values=(0,), nested=(inner,)), _leaf_arm(1, "b")),
    )
    graph = PhysicalGraph(chain=(mux,))
    assert open_path_to(graph, "deep") == (PathStep("mux_a", 0), PathStep("sib_inner", 1))


def test_open_path_to_unknown_instrument_inside_a_mux_raises():
    graph = _mux_graph()
    with pytest.raises(SibRetargetError, match="ghost"):
        open_path_to(graph, "ghost")


def test_path_to_sib_name_targets_a_mux_directly_with_a_chosen_value():
    """A mux has no single canonical "open" value the way a SIB's po does -- target_value is
    the only way to say which arm when there's nothing nested inside it to search for."""
    graph = _mux_graph()
    assert path_to_sib_name(graph, "mux_a", target_value=2) == (PathStep("mux_a", 2),)


def test_path_to_sib_name_default_target_value_is_one():
    """Matches every pre-ScanMux call site's own implicit assumption (a bare name means
    value 1) without requiring every existing caller to start passing target_value=1."""
    graph = _mux_graph()
    assert path_to_sib_name(graph, "mux_a") == (PathStep("mux_a", 1),)


def test_path_to_sib_name_resolves_a_sib_nested_inside_a_mux_arm():
    """The mux's own ancestor value (0, arm0's) is resolved by trying each arm in turn while
    searching for sib_inner deeper inside it -- target_value is irrelevant here since the
    resolved target is a SIB, not the mux itself."""
    inner = SibNode("sib_inner", _instrument("deep"))
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(ScanArm(values=(0,), nested=(inner,)), _leaf_arm(1, "b")),
    )
    graph = PhysicalGraph(chain=(mux,))
    assert path_to_sib_name(graph, "sib_inner") == (PathStep("mux_a", 0), PathStep("sib_inner", 1))


def test_stage_open_sequence_targets_a_mux_arm_directly():
    graph = _mux_graph()
    assert stage_open_sequence(graph, {"mux_a": 2}) == [{"mux_a": 2}]


def test_stage_open_sequence_nested_mux_target_needs_one_round_per_depth():
    inner = SibNode("sib_inner", _instrument("deep"))
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(ScanArm(values=(0,), nested=(inner,)), _leaf_arm(1, "b")),
    )
    graph = PhysicalGraph(chain=(mux,))
    assert stage_open_sequence(graph, {"sib_inner": 1}) == [
        {"mux_a": 0},
        {"mux_a": 0, "sib_inner": 1},
    ]


# --- currently_open: reusing an already-open ancestor prefix (retargeting shift-length
# optimization plan) -----------------------------------------------------------------------


def _nested_pair_graph() -> PhysicalGraph:
    """A > B > {sib_x, sib_y} -- the plan's own motivating shared-ancestor example."""
    inner = SibNode("B", instrument=None, nested=(
        SibNode("sib_x", _instrument("x")),
        SibNode("sib_y", _instrument("y")),
    ))
    outer = SibNode("A", instrument=None, nested=(inner,))
    return PhysicalGraph(chain=(outer,))


def test_stage_open_sequence_default_currently_open_is_byte_identical_to_before():
    """Regression proof: every pre-existing caller (which passes nothing) must see exactly
    today's output, unchanged -- an empty currently_open can never match anything, so
    first_new_depth always resolves to 1."""
    graph = _nested_pair_graph()
    target = {"A": 1, "B": 1, "sib_x": 1}
    assert stage_open_sequence(graph, target) == stage_open_sequence(
        graph, target, currently_open=frozenset()
    )
    assert stage_open_sequence(graph, target) == [
        {"A": 1}, {"A": 1, "B": 1}, {"A": 1, "B": 1, "sib_x": 1},
    ]


def test_stage_open_sequence_fully_satisfied_target_needs_zero_rounds():
    """Retargeting to the exact same already-open target needs no rounds at all -- this is
    also what fixes the WRITE-instrument clobber footgun (pdl_interpreter.iApply's own
    docstring): no redundant Update-DR means nothing to zero-fill and commit."""
    graph = _nested_pair_graph()
    target = {"A": 1, "B": 1, "sib_x": 1}
    assert stage_open_sequence(graph, target, currently_open=target) == []


def test_stage_open_sequence_reuses_a_shared_ancestor_prefix():
    """The plan's own headline case: y after x, sharing A and B -- only sib_y's own round is
    needed, not a full 3-round cold-start sequence."""
    graph = _nested_pair_graph()
    prior = {"A": 1, "B": 1, "sib_x": 1}
    target_y = {"A": 1, "B": 1, "sib_y": 1}
    assert stage_open_sequence(graph, target_y, currently_open=prior) == [
        {"A": 1, "B": 1, "sib_y": 1},
    ]


def test_stage_open_sequence_multi_branch_reuse_only_trims_the_satisfied_branch():
    """Two independent branches (P, Q), each with their own depth-2 child -- P is already
    fully open, Q is brand new. Stress-tests first_new_depth as a min() over EVERY
    mismatching entry, not just one path: Q's own depth-1 entry must force a full 2-round
    sequence for Q, even though P's entries at those same two depths already match."""
    p_branch = SibNode("P", instrument=None, nested=(SibNode("P1", _instrument("p1")),))
    q_branch = SibNode("Q", instrument=None, nested=(SibNode("Q1", _instrument("q1")),))
    graph = PhysicalGraph(chain=(p_branch, q_branch))
    prior = {"P": 1, "P1": 1}
    target = {"P": 1, "P1": 1, "Q": 1, "Q1": 1}
    assert stage_open_sequence(graph, target, currently_open=prior) == [
        {"P": 1, "Q": 1},
        {"P": 1, "P1": 1, "Q": 1, "Q1": 1},
    ]


def test_stage_open_sequence_mux_ancestor_matching_by_name_but_not_value_is_not_trimmed():
    """A mux ancestor whose NAME already appears in currently_open but with a different
    VALUE (switching arms) must still force a full round at that depth -- name-only matching
    would incorrectly treat an arm switch as already-satisfied."""
    graph = _mux_graph()
    assert stage_open_sequence(graph, {"mux_a": 2}, currently_open={"mux_a": 1}) == [
        {"mux_a": 2},
    ]


def test_stage_open_sequence_leftover_state_when_fully_satisfied_target_names_only_an_outer_node():
    """Documents a real, deliberate consequence (see the function's own docstring): a
    cold-start call's last round always equals target_open exactly, explicitly closing
    anything not named. When currently_open already fully satisfies the target and stage_
    open_sequence returns [], nothing is explicitly addressed either way -- so sib_inner
    here, though not named by this call's own target_open, is never explicitly closed. This
    is intentional (no caller today exercises this combination), not a bug -- locked in here
    so a future change can't silently alter it."""
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    graph = PhysicalGraph(chain=(outer,))
    prior = {"sib_outer": 1, "sib_inner": 1}
    assert stage_open_sequence(graph, {"sib_outer": 1}, currently_open=prior) == []
