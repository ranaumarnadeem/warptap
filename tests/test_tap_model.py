"""Pure-Python behavioral tests for warptap.tap_model.TapModel (implementation_plan.md §7
Stage 2): IR capture/shift/update correctness, BYPASS/IDCODE data registers end to end, the
default-on-reset rule, and the Stage-3 extension boundary for SAMPLE_PRELOAD/EXTEST.
"""

from __future__ import annotations

import pytest

from warptap.tap_fsm import TapState, next_state
from warptap.tap_model import (
    CAPTURE_IR_PATTERN,
    IDCODE_VALUE,
    Instruction,
    OPCODE_EXTEST,
    OPCODE_IDCODE,
    OPCODE_SAMPLE_PRELOAD,
    TapModel,
    TapModelError,
    bypass_opcode,
    decode_instruction,
)


def _reset_and_idle(model: TapModel) -> None:
    """Five TMS=1 cycles guarantee TEST_LOGIC_RESET from anywhere (the standard's own
    async-reset-equivalent property, locked in below by
    test_five_consecutive_tms1_cycles_reach_reset_from_any_state); one more TMS=0 cycle
    reaches RUN_TEST_IDLE, the common starting point every other helper below builds on."""
    for _ in range(5):
        model.tick(tms=1)
    model.tick(tms=0)
    assert model.state is TapState.RUN_TEST_IDLE


def _shift_ir_to_exit1(model: TapModel, opcode: int) -> None:
    """From RUN_TEST_IDLE, shift `opcode` into the instruction register and stop at
    EXIT1_IR — deliberately *before* Update-IR, so a test can observe that
    `model.instruction` hasn't changed yet.

    Real JTAG hardware shifts on *every* cycle resident in Shift-IR, including the
    cycle that exits via TMS=1 — the last bit and the exit transition happen on the
    same edge. So only `ir_width - 1` bits are fed while looping with TMS=0; the final
    bit rides the TMS=1 tick that actually leaves Shift-IR."""
    model.tick(tms=1)  # RUN_TEST_IDLE -> SELECT_DR_SCAN
    model.tick(tms=1)  # -> SELECT_IR_SCAN
    model.tick(tms=0)  # -> CAPTURE_IR
    model.tick(tms=0)  # -> SHIFT_IR (this tick's old_state=CAPTURE_IR loads the pattern)
    width = model.ir_width
    for i in range(width - 1):  # feed opcode LSB-first while remaining in SHIFT_IR
        model.tick(tms=0, tdi=(opcode >> i) & 1)
    model.tick(tms=1, tdi=(opcode >> (width - 1)) & 1)  # last bit + exit, same edge


def _shift_ir(model: TapModel, opcode: int) -> None:
    """Full IR shift, committed: instruction is updated and the FSM ends in
    RUN_TEST_IDLE."""
    _shift_ir_to_exit1(model, opcode)
    model.tick(tms=1)  # old_state=EXIT1_IR -> UPDATE_IR (arrival; no decode yet)
    model.tick(tms=0)  # old_state=UPDATE_IR -> decode fires -> RUN_TEST_IDLE


def _goto_shift_dr(model: TapModel) -> None:
    """From RUN_TEST_IDLE to SHIFT_DR, performing Capture-DR on the way — the shared
    setup every DR end-to-end test builds on."""
    model.tick(tms=1)  # RUN_TEST_IDLE -> SELECT_DR_SCAN
    model.tick(tms=0)  # -> CAPTURE_DR
    model.tick(tms=0)  # old_state=CAPTURE_DR -> capture() fires -> SHIFT_DR


def test_capture_ir_pattern_lsbs_are_01():
    assert CAPTURE_IR_PATTERN & 0b11 == 0b01


def test_capture_ir_pattern_equals_idcode_opcode():
    """Deliberate design choice (implementation_plan.md §7 Stage 2): if a shift
    sequence goes Capture-IR -> Update-IR without visiting Shift-IR, the captured
    pattern becomes the new instruction, and this way that edge case lands on IDCODE —
    the same safe default the reset rule already requires."""
    assert CAPTURE_IR_PATTERN == OPCODE_IDCODE


@pytest.mark.parametrize("ir_width", [2, 4, 8])
def test_bypass_opcode_is_all_ones_for_various_widths(ir_width):
    assert bypass_opcode(ir_width) == (1 << ir_width) - 1


def test_default_on_reset_selects_idcode_when_available():
    model = TapModel(has_idcode=True)
    assert model.instruction is Instruction.IDCODE
    model.instruction = Instruction.BYPASS  # perturb
    model.reset()
    assert model.instruction is Instruction.IDCODE


def test_default_on_reset_selects_bypass_when_idcode_not_configured():
    model = TapModel(has_idcode=False)
    assert model.instruction is Instruction.BYPASS
    model.reset()
    assert model.instruction is Instruction.BYPASS


def test_reserved_opcode_decodes_to_bypass():
    reserved = 0b0011  # not EXTEST/SAMPLE_PRELOAD/IDCODE/BYPASS(all-1s) for width 4
    assert decode_instruction(reserved, ir_width=4, has_idcode=True) is Instruction.BYPASS


def test_idcode_opcode_decodes_to_bypass_when_idcode_disabled():
    assert (
        decode_instruction(OPCODE_IDCODE, ir_width=4, has_idcode=False)
        is Instruction.BYPASS
    )


def test_instruction_does_not_change_before_departing_update_ir():
    """The precise off-by-one-prone semantics this stage is built around: the decode
    action fires on the tick that *departs* UPDATE_IR (old_state == UPDATE_IR), not on
    arrival at UPDATE_IR and not anywhere during the shift itself."""
    model = TapModel(has_idcode=True)  # starts at Instruction.IDCODE
    _reset_and_idle(model)

    _shift_ir_to_exit1(model, OPCODE_EXTEST)
    assert model.instruction is Instruction.IDCODE  # unchanged mid-shift

    model.tick(tms=1)  # old_state=EXIT1_IR -> arrives at UPDATE_IR
    assert model.state is TapState.UPDATE_IR
    assert model.instruction is Instruction.IDCODE  # still unchanged on arrival

    model.tick(tms=0)  # old_state=UPDATE_IR -> decode fires while departing
    assert model.instruction is Instruction.EXTEST


def test_shifting_sample_preload_opcode_selects_it():
    model = TapModel(has_idcode=True)
    _reset_and_idle(model)
    _shift_ir(model, OPCODE_SAMPLE_PRELOAD)
    assert model.instruction is Instruction.SAMPLE_PRELOAD


def test_bypass_register_captures_zero_and_shifts_with_one_cycle_latency():
    model = TapModel(has_idcode=False)  # defaults to BYPASS
    _reset_and_idle(model)
    _goto_shift_dr(model)

    fed = [1, 0, 1, 1, 0]
    observed = [model.tick(tms=0, tdi=bit) for bit in fed]

    assert observed[0] == 0  # BYPASS's mandated fixed capture value
    assert observed[1:] == fed[:-1]  # one cycle of shift latency thereafter


def test_idcode_register_shifts_out_configured_value_lsb_first():
    value = 0x1A5A5003
    model = TapModel(has_idcode=True, idcode_value=value)
    _reset_and_idle(model)
    _goto_shift_dr(model)

    bits = [model.tick(tms=0, tdi=0) for _ in range(32)]
    reconstructed = sum(bit << i for i, bit in enumerate(bits))

    assert reconstructed == value
    assert value & 1 == 1  # IEEE 1149.1 mandates IDCODE's LSB be fixed at 1


def test_shifting_through_extest_without_a_registered_dr_raises():
    model = TapModel(has_idcode=True)
    _reset_and_idle(model)
    _shift_ir(model, OPCODE_EXTEST)
    assert model.instruction is Instruction.EXTEST

    with pytest.raises(TapModelError, match="Stage 3"):
        _goto_shift_dr(model)


def test_registering_a_data_register_unblocks_shifting():
    """The Stage 3+ extension point: once a real register is plugged in, the boundary
    goes away without touching TapModel's FSM/IR internals."""

    class _StubRegister:
        def __init__(self):
            self.captured = False

        def capture(self):
            self.captured = True

        def shift(self, tdi):
            return 0

        def update(self):
            pass

    model = TapModel(has_idcode=True)
    stub = _StubRegister()
    model.register_data_register(Instruction.EXTEST, stub)
    _reset_and_idle(model)
    _shift_ir(model, OPCODE_EXTEST)

    _goto_shift_dr(model)  # no longer raises
    assert stub.captured


def test_five_consecutive_tms1_cycles_reach_reset_from_any_state():
    """Cheap, high-value robustness check directly reusing the FSM table: from every
    one of the 16 states, five consecutive TMS=1 cycles land in TEST_LOGIC_RESET.
    Targets the same off-by-one edge-transition bug class as the EXIT2_DR/IR check in
    test_tap_fsm_table.py, but exercises the whole table's connectivity at once."""
    for start in TapState:
        state = start
        for _ in range(5):
            state = next_state(state, 1)
        assert state is TapState.TEST_LOGIC_RESET, f"starting from {start.name}"
