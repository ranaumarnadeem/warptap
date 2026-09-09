"""Pure-Python tests for static SIB bit-layout arithmetic (implementation_plan.md §7 Stage 5,
§3.3). No Yosys/RTL involved -- synthetic PhysicalGraph objects only, mirroring
test_sib_retarget.py's style.
"""

from __future__ import annotations

import pytest

from warptap.icl_model import InstrumentNode, PhysicalGraph, ScanArm, ScanMuxNode, SibNode
from warptap.sib_layout import compose_bits, layout_bit_length


def _instrument(name: str, width: int, capture_value: int = 0) -> InstrumentNode:
    return InstrumentNode(name=name, width=width, capture_value=capture_value)


def _leaf_arm(value: int, name: str, width: int) -> ScanArm:
    return ScanArm(values=(value,), instrument=_instrument(name, width))


def test_all_closed_length_is_one_bit_per_sib():
    graph = PhysicalGraph(chain=(
        SibNode("sib_a", _instrument("a", width=3)),
        SibNode("sib_b", _instrument("b", width=5)),
        SibNode("sib_c", _instrument("c", width=1)),
    ))
    assert layout_bit_length(graph, frozenset()) == 3


def test_one_open_sib_adds_its_instrument_width():
    graph = PhysicalGraph(chain=(
        SibNode("sib_a", _instrument("a", width=3)),
        SibNode("sib_b", _instrument("b", width=5)),
    ))
    assert layout_bit_length(graph, frozenset({"sib_b"})) == 1 + (5 + 1)  # sib_a closed, sib_b open


def test_every_open_sib_contributes_independently():
    graph = PhysicalGraph(chain=(
        SibNode("sib_a", _instrument("a", width=3)),
        SibNode("sib_b", _instrument("b", width=5)),
    ))
    assert layout_bit_length(graph, frozenset({"sib_a", "sib_b"})) == (3 + 1) + (5 + 1)


def test_open_sib_name_not_in_graph_raises_no_error_and_has_no_effect():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a", width=3)),))
    assert layout_bit_length(graph, frozenset({"nonexistent"})) == 1


def test_layout_bit_length_matches_dtm2012_worked_example():
    """Zadegan, Ingelsson, Larsson, Carlsson, 'Reusing and Retargeting On-Chip Instrument
    Access Procedures in IEEE P1687', DTM 2012 -- SIB1 gates a 4-bit TDR1, SIB2 is a plain
    sibling. Phase 1 (nothing open yet) = 2 bits (SIB1 + SIB2, both closed). Phase 2 (SIB1
    open) = 6 bits (4-bit TDR1 + SIB1's own select bit + SIB2's still-closed sibling bit) --
    the single most concretely externally-verified number found in this stage's research."""
    graph = PhysicalGraph(chain=(
        SibNode("SIB1", _instrument("TDR1", width=4)),
        SibNode("SIB2", _instrument("Other", width=1)),
    ))
    assert layout_bit_length(graph, frozenset()) == 2
    assert layout_bit_length(graph, frozenset({"SIB1"})) == 6


def test_compose_bits_matches_dtm2012_worked_example_structure():
    graph = PhysicalGraph(chain=(
        SibNode("SIB1", _instrument("TDR1", width=4)),
        SibNode("SIB2", _instrument("Other", width=1)),
    ))
    # Phase 1: nothing physically open yet; SIB1 about to be asserted, SIB2 stays deasserted.
    phase1 = compose_bits(graph, frozenset(), frozenset({"SIB1"}), target_sib=None, payload_value=0)
    assert phase1 == [1, 0]

    # Phase 2: SIB1 now physically open; en=1 (bit 0 of TDR1) shifted in as the real payload.
    phase2 = compose_bits(
        graph, frozenset({"SIB1"}), frozenset({"SIB1"}), target_sib="SIB1", payload_value=0b0001
    )
    assert phase2 == [1, 0, 0, 0, 1, 0]
    assert len(phase2) == layout_bit_length(graph, frozenset({"SIB1"}))


def test_compose_bits_closed_slot_contributes_only_its_select_bit():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a", width=4)),))
    bits = compose_bits(graph, frozenset(), frozenset(), target_sib=None, payload_value=0xF)
    assert bits == [0]  # closed: no content bits reach the chain at all, payload ignored


def test_compose_bits_open_non_target_slot_gets_dont_care_zero_content():
    graph = PhysicalGraph(chain=(
        SibNode("sib_a", _instrument("a", width=2)),
        SibNode("sib_b", _instrument("b", width=2)),
    ))
    # sib_a physically open (garbage content expected) but sib_b is the real target; sib_b
    # itself is closed here (not in open_now/open_after), contributing just its own bit.
    bits = compose_bits(
        graph, frozenset({"sib_a"}), frozenset({"sib_a"}), target_sib="sib_b", payload_value=0
    )
    assert bits == [0, 0, 1, 0]  # sib_a: 2 don't-care zero bits + its own select (asserted); sib_b: closed


def test_compose_bits_select_value_follows_open_after_not_open_now():
    """The core phase-1 property: a slot physically open right now (per open_now) but NOT
    desired open after this shift (per open_after) gets its select bit driven to 0, closing
    it -- even though its instrument content is still physically present and gets shifted
    through as don't-care garbage."""
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a", width=3)),))
    bits = compose_bits(
        graph, frozenset({"sib_a"}), frozenset(), target_sib=None, payload_value=0
    )
    assert bits == [0, 0, 0, 0]  # 3 garbage content bits + select bit driven to 0 (closing it)


def test_compose_bits_can_open_a_sib_not_previously_physically_present():
    """The other core phase-1 property: a slot NOT physically open right now but desired
    open after this shift gets its select bit driven to 1 -- with zero content bits, since
    there's nothing physically in the chain there yet to shift through."""
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a", width=3)),))
    bits = compose_bits(
        graph, frozenset(), frozenset({"sib_a"}), target_sib=None, payload_value=0
    )
    assert bits == [1]  # no content bits (not physically open yet), select bit asserted


def test_instrumentless_open_sib_raises_not_a_bare_crash():
    graph = PhysicalGraph(chain=(SibNode("sib_a", instrument=None),))
    with pytest.raises(NotImplementedError, match="sib_a"):
        layout_bit_length(graph, frozenset({"sib_a"}))
    with pytest.raises(NotImplementedError, match="sib_a"):
        compose_bits(graph, frozenset({"sib_a"}), frozenset({"sib_a"}))


def test_instrumentless_closed_sib_is_fine():
    """A closed SIB never touches its own instrument at all -- instrument=None must not
    raise as long as it's never opened."""
    graph = PhysicalGraph(chain=(SibNode("sib_a", instrument=None),))
    assert layout_bit_length(graph, frozenset()) == 1
    assert compose_bits(graph, frozenset(), frozenset()) == [0]


def _nested_graph() -> PhysicalGraph:
    inner = SibNode("sib_inner", _instrument("deep", width=3))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    return PhysicalGraph(chain=(outer,))


def test_closed_hierarchy_sib_contributes_only_its_own_bit():
    """Regardless of how much is nested inside, a closed hierarchy SIB is exactly as cheap
    as a closed leaf -- its nested content isn't part of the live chain at all."""
    assert layout_bit_length(_nested_graph(), frozenset()) == 1


def test_open_hierarchy_but_still_closed_nested_child():
    """Opening the hierarchy SIB alone (its own nested child still closed) exposes the
    child's own closed-bypass bit, plus the hierarchy SIB's own select bit -- matching the
    real RTL/cross-sim spike's own finding that a single round can only open one previously-
    closed level."""
    graph = _nested_graph()
    assert layout_bit_length(graph, frozenset({"sib_outer"})) == 1 + 1


def test_open_hierarchy_and_open_nested_child():
    graph = _nested_graph()
    opened = frozenset({"sib_outer", "sib_inner"})
    assert layout_bit_length(graph, opened) == (3 + 1) + 1  # deep's width + its own + outer's own


def test_compose_bits_closed_hierarchy_contributes_only_its_select_bit():
    graph = _nested_graph()
    bits = compose_bits(graph, frozenset(), frozenset(), target_sib=None, payload_value=0)
    assert bits == [0]


def test_compose_bits_open_hierarchy_recurses_into_nested_chain():
    """target_sib names the leaf SIB directly -- the recursive walk matches it at whatever
    depth it actually occurs, exactly as compose_bits already does for a flat chain, with
    the hierarchy SIB's own select bit appended after its nested chain's own bits."""
    graph = _nested_graph()
    opened = frozenset({"sib_outer", "sib_inner"})
    bits = compose_bits(graph, opened, opened, target_sib="sib_inner", payload_value=0b101)
    # inner: 3 payload content bits (LSB first) + inner's own select bit, then outer's own.
    assert bits == [1, 0, 1, 1, 1]


def _mux_graph() -> PhysicalGraph:
    """A 2-arm mux with a wider-than-strictly-needed select field (2 bits for 2 arms, values
    1 and 2 -- 0 and 3 are both implicit bypass), each arm a differently-sized leaf
    instrument, so arm1 vs arm2 content width is directly observable in the numbers."""
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(_leaf_arm(1, "arm1_instr", width=3), _leaf_arm(2, "arm2_instr", width=2)),
    )
    return PhysicalGraph(chain=(mux,))


def test_bypassing_mux_contributes_only_its_select_width():
    graph = _mux_graph()
    assert layout_bit_length(graph, {}) == 2


def test_matched_mux_adds_the_matched_arms_own_width():
    graph = _mux_graph()
    assert layout_bit_length(graph, {"mux_a": 1}) == 2 + 3  # arm1
    assert layout_bit_length(graph, {"mux_a": 2}) == 2 + 2  # arm2


def test_a_value_matching_no_declared_arm_is_bypass_even_if_nonzero():
    """The direct generalization of a closed SibNode's own unmatched state: value 3 fits in
    this mux's 2-bit select field but isn't any declared arm's value, so it's bypass exactly
    like 0 would be -- not an error, not arm1, not arm2."""
    graph = _mux_graph()
    assert layout_bit_length(graph, {"mux_a": 3}) == 2


def test_compose_bits_bypassing_mux_has_no_content_only_select_field():
    """Phase-1-shaped: nothing physically open yet, targeting arm1 (value 1) after this
    shift. Select field is MSB first in position order: value 1 = 0b01 -> [0, 1]."""
    graph = _mux_graph()
    bits = compose_bits(graph, {}, {"mux_a": 1}, target_sib=None, payload_value=0)
    assert bits == [0, 1]
    assert len(bits) == layout_bit_length(graph, {})


def test_compose_bits_matched_mux_target_gets_real_payload():
    graph = _mux_graph()
    bits = compose_bits(
        graph, {"mux_a": 1}, {"mux_a": 1}, target_sib="mux_a", payload_value=0b101
    )
    # arm1: 3 payload content bits (LSB first) + select field (value 1 = 0b01, MSB first).
    assert bits == [1, 0, 1, 0, 1]
    assert len(bits) == layout_bit_length(graph, {"mux_a": 1})


def test_compose_bits_closing_an_open_arm_still_carries_its_content_as_dont_care():
    """Matches sib_layout's own documented finding from the RTL spike: closing (or
    switching) still carries the CURRENTLY matched arm's own content width as dummy cycles."""
    graph = _mux_graph()
    bits = compose_bits(graph, {"mux_a": 1}, {}, target_sib=None, payload_value=0xFF)
    # arm1's 3 content bits are don't-care zero (not the target), select field = 0 (bypass).
    assert bits == [0, 0, 0, 0, 0]
    assert len(bits) == layout_bit_length(graph, {"mux_a": 1})


def test_compose_bits_switching_arms_directly_carries_the_old_arms_content_not_the_new_arms():
    """The single most novel case this whole feature exists for: switching from arm1 to arm2
    in one round. Content bits are still arm1's own width (3), not arm2's (2) -- the
    currently-matched arm's content, never the target arm's -- while the select field encodes
    the NEW target (value 2 = 0b10, MSB first -> [1, 0])."""
    graph = _mux_graph()
    bits = compose_bits(graph, {"mux_a": 1}, {"mux_a": 2}, target_sib=None, payload_value=0)
    assert bits == [0, 0, 0, 1, 0]
    assert len(bits) == layout_bit_length(graph, {"mux_a": 1})  # arm1's width, not arm2's


def test_select_field_bit_order_is_msb_first_not_lsb_first():
    """A 3-bit select field with a non-palindromic value (6 = 0b110) makes the two possible
    orders unambiguous: MSB-first gives [1, 1, 0]; LSB-first would give [0, 1, 1]."""
    mux = ScanMuxNode(
        "mux_a", select_width=3,
        arms=(_leaf_arm(6, "a", width=1), _leaf_arm(7, "b", width=1)),
    )
    graph = PhysicalGraph(chain=(mux,))
    bits = compose_bits(graph, {}, {"mux_a": 6}, target_sib=None, payload_value=0)
    assert bits == [1, 1, 0]


def test_scan_mux_with_a_nested_arm_recurses_like_a_hierarchy_sib():
    """A mux arm can itself be a hierarchy slot -- matching arm0 alone doesn't cascade into
    ALSO opening sib_inner (it needs its own separate entry in the open-map/its own round),
    exactly mirroring test_open_hierarchy_but_still_closed_nested_child's own finding for a
    plain hierarchy SibNode."""
    inner = SibNode("sib_inner", _instrument("deep", width=3))
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(ScanArm(values=(0,), nested=(inner,)), _leaf_arm(1, "b", width=1)),
    )
    graph = PhysicalGraph(chain=(mux,))
    assert layout_bit_length(graph, {}) == 1  # bypass: just the select bit
    assert layout_bit_length(graph, {"mux_a": 0}) == 1 + 1  # arm0 matched, sib_inner still closed
    opened = {"mux_a": 0, "sib_inner": 1}
    assert layout_bit_length(graph, opened) == 1 + (3 + 1)  # sib_inner open too: its full width
    bits = compose_bits(graph, opened, opened, target_sib="sib_inner", payload_value=0b110)
    # sib_inner: 3 payload bits (LSB first) + its own select bit (1), then mux_a's own
    # select field (1 bit, value 0).
    assert bits == [0, 1, 1, 1, 0]


def test_sib_nesting_a_scan_mux_recurses_the_other_direction():
    """The reverse composition -- a SibNode's own nested chain holding a ScanMuxNode -- uses
    the exact same recursive _walk, just entered from the SibNode branch instead."""
    mux = ScanMuxNode("mux_inner", select_width=1, arms=(_leaf_arm(0, "a", width=2), _leaf_arm(1, "b", width=1)))
    outer = SibNode("sib_outer", instrument=None, nested=(mux,))
    graph = PhysicalGraph(chain=(outer,))
    assert layout_bit_length(graph, frozenset()) == 1  # sib_outer closed: mux never reached
    opened = {"sib_outer": 1, "mux_inner": 0}
    assert layout_bit_length(graph, opened) == (2 + 1) + 1  # arm0's width + mux's select + outer's own


def test_scan_mux_arm_without_instrument_or_nested_raises():
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(ScanArm(values=(0,)), _leaf_arm(1, "b", width=1)),
    )
    graph = PhysicalGraph(chain=(mux,))
    with pytest.raises(NotImplementedError, match="mux_a"):
        layout_bit_length(graph, {"mux_a": 0})
    with pytest.raises(NotImplementedError, match="mux_a"):
        compose_bits(graph, {"mux_a": 0}, {"mux_a": 0})
