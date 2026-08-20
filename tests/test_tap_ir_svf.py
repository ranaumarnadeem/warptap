"""Pure-Python tests for the SVF emitter (implementation_plan.md §7 Stage 7). No RTL/OpenOCD
involved here -- structural/textual assertions against the real SVF Specification Rev. E
grammar, mirroring this project's established style for a from-scratch format emitter.
"""

from __future__ import annotations

import pytest

from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, Runtest, ShiftDR, ShiftIR
from warptap.tap_ir_svf import TapIrSvfError, to_svf


def test_header_is_always_emitted():
    svf = to_svf([])
    assert svf == "ENDIR IDLE;\nENDDR IDLE;\n"


def test_gotostate_ops_are_dropped():
    ops = [GotoState(TapState.SHIFT_DR), GotoState(TapState.RUN_TEST_IDLE)]
    svf = to_svf(ops)
    assert svf == "ENDIR IDLE;\nENDDR IDLE;\n"


def test_sdr_emits_tdi_only_when_tdo_mask_absent():
    ops = [ShiftDR(bits=8, tdi=0x41)]
    svf = to_svf(ops)
    assert "SDR 8 TDI (41);" in svf
    assert "TDO" not in svf
    assert "MASK" not in svf


def test_sdr_emits_tdo_and_mask_when_present():
    ops = [ShiftDR(bits=32, tdi=0xABCD1234, tdo=0x11112222, mask=0xFFFFFFFF)]
    svf = to_svf(ops)
    assert "SDR 32 TDI (ABCD1234) TDO (11112222) MASK (FFFFFFFF);" in svf


def test_sir_uses_sir_command():
    ops = [ShiftIR(bits=8, tdi=0x41)]
    svf = to_svf(ops)
    assert "SIR 8 TDI (41);" in svf
    assert "SDR" not in svf


def test_hex_is_zero_padded_to_full_width():
    ops = [ShiftDR(bits=16, tdi=0x1)]
    svf = to_svf(ops)
    assert "TDI (0001);" in svf


def test_hex_width_rounds_up_to_nearest_nibble():
    ops = [ShiftDR(bits=9, tdi=0b101)]  # 9 bits -> ceil(9/4) = 3 hex digits
    svf = to_svf(ops)
    assert "SDR 9 TDI (005);" in svf


def test_runtest_renders_state_names_and_count():
    ops = [Runtest(100, run_state=TapState.RUN_TEST_IDLE, end_state=TapState.PAUSE_IR)]
    svf = to_svf(ops)
    assert "RUNTEST IDLE 100 TCK ENDSTATE IRPAUSE;" in svf


def test_full_directed_scenario_matches_realistic_shape():
    """Mirrors the shape of a real PDLInterpreter.iApply()-style op sequence: select
    EXTEST via ShiftIR, then a GotoState/ShiftDR/GotoState phase -- confirms the whole
    pipeline (drop GotoState, keep scans, keep header) composes correctly end to end."""
    ops = [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(bits=4, tdi=0),
        GotoState(TapState.RUN_TEST_IDLE),
        GotoState(TapState.SHIFT_DR),
        ShiftDR(bits=5, tdi=0b10101, tdo=0b10101, mask=0b11111),
        GotoState(TapState.RUN_TEST_IDLE),
    ]
    svf = to_svf(ops)
    lines = svf.splitlines()
    assert lines == [
        "ENDIR IDLE;",
        "ENDDR IDLE;",
        "SIR 4 TDI (0);",
        "SDR 5 TDI (15) TDO (15) MASK (1F);",
    ]


def test_unsupported_op_raises_named_error():
    with pytest.raises(TapIrSvfError, match="does not support"):
        to_svf(["not an op"])


def test_svf_state_names_match_spec_table_for_every_state():
    """Regression-locks the SVF Specification Rev. E p.6 state-name table (verbatim,
    quoted under the spec's own no-cost-copying grant) for every one of the 16 states --
    not just the 4 stable ones this module's own RUNTEST/header code path exercises."""
    from warptap.tap_ir import TAP_STATE_NAMES

    expected = {
        TapState.TEST_LOGIC_RESET: "RESET",
        TapState.RUN_TEST_IDLE: "IDLE",
        TapState.SELECT_DR_SCAN: "DRSELECT",
        TapState.CAPTURE_DR: "DRCAPTURE",
        TapState.SHIFT_DR: "DRSHIFT",
        TapState.EXIT1_DR: "DREXIT1",
        TapState.PAUSE_DR: "DRPAUSE",
        TapState.EXIT2_DR: "DREXIT2",
        TapState.UPDATE_DR: "DRUPDATE",
        TapState.SELECT_IR_SCAN: "IRSELECT",
        TapState.CAPTURE_IR: "IRCAPTURE",
        TapState.SHIFT_IR: "IRSHIFT",
        TapState.EXIT1_IR: "IREXIT1",
        TapState.PAUSE_IR: "IRPAUSE",
        TapState.EXIT2_IR: "IREXIT2",
        TapState.UPDATE_IR: "IRUPDATE",
    }
    assert TAP_STATE_NAMES == expected
