"""warptap.wsp_ir / warptap.wsp_play: scan operations on an IEEE 1500 wrapper serial port,
lowered to WSP cycles and played against the model.

A ShiftIR is a WIR scan (SelectWIR high, ShiftWR high for every bit, then an update); a
ShiftDR is a data-register scan (a capture, the shifts, an update). The same operations
the TAP backends take, so the PDL layer above them is shared."""

from __future__ import annotations

import pytest

from warptap.icl_emit_1500 import icl_literal
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, Runtest, ShiftDR, ShiftIR
from warptap.wsp1500_model import (
    WbrCell,
    Wrapper1500,
    Wsp1500Chain,
    WspCycle,
    program_cycles,
)
from warptap.wsp_ir import WspReset
from warptap.wsp_play import WspPlayError, mismatches, play, to_wsp_rows

BLOCK = Wrapper1500(
    name="blk",
    chains=(
        (WbrCell("a", "input"), WbrCell("b", "input")),
        (WbrCell("y", "output"),),
    ),
)


def _value(code: int) -> int:
    """The value ICL gives a code: its bits reversed (icl_emit_1500)."""
    return int(icl_literal(code, 3).split("'b")[1], 2)


def test_a_wir_scan_shifts_the_codes_bits_last_bit_first_like_the_model_does() -> None:
    """iWrite WIR WS_EXTEST: the ICL value 3'b100 as a chronological tdi is the very
    cycles the model's own load program has -- the first bit shifted in is the code's
    last."""
    rows = to_wsp_rows([WspReset(), ShiftIR(bits=3, tdi=_value(0b001))])
    mine = [row.cycle for row in rows]
    theirs = program_cycles(BLOCK, "WS_EXTEST")[:-1]  # without its trailing idle cycle
    assert mine == theirs


@pytest.mark.parametrize("name", ["WS_BYPASS", "WS_EXTEST", "WP_INTEST", "WP_EXTEST", "WS_INTEST"])
def test_every_instruction_loaded_through_a_wir_scan_is_in_force(name: str) -> None:
    code = BLOCK.instruction(name).code
    rows = to_wsp_rows([WspReset(), ShiftIR(3, _value(code))])
    chain = Wsp1500Chain([BLOCK])
    play(chain, rows)
    assert chain.sims[0].instruction == name


def test_a_wir_scan_ends_with_an_update_and_has_no_capture() -> None:
    rows = to_wsp_rows([ShiftIR(3, 0b101)])
    assert [(r.cycle.selectwir, r.cycle.shiftwr, r.cycle.capturewr, r.cycle.updatewr) for r in rows] == [
        (1, 1, 0, 0),
        (1, 1, 0, 0),
        (1, 1, 0, 0),
        (1, 0, 0, 1),
    ]
    assert [r.cycle.wsi for r in rows[:3]] == [1, 0, 1]  # tdi bit 0 first


def test_a_dr_scan_captures_shifts_and_updates_with_the_wir_deselected() -> None:
    rows = to_wsp_rows([ShiftDR(2, 0b10)])
    flags = [
        (r.cycle.selectwir, r.cycle.shiftwr, r.cycle.capturewr, r.cycle.updatewr)
        for r in rows
    ]
    assert flags == [(0, 0, 1, 0), (0, 1, 0, 0), (0, 1, 0, 0), (0, 0, 0, 1)]
    assert [r.cycle.wsi for r in rows[1:3]] == [0, 1]


def test_expected_tdo_becomes_the_wso_each_shift_should_show_where_masked_in() -> None:
    rows = to_wsp_rows([ShiftDR(4, 0, tdo=0b0110, mask=0b1110)])
    shifts = [r for r in rows if r.cycle.shiftwr]
    assert [r.wso for r in shifts] == [None, 1, 1, 0]
    unmasked = to_wsp_rows([ShiftDR(2, 0, tdo=0b01)])
    assert [r.wso for r in unmasked if r.cycle.shiftwr] == [1, 0]
    assert all(r.wso is None for r in to_wsp_rows([ShiftDR(2, 0)]))


def test_a_bypass_scan_reads_back_the_bit_two_chains_deep() -> None:
    """The bypass register of each of two wrappers: what is fed in comes out two
    shifts later; the scan's own tdo is checked against the model."""
    chain = Wsp1500Chain([BLOCK, BLOCK])
    ops = [WspReset(), ShiftDR(6, tdi=0b110101, tdo=0b010100, mask=0b111100)]
    rows = to_wsp_rows(ops)
    observed = play(chain, rows)
    assert mismatches(rows, observed) == []
    wrong = to_wsp_rows([WspReset(), ShiftDR(6, tdi=0b110101, tdo=0b110100, mask=0b111100)])
    assert mismatches(wrong, play(Wsp1500Chain([BLOCK, BLOCK]), wrong)) != []


def test_a_wir_scan_of_a_chain_loads_the_wrapper_nearest_wso_first() -> None:
    chain = Wsp1500Chain([BLOCK, BLOCK])
    far, near = _value(0b001), _value(0b010)  # WS_EXTEST at the far end, WP_INTEST near
    play(chain, to_wsp_rows([WspReset(), ShiftIR(6, tdi=far | (near << 3))]))
    assert [s.instruction for s in chain.sims] == ["WP_INTEST", "WS_EXTEST"]


def test_ws_extest_captures_the_ports_the_capture_row_drives() -> None:
    chain = Wsp1500Chain([BLOCK])
    play(chain, to_wsp_rows([WspReset(), ShiftIR(3, _value(0b001))]))
    rows = to_wsp_rows([ShiftDR(3, 0)], cfi=(1, 0, 1))
    observed = play(chain, rows)
    # y (unknown), then b = 0, then a = 1.
    assert [o for r, o in zip(rows, observed) if r.cycle.shiftwr] == [None, 0, 1]
    expected = to_wsp_rows([ShiftDR(3, 0, tdo=0b100, mask=0b110)], cfi=(1, 0, 1))
    assert mismatches(expected, observed) == []
    wrong = to_wsp_rows([ShiftDR(3, 0, tdo=0b010, mask=0b110)], cfi=(1, 0, 1))
    assert mismatches(wrong, observed) != []


def test_idle_cycles_pulse_wrck_and_a_tap_state_change_is_nothing() -> None:
    rows = to_wsp_rows([Runtest(3), GotoState(TapState.RUN_TEST_IDLE)])
    assert [r.cycle for r in rows] == [WspCycle()] * 3


def test_an_op_a_wsp_has_no_use_for_is_refused() -> None:
    with pytest.raises(WspPlayError, match="PulsePin"):
        from warptap.tap_ir import PulsePin

        to_wsp_rows([PulsePin("clk", 3)])
