"""Pure-Python tests for the PDL interpreter (implementation_plan.md §7 Stage 5, §3.3). No
Yosys/RTL involved -- built directly against sib_plan.build_sib_plan's output, mirroring
test_sib_retarget.py's/test_sib_plan.py's style.
"""

from __future__ import annotations

import pytest

from warptap.icl_model import Alias, ICLAddressError
from warptap.pdl_interpreter import PDLError, PDLInterpreter
from warptap.sib_layout import layout_bit_length
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, bits_from_int

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]


def _interpreter() -> PDLInterpreter:
    graph, root = build_sib_plan(_SPECS)
    return PDLInterpreter(graph, root)


def _interpreter_with_aliases() -> PDLInterpreter:
    """A single 4-bit instrument with two declared sub-fields -- `lo` = bits[1:0], `hi` =
    bits[3:2] -- for Stage 15's named sub-field addressing tests."""
    specs = [
        InstrumentSpec(
            "status_reg",
            width=4,
            capture_value=0,
            aliases=(Alias("lo", 0, 1), Alias("hi", 2, 3)),
        )
    ]
    graph, root = build_sib_plan(specs)
    return PDLInterpreter(graph, root)


def test_itarget_resolves_and_scopes_iwrite():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iWrite(5)
    assert pdl._pending_writes == {"sensor_a": 5}


def test_itarget_bad_path_propagates_icladdresserror():
    pdl = _interpreter()
    with pytest.raises(ICLAddressError, match="ghost"):
        pdl.iTarget("ghost")


def test_iwrite_before_itarget_raises_pdlerror():
    pdl = _interpreter()
    with pytest.raises(PDLError, match="iWrite"):
        pdl.iWrite(1)


def test_iread_before_itarget_raises_pdlerror():
    pdl = _interpreter()
    with pytest.raises(PDLError, match="iRead"):
        pdl.iRead(1)


def test_iapply_before_itarget_raises_pdlerror():
    pdl = _interpreter()
    with pytest.raises(PDLError, match="iApply"):
        pdl.iApply()


def test_iapply_returns_two_shiftdr_ops_bracketed_by_gotostate():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    ops = pdl.iApply()
    assert [type(op) for op in ops] == [GotoState, ShiftDR, GotoState, GotoState, ShiftDR, GotoState]
    assert ops[0] == GotoState(TapState.SHIFT_DR)
    assert ops[2] == GotoState(TapState.RUN_TEST_IDLE)
    assert ops[3] == GotoState(TapState.SHIFT_DR)
    assert ops[5] == GotoState(TapState.RUN_TEST_IDLE)


def test_iapply_appends_to_program():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    ops = pdl.iApply()
    assert pdl.program == ops


def test_first_iapply_phase1_length_equals_all_closed_chain_length():
    """Nothing has ever been open before -- phase 1's length must be exactly
    len(graph.chain) (every SIB contributes its own 1 bit, none has instrument content
    physically present yet)."""
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    ops = pdl.iApply()
    phase1_shift = ops[1]
    assert isinstance(phase1_shift, ShiftDR)
    assert phase1_shift.bits == len(graph.chain) == 2


def test_iapply_phase2_length_reflects_opened_targets_instrument_width():
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    ops = pdl.iApply()
    phase2_shift = ops[4]
    assert isinstance(phase2_shift, ShiftDR)
    # sensor_a's own SIB (open, width 3) + sensor_b's SIB (closed) = (3+1) + 1 = 5
    assert phase2_shift.bits == layout_bit_length(graph, frozenset({"sib_sensor_a"})) == 5


def test_sequential_iapply_to_different_instruments_second_phase1_reflects_first_still_open():
    """The core state-tracking subtlety implementation_plan.md's own text doesn't
    anticipate: a second iApply to a DIFFERENT instrument must size its own phase 1 against
    whatever the FIRST iApply left open, not a static len(graph.chain) baseline."""
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)

    pdl.iTarget("sensor_a")
    pdl.iApply()  # leaves sib_sensor_a open (sensor_a's width=3 -> 3+1=4 bits there)

    pdl.iTarget("sensor_b")
    second_ops = pdl.iApply()
    second_phase1 = second_ops[1]
    assert isinstance(second_phase1, ShiftDR)
    # sib_sensor_a still physically open (3+1=4 bits) + sib_sensor_b closed (1 bit) = 5
    expected = layout_bit_length(graph, frozenset({"sib_sensor_a"}))
    assert second_phase1.bits == expected == 5

    # And the SECOND call's own phase-1 select pattern must open sensor_b, close sensor_a:
    # decode chronological-order tdi back to position order (reverse) and check the tail
    # two bits are sib_sensor_a's own (now-deasserted) select bit and... actually simplest:
    # after this iApply, sensor_a's SIB must no longer be tracked as open. (Now a dict, not a
    # frozenset -- sib_retarget.stage_open_sequence returns dict[str, int] rounds since the
    # multi-arm ScanMux plan's Phase 2; _currently_open's own type formally generalizes in
    # that plan's Phase 4, but already receives whatever stage_open_sequence itself returns.)
    assert pdl._currently_open == {"sib_sensor_b": 1}


def test_iwrite_payload_reaches_phase2_tdi():
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_b")  # width 2
    pdl.iWrite(0b10)
    ops = pdl.iApply()
    phase2_shift = ops[4]
    # sib_sensor_a stays closed (1 bit) + sib_sensor_b opens (2 content + 1 select = 3) = 4
    assert phase2_shift.bits == 4
    fed_bits = bits_from_int(phase2_shift.tdi, phase2_shift.bits)
    # Position order: [sib_sensor_a closed select, sensor_b bit0, sensor_b bit1, sib_sensor_b select]
    # Chronological (fed) order is the reverse of position order (last fed lands nearest TDI).
    position_order = list(reversed(fed_bits))
    assert position_order == [0, 0, 1, 1]  # sensor_a closed(0); sensor_b=0b10 -> [0,1]; select=1


def test_iwrite_pending_write_cleared_after_iapply():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iWrite(5)
    pdl.iApply()
    assert pdl._pending_writes == {}


def test_iread_populates_tdo_and_mask_on_phase2():
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")  # width 3, chained first (nearest TDI)
    pdl.iRead(0b101)
    ops = pdl.iApply()
    phase2_shift = ops[4]
    assert phase2_shift.tdo is not None
    assert phase2_shift.mask is not None

    fed_mask_bits = bits_from_int(phase2_shift.mask, phase2_shift.bits)
    position_mask = list(reversed(fed_mask_bits))
    # sensor_a (open, target): [content, content, content, select]; sensor_b (still closed): [select]
    assert position_mask == [1, 1, 1, 0, 0]  # 3 content bits masked in; neither select bit is

    fed_tdo_bits = bits_from_int(phase2_shift.tdo, phase2_shift.bits)
    position_tdo = list(reversed(fed_tdo_bits))
    # 0b101 LSB-first (1,0,1) + sensor_a's select (asserted, 1) + sensor_b's select (closed, 0)
    assert position_tdo == [1, 0, 1, 1, 0]


def test_iread_pending_read_cleared_after_iapply():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iRead(5)
    pdl.iApply()
    assert pdl._pending_reads == {}


def test_no_pending_read_leaves_tdo_and_mask_none():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iWrite(1)  # no iRead
    ops = pdl.iApply()
    phase2_shift = ops[4]
    assert phase2_shift.tdo is None
    assert phase2_shift.mask is None


def test_irunloop_appends_directly_not_deferred():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iRunLoop(100)
    assert len(pdl.program) == 1
    op = pdl.program[0]
    assert op.count == 100
    assert op.run_state is TapState.RUN_TEST_IDLE
    assert op.end_state is TapState.RUN_TEST_IDLE


def test_irunloop_does_not_require_a_scope():
    pdl = _interpreter()
    pdl.iRunLoop(10)  # no iTarget called at all
    assert len(pdl.program) == 1


def test_program_accumulates_across_multiple_commands():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iRunLoop(10)
    first_apply_ops = pdl.iApply()
    assert pdl.program[0].count == 10
    assert pdl.program[1:] == first_apply_ops


# --- Stage 15: named sub-field addressing (real ICL Alias) ---------------------------------


def test_iwrite_with_field_merges_into_only_that_sub_range():
    pdl = _interpreter_with_aliases()
    pdl.iTarget("status_reg")
    pdl.iWrite(0b11, field="hi")  # bits[3:2]
    assert pdl._pending_writes == {"status_reg": 0b1100}


def test_iwrite_with_field_preserves_a_previously_queued_different_field():
    pdl = _interpreter_with_aliases()
    pdl.iTarget("status_reg")
    pdl.iWrite(0b01, field="lo")  # bits[1:0]
    pdl.iWrite(0b11, field="hi")  # bits[3:2] -- must not clobber lo's own bits
    assert pdl._pending_writes == {"status_reg": 0b1101}


def test_iwrite_unknown_field_raises_named_pdlerror():
    pdl = _interpreter_with_aliases()
    pdl.iTarget("status_reg")
    with pytest.raises(PDLError, match="ghost"):
        pdl.iWrite(1, field="ghost")


def test_iwrite_with_no_field_still_addresses_whole_instrument():
    """field=None (the default) is byte-identical to Stage 5's original whole-instrument
    behavior -- reconfirmed against the SAME aliased instrument used by the field-based
    tests above, not just the no-alias _SPECS fixture."""
    pdl = _interpreter_with_aliases()
    pdl.iTarget("status_reg")
    pdl.iWrite(0b1010)
    assert pdl._pending_writes == {"status_reg": 0b1010}


def test_iread_with_field_masks_only_that_sub_range_in_phase2():
    pdl = _interpreter_with_aliases()
    pdl.iTarget("status_reg")
    pdl.iRead(0b11, field="hi")  # bits[3:2]
    ops = pdl.iApply()
    phase2 = ops[4]

    fed_mask_bits = bits_from_int(phase2.mask, phase2.bits)
    position_mask = list(reversed(fed_mask_bits))
    assert position_mask == [0, 0, 1, 1, 0]  # lo(2 bits) unmasked, hi(2 bits) masked, select=0

    fed_tdo_bits = bits_from_int(phase2.tdo, phase2.bits)
    position_tdo = list(reversed(fed_tdo_bits))
    # hi=0b11 LSB-first at its own bit offset (positions 2-3); lo's own positions (0-1) are
    # don't-care content (0, since no write was queued); trailing bit is the SIB's own select,
    # asserted (1) since it's the open/target SIB.
    assert position_tdo == [0, 0, 1, 1, 1]


def test_iread_unknown_field_raises_named_pdlerror():
    pdl = _interpreter_with_aliases()
    pdl.iTarget("status_reg")
    with pytest.raises(PDLError, match="ghost"):
        pdl.iRead(1, field="ghost")


def test_iread_with_no_field_still_masks_the_whole_instrument():
    """field=None (the default) is byte-identical to Stage 5's original whole-instrument
    mask -- reconfirmed against the aliased instrument."""
    pdl = _interpreter_with_aliases()
    pdl.iTarget("status_reg")
    pdl.iRead(0b1010)
    ops = pdl.iApply()
    phase2 = ops[4]
    fed_mask_bits = bits_from_int(phase2.mask, phase2.bits)
    position_mask = list(reversed(fed_mask_bits))
    assert position_mask == [1, 1, 1, 1, 0]  # all 4 content bits masked in, select bit is not


# --- Stage 15: iRunLoop's -sck clock selector -----------------------------------------------


def test_irunloop_default_still_appends_runtest_not_pulsepin():
    """sck_port=None (the default) is byte-identical to Stage 5's original -tck behavior."""
    pdl = _interpreter()
    pdl.iRunLoop(10)
    assert len(pdl.program) == 1
    assert isinstance(pdl.program[0], Runtest)


def test_irunloop_with_sck_port_appends_pulsepin_not_runtest():
    pdl = _interpreter()
    pdl.iRunLoop(5, sck_port="sysclk")
    assert len(pdl.program) == 1
    op = pdl.program[0]
    assert isinstance(op, PulsePin)
    assert op.port == "sysclk"
    assert op.count == 5


def test_irunloop_sck_recorded_in_history():
    from warptap.pdl_history import PdlRunLoopStmt

    pdl = _interpreter()
    pdl.iRunLoop(5, sck_port="sysclk")
    assert pdl.history == [PdlRunLoopStmt(5, "sysclk")]


def test_irunloop_default_recorded_in_history_with_no_sck_port():
    from warptap.pdl_history import PdlRunLoopStmt

    pdl = _interpreter()
    pdl.iRunLoop(5)
    assert pdl.history == [PdlRunLoopStmt(5, None)]
