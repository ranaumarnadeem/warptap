"""Pure-Python tests for the ICL connectivity check (implementation_plan.md §7 Stage 6). No
Yosys/RTL involved -- synthetic PhysicalGraph objects only, mirroring test_sib_layout.py's
style.
"""

from __future__ import annotations

import pytest

from warptap.icl_model import InstrumentNode, PhysicalGraph, SibNode
from warptap.sib_layout import layout_bit_length
from warptap.sib_model import SibNetworkRegister
from warptap.sib_overshift import (
    build_overshift_ops,
    default_sentinel_pattern,
    diagnose_overshift,
    expected_probe_tdo,
)
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftDR, ShiftIR, bits_to_int
from warptap.tap_ir_play import play
from warptap.tap_model import Instruction, TapModel

IR_WIDTH = 4
OPCODE_EXTEST = 0


def _instrument(name: str, width: int, capture_value: int = 0) -> InstrumentNode:
    return InstrumentNode(name=name, width=width, capture_value=capture_value)


def _simple_graph(width_a: int = 3, capture_value_a: int = 0b101) -> PhysicalGraph:
    return PhysicalGraph(chain=(
        SibNode("sib_a", _instrument("a", width=width_a, capture_value=capture_value_a)),
        SibNode("sib_b", _instrument("b", width=1, capture_value=0)),
    ))


def _shift_ops(ops):
    return [op for op in ops if isinstance(op, ShiftDR)]


def _select_extest_ops():
    """SibNetworkRegister is only the active data register once EXTEST is selected --
    TapModel defaults to IDCODE on reset. build_overshift_ops (like PDLInterpreter.iApply)
    deliberately never selects an instruction itself; every test that actually EXECUTES
    probe ops must prepend this, mirroring test_pdl_interpreter_cross_sim.py's own
    convention."""
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _probe_ops(graph, target_open, **kwargs):
    return _select_extest_ops() + build_overshift_ops(graph, target_open, **kwargs)


def _fresh_model_and_register(graph):
    model = TapModel(has_idcode=True)
    reg = SibNetworkRegister(graph)
    model.register_data_register(Instruction.EXTEST, reg)
    model.reset()
    reg.reset()
    model.tick(0, 0)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE settle
    return model, reg


def test_default_sentinel_pattern_is_periodic_0011():
    assert default_sentinel_pattern(0) == 0
    pattern = default_sentinel_pattern(8)
    bits = [(pattern >> i) & 1 for i in range(8)]
    assert bits == [0, 0, 1, 1, 0, 0, 1, 1]


def test_probe_length_is_layout_plus_margin():
    graph = _simple_graph()
    target_open = frozenset({"sib_a"})
    ops = build_overshift_ops(graph, target_open, margin=5)
    probe = _shift_ops(ops)[-1]
    assert probe.bits == layout_bit_length(graph, target_open) + 5


def test_negative_margin_raises():
    graph = _simple_graph()
    with pytest.raises(ValueError, match="margin"):
        build_overshift_ops(graph, frozenset({"sib_a"}), margin=-1)


def test_margin_zero_is_legal_but_minimal():
    graph = _simple_graph()
    target_open = frozenset({"sib_a"})
    ops = build_overshift_ops(graph, target_open, margin=0)
    probe = _shift_ops(ops)[-1]
    assert probe.bits == layout_bit_length(graph, target_open)


def test_self_restoring_property():
    """The probe must leave the network in exactly target_open afterward -- Update-DR
    fires on exit from Shift-DR regardless, so this is a real property to prove, not an
    assumption that naturally falls out of the RTL/model shape."""
    graph = _simple_graph()
    target_open = frozenset({"sib_a"})
    ops = _probe_ops(graph, target_open, margin=4)

    model, reg = _fresh_model_and_register(graph)
    play(model, ops)

    open_slots = {s.sib_name for s in reg._slots if s.sib_po}
    assert open_slots == target_open


def test_happy_path_matches_and_reports_passed():
    graph = _simple_graph()
    target_open = frozenset({"sib_a"})
    ops = _probe_ops(graph, target_open, margin=4)
    expected = expected_probe_tdo(graph, ops)
    observed = expected_probe_tdo(graph, ops)  # a second, independent fresh subject
    probe = _shift_ops(ops)[-1]

    result = diagnose_overshift(
        graph, target_open, probe.bits, layout_bit_length(graph, target_open), expected, observed
    )
    assert result.passed
    assert result.diagnosis == "match"


def test_margin_zero_can_let_a_real_length_defect_slip_through():
    """The margin rationale, proven as an existence property rather than a universal
    direction rule. Empirically (verified directly, not assumed): exact bit-for-bit stream
    comparison is sensitive enough that MOST structural defects -- including a sib_a wider
    than declared -- get caught even at margin=0, because the comparison only needs ONE
    mismatched bit anywhere in the observed window, not a recognizable sentinel arriving on
    schedule the way Keim's own timing-only technique needs. This is a real, additional
    strength of the exact-comparison upgrade over the historical technique (see module
    docstring) -- but it means margin=0 is not GUARANTEED blind to over-length defects the
    way the plan's own mechanistic story (single-pattern arrival) would suggest for Keim's
    narrower method.

    What margin=0 genuinely cannot provide is a RELIABLE guarantee: whether a given defect
    is caught at margin=0 depends on whether its content happens to coincidentally echo back
    something that matches the reference's own prediction for that same cycle -- this is
    exactly what a narrower subject (sib_a width 3 -> 1) demonstrates: at margin=0, the
    subject's own 3-element live layout wraps and its own earlier-fed sentinel-less tail
    bits echo back in a way that happens to match the reference's prediction for those same
    cycles, so the defect goes UNDETECTED -- until margin=1 gives the comparison one more
    cycle of real content to disagree on, which it does."""
    graph_ref = _simple_graph(width_a=3)
    graph_narrower = _simple_graph(width_a=1)
    target_open = frozenset({"sib_a"})

    def run(graph_subject, margin):
        ops = _probe_ops(graph_ref, target_open, margin=margin)
        expected = expected_probe_tdo(graph_ref, ops)
        observed = expected_probe_tdo(graph_subject, ops)
        probe = _shift_ops(ops)[-1]
        return diagnose_overshift(
            graph_ref, target_open, probe.bits,
            layout_bit_length(graph_ref, target_open), expected, observed,
        )

    assert run(graph_narrower, margin=0).passed is True  # real defect slips through
    assert run(graph_narrower, margin=1).passed is False  # one more cycle catches it


def test_wider_subject_reliably_detected_regardless_of_margin():
    """The complementary, positive finding from the same empirical sweep: a sib_a WIDER
    than declared (the classic "stuck-open SIB exposes an unexpected subnet" shape Keim's
    own worked example 1 describes) is caught at every margin tested, including margin=0 --
    exact-stream comparison's sensitivity means over-length defects are, in practice, the
    easier case to catch for this implementation, not the harder one Keim's own timing-only
    technique treats them as."""
    graph_ref = _simple_graph(width_a=3)
    graph_wider = _simple_graph(width_a=5)
    target_open = frozenset({"sib_a"})

    for margin in (0, 1, 4):
        ops = _probe_ops(graph_ref, target_open, margin=margin)
        expected = expected_probe_tdo(graph_ref, ops)
        observed = expected_probe_tdo(graph_wider, ops)
        probe = _shift_ops(ops)[-1]
        result = diagnose_overshift(
            graph_ref, target_open, probe.bits,
            layout_bit_length(graph_ref, target_open), expected, observed,
        )
        assert not result.passed


def test_content_only_defect_detected_even_with_zero_margin():
    """Same declared length, different captured bit content -- caught by exact-stream
    comparison, a real advantage over Keim's own timing-only technique (which would see
    nothing wrong here at all, since arrival timing is identical)."""
    graph_ref = _simple_graph(capture_value_a=0b101)
    graph_subject = _simple_graph(capture_value_a=0b110)
    target_open = frozenset({"sib_a"})
    ops = _probe_ops(graph_ref, target_open, margin=0)
    expected = expected_probe_tdo(graph_ref, ops)
    observed = expected_probe_tdo(graph_subject, ops)
    probe = _shift_ops(ops)[-1]
    result = diagnose_overshift(
        graph_ref, target_open, probe.bits,
        layout_bit_length(graph_ref, target_open), expected, observed,
    )
    assert not result.passed
    assert result.diagnosis != "match"


def test_stuck_open_sib_detected():
    """Simulates a SIB stuck-at-assert (Keim's own named fault class): after the normal
    phase-1 retargeting sequence runs, directly force sib_b's po to 1, as if it never
    responded to phase 1's own command to stay closed, then run the probe and confirm it's
    detected."""
    graph = _simple_graph()
    target_open = frozenset({"sib_a"})
    ops = _probe_ops(graph, target_open, margin=4)
    # ops = [select-EXTEST preamble (3 ops), phase 1 (3 ops), phase 2/probe (3 ops)].
    preamble_and_phase1, phase2_ops = ops[:6], ops[6:]
    expected = expected_probe_tdo(graph, ops)

    model, reg = _fresh_model_and_register(graph)
    play(model, preamble_and_phase1)
    sib_b = next(s for s in reg._slots if s.sib_name == "sib_b")
    assert sib_b.sib_po == 0  # confirm phase 1 really did command it closed
    sib_b.sib_po = 1  # ...then simulate it not actually responding (stuck open)
    observed = play(model, phase2_ops)[-1]

    probe = _shift_ops(ops)[-1]
    result = diagnose_overshift(
        graph, target_open, probe.bits, layout_bit_length(graph, target_open), expected, observed
    )
    assert not result.passed


def test_same_length_swap_is_a_known_blind_spot():
    """Two structurally-identical slots swapped in chain order -- Keim's own admitted
    limitation (NTF17 slide 21: same-length paths indistinguishable). Documented here as a
    proven, expected property, not a silent gap."""
    x_first = PhysicalGraph(chain=(
        SibNode("sib_x", _instrument("x", width=2, capture_value=0b01)),
        SibNode("sib_y", _instrument("y", width=2, capture_value=0b01)),
    ))
    y_first = PhysicalGraph(chain=(
        SibNode("sib_y", _instrument("y", width=2, capture_value=0b01)),
        SibNode("sib_x", _instrument("x", width=2, capture_value=0b01)),
    ))
    target_open = frozenset({"sib_x", "sib_y"})
    ops = _probe_ops(x_first, target_open, margin=3)
    expected = expected_probe_tdo(x_first, ops)
    observed = expected_probe_tdo(y_first, ops)
    probe = _shift_ops(ops)[-1]
    result = diagnose_overshift(
        x_first, target_open, probe.bits, layout_bit_length(x_first, target_open),
        expected, observed,
    )
    assert result.passed  # structurally identical slots -- genuinely indistinguishable


def test_constant_output_diagnosed_as_stuck_at_signature():
    graph = _simple_graph()
    target_open = frozenset({"sib_a"})
    probe_bits = 8
    result = diagnose_overshift(
        graph, target_open, probe_bits, layout_bit_length(graph, target_open),
        expected_tdo=0b10101100, observed_tdo=0,
    )
    assert not result.passed
    assert "stuck-at" in result.diagnosis
