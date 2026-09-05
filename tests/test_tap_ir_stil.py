"""Pure-Python tests for the STIL emitter (implementation_plan.md §7 Stage 13). No external
tool involved here -- structural/textual assertions against the real STIL grammar confirmed
this session, mirroring tests/test_tap_ir_svf.py's own style. Live validation against the real
Semi-ATE-STIL parser is a separate file (test_tap_ir_stil_semiate_validation.py).
"""

from __future__ import annotations

import pytest

from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR
from warptap.tap_ir_stil import TapIrStilError, to_stil


def test_empty_ops_still_emits_every_required_block():
    text = to_stil([])
    for block in ("STIL 1.0;", "Signals {", "SignalGroups {", "Timing tap_timing {",
                  "WaveformTable jtag_wft {", "PatternBurst", "PatternExec", "Pattern"):
        assert block in text


def test_block_order_matches_empirically_confirmed_requirement():
    """SignalGroups before Timing; PatternBurst and PatternExec both before Pattern -- a real,
    undocumented-elsewhere ordering requirement found by actually feeding files through
    Semi-ATE-STIL this session (see module docstring)."""
    text = to_stil([])
    assert text.index("SignalGroups {") < text.index("Timing tap_timing {")
    assert text.index("PatternBurst") < text.index("Pattern warptap_pattern {")
    assert text.index("PatternExec") < text.index("Pattern warptap_pattern {")


def test_only_tap_signals_declared_when_no_pulsepin_present():
    text = to_stil([ShiftDR(bits=1, tdi=0)])
    signals_block = text.split("Signals {")[1].split("}")[0]
    assert "tck" in signals_block
    assert "tms" in signals_block
    assert "tdi" in signals_block
    assert "tdo" in signals_block
    assert "sysclk" not in signals_block


def test_tdo_out_direction_everything_else_in():
    text = to_stil([])
    assert "tdo Out;" in text
    assert "tck In;" in text
    assert "tms In;" in text
    assert "tdi In;" in text


def test_gotostate_produces_real_navigation_vectors_not_dropped():
    """Unlike SVF/STAPL (whose SIR/SDR statements navigate implicitly), STIL has no such
    automatic navigation -- GotoState must produce real per-cycle vectors."""
    text = to_stil([GotoState(TapState.SHIFT_DR)])
    vector_lines = [l for l in text.splitlines() if l.strip().startswith("V {")]
    assert len(vector_lines) == 3  # RUN_TEST_IDLE -> SELECT_DR_SCAN -> CAPTURE_DR -> SHIFT_DR


def test_shiftdr_with_no_tdo_mask_produces_dont_compare_vectors():
    text = to_stil([ShiftDR(bits=3, tdi=0b101)])
    vector_lines = [l for l in text.splitlines() if l.strip().startswith("V {")]
    assert len(vector_lines) == 3
    assert all("tdo=X;" in l for l in vector_lines)


def test_shiftdr_with_tdo_mask_produces_compare_vectors():
    text = to_stil([ShiftDR(bits=2, tdi=0b01, tdo=0b01, mask=0b11)])
    vector_lines = [l for l in text.splitlines() if l.strip().startswith("V {")]
    assert len(vector_lines) == 2
    # bit0=1 -> H, bit1=0 -> L (LSB-first chronological, matches tap_ir.py's own convention)
    assert "tdo=H;" in vector_lines[0]
    assert "tdo=L;" in vector_lines[1]


def test_partial_mask_leaves_unmasked_cycles_dont_compare():
    text = to_stil([ShiftDR(bits=2, tdi=0b00, tdo=0b01, mask=0b01)])
    vector_lines = [l for l in text.splitlines() if l.strip().startswith("V {")]
    assert "tdo=H;" in vector_lines[0]
    assert "tdo=X;" in vector_lines[1]


def test_shiftir_uses_same_vector_shape_as_shiftdr():
    text_ir = to_stil([ShiftIR(bits=1, tdi=1)])
    text_dr = to_stil([ShiftDR(bits=1, tdi=1)])
    ir_vectors = [l for l in text_ir.splitlines() if l.strip().startswith("V {")]
    dr_vectors = [l for l in text_dr.splitlines() if l.strip().startswith("V {")]
    assert ir_vectors == dr_vectors


def test_runtest_produces_one_idle_vector_per_cycle():
    text = to_stil([Runtest(5)])
    vector_lines = [l for l in text.splitlines() if l.strip().startswith("V {")]
    assert len(vector_lines) == 5
    assert all("tms=0; tdi=0;" in l for l in vector_lines)


def test_pulsepin_declares_its_own_signal_and_waveformtable():
    text = to_stil([PulsePin("sysclk", 2)], pulse_periods={"sysclk": "20ns"})
    assert "sysclk In;" in text
    assert "WaveformTable pulse_sysclk_wft {" in text
    assert "Period '20ns';" in text


def test_pulsepin_switches_waveformtable_and_holds_tap_pins():
    text = to_stil(
        [ShiftDR(bits=1, tdi=0), PulsePin("sysclk", 1), ShiftDR(bits=1, tdi=0)],
        pulse_periods={"sysclk": "20ns"},
    )
    lines = text.splitlines()
    w_lines = [l.strip() for l in lines if l.strip().startswith("W ")]
    assert w_lines == ["W jtag_wft;", "W pulse_sysclk_wft;", "W jtag_wft;"]
    pulse_vector = next(l for l in lines if "sysclk=1;" in l)
    assert "tck=P;" in pulse_vector
    assert "tms=P;" in pulse_vector
    assert "tdi=P;" in pulse_vector


def test_pulsepin_hold_pins_drive_explicit_values():
    text = to_stil(
        [PulsePin("sysclk", 1, hold_pins=(("pi_a", 1), ("pi_b", 0)))],
        pulse_periods={"sysclk": "20ns"},
    )
    pulse_vector = next(l for l in text.splitlines() if "sysclk=1;" in l)
    assert "pi_a=1;" in pulse_vector
    assert "pi_b=0;" in pulse_vector


def test_pulsepin_target_missing_from_pulse_periods_raises_named_error():
    with pytest.raises(TapIrStilError, match="sysclk"):
        to_stil([PulsePin("sysclk", 1)])


def test_unsupported_op_raises_named_error():
    with pytest.raises(TapIrStilError, match="does not support"):
        to_stil(["not an op"])
