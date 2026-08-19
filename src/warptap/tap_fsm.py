"""The canonical IEEE 1149.1 TAP state-transition table (implementation_plan.md §7 Stage 2).

This is the single source of truth two other artifacts are checked against: the hand-authored
`rtl/tap_core.v` (via tests/test_tap_fsm_cross_sim.py's behavioral simulation) and this same
table re-derived from independent references (tests/test_tap_fsm_table.py's structural check).
Deliberately has no knowledge of instructions/opcodes — the real 16-state FSM is driven by TMS
alone, identically for every IEEE-1149.1-conformant TAP regardless of IR width or instruction
set, so keeping that fact out of this file is what makes it the right thing to cross-check.
"""

from __future__ import annotations

import enum


class TapState(enum.IntEnum):
    """The 16 TAP controller states. Values match the state encoding used in
    rtl/tap_core.v's `localparam`s exactly, so a cross-simulation trace comparison
    (tests/test_tap_fsm_cross_sim.py) can diff `TapState(rtl_state_bits)` against
    `TapModel.state` with no translation table in between."""

    TEST_LOGIC_RESET = 0
    RUN_TEST_IDLE = 1
    SELECT_DR_SCAN = 2
    CAPTURE_DR = 3
    SHIFT_DR = 4
    EXIT1_DR = 5
    PAUSE_DR = 6
    EXIT2_DR = 7
    UPDATE_DR = 8
    SELECT_IR_SCAN = 9
    CAPTURE_IR = 10
    SHIFT_IR = 11
    EXIT1_IR = 12
    PAUSE_IR = 13
    EXIT2_IR = 14
    UPDATE_IR = 15


# state -> (next state if TMS=0, next state if TMS=1). The two entries most prone to an
# off-by-one transcription error are EXIT2_DR/EXIT2_IR with TMS=0: they return to
# SHIFT_DR/SHIFT_IR, not back to CAPTURE_DR/CAPTURE_IR (implementation_plan.md §7 names this
# exact bug class explicitly).
TRANSITIONS: dict[TapState, tuple[TapState, TapState]] = {
    TapState.TEST_LOGIC_RESET: (TapState.RUN_TEST_IDLE, TapState.TEST_LOGIC_RESET),
    TapState.RUN_TEST_IDLE: (TapState.RUN_TEST_IDLE, TapState.SELECT_DR_SCAN),
    TapState.SELECT_DR_SCAN: (TapState.CAPTURE_DR, TapState.SELECT_IR_SCAN),
    TapState.CAPTURE_DR: (TapState.SHIFT_DR, TapState.EXIT1_DR),
    TapState.SHIFT_DR: (TapState.SHIFT_DR, TapState.EXIT1_DR),
    TapState.EXIT1_DR: (TapState.PAUSE_DR, TapState.UPDATE_DR),
    TapState.PAUSE_DR: (TapState.PAUSE_DR, TapState.EXIT2_DR),
    TapState.EXIT2_DR: (TapState.SHIFT_DR, TapState.UPDATE_DR),
    TapState.UPDATE_DR: (TapState.RUN_TEST_IDLE, TapState.SELECT_DR_SCAN),
    TapState.SELECT_IR_SCAN: (TapState.CAPTURE_IR, TapState.TEST_LOGIC_RESET),
    TapState.CAPTURE_IR: (TapState.SHIFT_IR, TapState.EXIT1_IR),
    TapState.SHIFT_IR: (TapState.SHIFT_IR, TapState.EXIT1_IR),
    TapState.EXIT1_IR: (TapState.PAUSE_IR, TapState.UPDATE_IR),
    TapState.PAUSE_IR: (TapState.PAUSE_IR, TapState.EXIT2_IR),
    TapState.EXIT2_IR: (TapState.SHIFT_IR, TapState.UPDATE_IR),
    TapState.UPDATE_IR: (TapState.RUN_TEST_IDLE, TapState.SELECT_DR_SCAN),
}


def next_state(state: TapState, tms: int) -> TapState:
    """Pure combinational lookup: the next TAP state given the current state and the
    value of TMS sampled on this TCK rising edge."""
    return TRANSITIONS[state][tms & 1]
