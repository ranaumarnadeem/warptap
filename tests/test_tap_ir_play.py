"""The TMS reset ``navigation_tms`` adds to its narrow navigation: five TMS=1 cycles reach
Test-Logic-Reset from every one of the 16 states, and one TMS=0 cycle settles in Run-Test/Idle."""

from __future__ import annotations

import pytest

from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import GotoState, SetPins, ShiftIR
from warptap.tap_ir_play import TapIrPlayError, navigation_tms, play, to_cycles
from warptap.tap_model import OPCODE_EXTEST, Instruction, TapModel


@pytest.mark.parametrize("start", list(TapState))
def test_five_tms_1_reach_test_logic_reset_from_any_state(start):
    state = start
    for tms in navigation_tms(start, TapState.TEST_LOGIC_RESET):
        state = next_state(state, tms)
    assert state is TapState.TEST_LOGIC_RESET
    assert navigation_tms(start, TapState.TEST_LOGIC_RESET) == (1, 1, 1, 1, 1)


def test_one_tms_0_settles_from_test_logic_reset():
    assert navigation_tms(TapState.TEST_LOGIC_RESET, TapState.RUN_TEST_IDLE) == (0,)


def test_shift_states_still_need_run_test_idle_first():
    with pytest.raises(TapIrPlayError):
        navigation_tms(TapState.TEST_LOGIC_RESET, TapState.SHIFT_IR)


def test_a_tms_reset_reloads_idcode_on_the_model():
    model = TapModel()
    model.reset()
    model.tick(0, 0)
    ops = [
        GotoState(TapState.SHIFT_IR), ShiftIR(4, OPCODE_EXTEST), GotoState(TapState.RUN_TEST_IDLE),
        GotoState(TapState.TEST_LOGIC_RESET), GotoState(TapState.RUN_TEST_IDLE),
    ]
    play(model, ops)
    assert model.state is TapState.RUN_TEST_IDLE
    assert model.instruction is Instruction.IDCODE
    assert to_cycles(ops)[-6:] == [(1, 0)] * 5 + [(0, 0)]


def test_setpins_is_rejected():
    """play() and to_cycles() drive TMS and TDI only; SetPins is for to_stil."""
    with pytest.raises(TapIrPlayError, match="does not support"):
        to_cycles([SetPins((("trst_n", 0),))])
