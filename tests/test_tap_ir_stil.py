"""Pure-Python tests for the STIL emitter (implementation_plan.md §7 Stage 13). No external
tool involved here -- structural/textual assertions against the real STIL grammar confirmed
this session, mirroring tests/test_tap_ir_svf.py's own style. Live validation against the real
Semi-ATE-STIL parser is a separate file (test_tap_ir_stil_semiate_validation.py).
"""

from __future__ import annotations

import pytest

from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, PulsePin, Runtest, SetPins, ShiftDR, ShiftIR
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


def test_runtest_is_one_idle_vector_looped_count_times():
    text = to_stil([ShiftDR(bits=1, tdi=0), Runtest(5)])
    vector_lines = [l for l in text.splitlines() if l.strip().startswith("V {")]
    assert len(vector_lines) == 2
    assert "  Loop 5 {\n    V { tck=1; tms=0; tdi=0; tdo=X; }\n  }\n" in text


def test_a_loop_that_would_open_the_pattern_starts_with_one_plain_vector():
    """Semi-ATE-STIL's compiler rejects a Loop with no vector before it."""
    text = to_stil([Runtest(5), PulsePin("sysclk", 1000)], pulse_periods={"sysclk": "10ns"})
    pattern = text.split("Pattern warptap_pattern {\n")[1]
    assert pattern.startswith(
        "  W jtag_wft;\n  V { tck=1; tms=0; tdi=0; tdo=X; sysclk=P; }\n  Loop 4 {"
    )
    assert "  W pulse_sysclk_wft;\n  Loop 1000 {\n    V { tck=P;" in pattern


def test_a_count_of_one_or_zero_is_not_a_loop():
    text = to_stil(
        [Runtest(1), Runtest(0), PulsePin("sysclk", 1)], pulse_periods={"sysclk": "10ns"}
    )
    assert "Loop" not in text
    assert len([l for l in text.splitlines() if l.strip().startswith("V {")]) == 2


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


def test_tdo_is_compared_before_tck_rises_and_tck_idles_low():
    """T/4 strobe, T/2 rise, 3T/4 fall (to_stil's docstring); the replay test proves it on RTL."""
    text = to_stil([], jtag_period="100ns")
    assert "tck { 01 { '0ns' D; '50ns' D/U; '75ns' D; }}" in text
    assert "tdo { HLX { '0ns' X; '25ns' H/L/X; }}" in text


def test_edge_times_keep_the_period_unit_and_its_fractions():
    text = to_stil([PulsePin("sysclk", 1)], jtag_period="50ns", pulse_periods={"sysclk": "0.1us"})
    assert "'12.5ns' H/L/X" in text
    assert "sysclk { 01 { '0ns' D; '0.05us' D/U; '0.075us' D; }}" in text


@pytest.mark.parametrize("period", ["50", "0ns", "fast", "50 ns"])
def test_a_period_that_is_not_a_positive_time_raises_named_error(period):
    with pytest.raises(TapIrStilError, match="jtag_period"):
        to_stil([], jtag_period=period)


def _vectors(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip().startswith("V {")]


def test_declared_inputs_hold_their_value_and_outputs_are_never_compared():
    text = to_stil(
        [Runtest(1), PulsePin("sysclk", 1)], pulse_periods={"sysclk": "20ns"},
        inputs={"rst_n": 1, "test_mode": 0}, outputs=["status_out"],
    )
    assert "rst_n In;" in text and "test_mode In;" in text and "status_out Out;" in text
    jtag_vector, pulse_vector = _vectors(text)
    for vector in (jtag_vector, pulse_vector):
        assert "rst_n=1;" in vector and "test_mode=0;" in vector and "status_out=X;" in vector
    assert "status_out { X { '0ns' X; }}" in text


def test_hold_pins_override_a_declared_input_for_the_pulse_only():
    text = to_stil(
        [PulsePin("sysclk", 1, hold_pins=(("test_mode", 1),)), Runtest(1)],
        pulse_periods={"sysclk": "20ns"}, inputs={"test_mode": 0},
    )
    pulse_vector, jtag_vector = _vectors(text)
    assert "test_mode=1;" in pulse_vector
    assert "test_mode=0;" in jtag_vector


def test_a_pulse_holds_the_pins_it_does_not_list_instead_of_driving_0():
    text = to_stil(
        [PulsePin("sysclk_a", 1, hold_pins=(("pi_x", 1),)), PulsePin("sysclk_b", 1)],
        pulse_periods={"sysclk_a": "20ns", "sysclk_b": "20ns"},
    )
    _, b_vector = _vectors(text)
    assert "pi_x=P;" in b_vector and "sysclk_a=P;" in b_vector
    assert "=0;" not in b_vector


@pytest.mark.parametrize(
    "inputs, outputs, ops, match",
    [
        ({"tck": 1}, (), [], "TAP pin"),
        ({}, ["tdo"], [], "TAP pin"),
        ({"rst_n": 2}, (), [], "0 or 1"),
        ({"rst_n": 1}, ["rst_n"], [], "both"),
        ({}, ["status_out"], [PulsePin("sysclk", 1, (("status_out", 1),))], "declared output"),
        ({}, (), [PulsePin("sysclk", 1, (("pi_a", 3),))], "0 or 1"),
        ({}, (), [SetPins((("trst_n", 0),))], "not one of to_stil's inputs"),
        ({"trst_n": 1}, (), [SetPins((("trst_n", 2),))], "0 or 1"),
    ],
)
def test_bad_pin_declarations_raise_named_error(inputs, outputs, ops, match):
    with pytest.raises(TapIrStilError, match=match):
        to_stil(ops, pulse_periods={"sysclk": "20ns"}, inputs=inputs, outputs=outputs)


def test_setpins_changes_a_declared_input_from_the_next_vector_on():
    ops = [
        Runtest(1), SetPins((("trst_n", 0),)), Runtest(1),
        PulsePin("sysclk", 1), SetPins((("trst_n", 1),)), Runtest(1),
    ]
    text = to_stil(ops, pulse_periods={"sysclk": "20ns"}, inputs={"trst_n": 1, "rst_n": 1})
    values = [v.split("trst_n=")[1][0] for v in _vectors(text)]
    assert values == ["1", "0", "0", "1"]
    assert all("rst_n=1;" in v for v in _vectors(text))


def test_per_bit_names_are_quoted_everywhere_and_sorted_by_bit():
    text = to_stil(
        [Runtest(1), PulsePin("clk[0]", 1)], pulse_periods={"clk[0]": "10ns"},
        inputs={f"addr[{i}]": i % 2 for i in (10, 2, 0)}, outputs=["dout[1]"],
    )
    assert '"addr[0]" In;\n  "addr[2]" In;\n  "addr[10]" In;' in text
    assert "'tck + tms + tdi + tdo + \"addr[0]\" + \"addr[2]\" + \"addr[10]\"" in text
    assert "\"addr[10]\" { 01 { '0ns' D/U; }}" in text
    assert 'WaveformTable "pulse_clk[0]_wft" {' in text and 'W "pulse_clk[0]_wft";' in text
    jtag_vector, pulse_vector = _vectors(text)
    assert '"addr[0]"=0; "addr[2]"=0; "addr[10]"=0;' in jtag_vector
    assert '"clk[0]"=1;' in pulse_vector and '"dout[1]"=X;' in pulse_vector


def test_a_name_with_a_double_quote_raises_named_error():
    with pytest.raises(TapIrStilError, match="can't be written in STIL"):
        to_stil([], inputs={'bad"name': 0})


def test_the_documented_reset_lead_in_asserts_trst_over_five_tms_1_cycles():
    lead_in = [
        SetPins((("trst_n", 0),)), GotoState(TapState.TEST_LOGIC_RESET),
        SetPins((("trst_n", 1),)), GotoState(TapState.RUN_TEST_IDLE),
    ]
    vectors = _vectors(to_stil(lead_in, inputs={"trst_n": 1}))
    assert [("tms=1;" in v, "trst_n=0;" in v) for v in vectors] == [(True, True)] * 5 + [(False, False)]
