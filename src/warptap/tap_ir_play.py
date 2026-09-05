"""Lowering backends for warptap.tap_ir ops (implementation_plan.md §7 Stage 5).

Deliberately narrow: only implements the ONE navigation pattern
``pdl_interpreter.PDLInterpreter.iApply`` actually emits (Run-Test-Idle -> Select-x-Scan ->
Capture-x -> Shift-x -> Exit1-x -> Update-x -> Run-Test-Idle), not a general BFS pathfinder
over the 16-state FSM -- a future Stage 7 backend (SVF/STAPL text emission) walks the identical
``ir_ops`` list with a different lowering. Both backends here share that one piece of
navigation-step logic (:func:`navigation_tms`) rather than duplicating it:

- :func:`play` drives a live :class:`~warptap.tap_model.TapModel` (+ whatever
  :class:`~warptap.tap_model.DataRegister` it has registered, e.g.
  :class:`~warptap.sib_model.SibNetworkRegister`), returning the observed TDO stream --
  used for Python-side cross-sim assertions.
- :func:`to_cycles` computes the identical raw per-TCK-cycle ``(tms, tdi)`` sequence with
  no model or registered DataRegister involved at all (pure ``tap_fsm.next_state`` state
  tracking) -- used to build real-RTL testbench stimulus, mirroring
  ``tests/test_sib_insert_cross_sim.py``'s own hand-built stimulus shape but generated from
  the same ops :func:`play` consumes, rather than a second, independently-hand-written
  sequence that could silently drift from it.

Both preconditions: the caller must already have the TAP resident in ``RUN_TEST_IDLE`` before
the first op (a reset + settle lead-in, same discipline every prior stage's stimulus already
follows) -- neither function drives a reset itself.

``navigation_tms``/``shift_tms`` are public (Stage 13): :mod:`warptap.tap_ir_stil` became a
second real consumer of this exact per-cycle navigation logic, needing its own walker since
STIL vectors are self-contained send+expect (unlike this module's own deliberately send-only
``to_cycles``) -- the same "rule of three" threshold this project applies elsewhere (e.g.
:mod:`warptap.tap_ports`) rather than a second, hand-copied implementation.
"""

from __future__ import annotations

from typing import List, Tuple, Union

from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import GotoState, Runtest, ShiftDR, ShiftIR, bits_from_int, bits_to_int
from warptap.tap_model import TapModel

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest]

_GOTO_SHIFT_DR_TMS = (1, 0, 0)  # RUN_TEST_IDLE -> SELECT_DR_SCAN -> CAPTURE_DR -> SHIFT_DR
_GOTO_SHIFT_IR_TMS = (1, 1, 0, 0)  # RUN_TEST_IDLE -> ... -> SELECT_IR_SCAN -> CAPTURE_IR -> SHIFT_IR
_EXIT_TO_IDLE_TMS = (1, 0)  # EXIT1_x -> UPDATE_x -> RUN_TEST_IDLE


class TapIrPlayError(RuntimeError):
    """Raised when a navigation target/source pair isn't one of the transitions this
    deliberately-narrow module implements (see module docstring)."""


def navigation_tms(from_state: TapState, to_state: TapState) -> Tuple[int, ...]:
    """The tms sequence (tdi held at 0 throughout) needed to walk from ``from_state`` to
    ``to_state`` -- only the specific transitions ``PDLInterpreter.iApply`` actually emits."""
    if to_state is TapState.SHIFT_DR:
        if from_state is not TapState.RUN_TEST_IDLE:
            raise TapIrPlayError(f"cannot navigate to SHIFT_DR from {from_state}")
        return _GOTO_SHIFT_DR_TMS
    if to_state is TapState.SHIFT_IR:
        if from_state is not TapState.RUN_TEST_IDLE:
            raise TapIrPlayError(f"cannot navigate to SHIFT_IR from {from_state}")
        return _GOTO_SHIFT_IR_TMS
    if to_state is TapState.RUN_TEST_IDLE:
        if from_state is TapState.RUN_TEST_IDLE:
            return ()
        if from_state in (TapState.EXIT1_DR, TapState.EXIT1_IR):
            return _EXIT_TO_IDLE_TMS
        raise TapIrPlayError(f"cannot navigate to RUN_TEST_IDLE from {from_state}")
    raise TapIrPlayError(f"navigating to {to_state} is not supported")


def shift_tms(bits: int) -> Tuple[int, ...]:
    """tms for each of ``bits`` shift cycles -- 0 throughout except the last, which rides
    the exit edge (every prior stage's own directed tests rely on this same convention:
    the last bit shifted and the state-exit transition share one TCK edge)."""
    return tuple(0 for _ in range(bits - 1)) + (1,)


def play(model: TapModel, ir_ops: List[_IrOp]) -> List[int]:
    """Drive ``ir_ops`` against ``model``, returning one observed-TDO int per
    :class:`~warptap.tap_ir.ShiftIR`/:class:`~warptap.tap_ir.ShiftDR` op (chronological/
    LSB-first, matching ``op.tdi``'s own convention). Does not itself compare against
    ``op.tdo``/``op.mask`` -- that's the caller's job (mirrors this project's "no fault
    model involved, this is warptap checking its own output" framing elsewhere)."""
    observed: List[int] = []
    for op in ir_ops:
        if isinstance(op, GotoState):
            for tms in navigation_tms(model.state, op.state):
                model.tick(tms, 0)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            fed_bits = bits_from_int(op.tdi, op.bits)
            out = [model.tick(tms, bit) for tms, bit in zip(shift_tms(op.bits), fed_bits)]
            observed.append(bits_to_int(out))
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                model.tick(0, 0)
        else:
            raise TapIrPlayError(f"play() does not support op {op!r}")
    return observed


def to_cycles(ir_ops: List[_IrOp]) -> List[Tuple[int, int]]:
    """Flatten ``ir_ops`` into the full per-TCK-cycle ``(tms, tdi)`` sequence needed to
    realize them -- pure ``tap_fsm.next_state`` state tracking, no ``TapModel``/
    ``DataRegister`` involved (so it works regardless of which instruction is active or
    what's registered for it -- this function only decides what to *send*, never what
    comes back). Feeds real RTL testbenches the identical stimulus :func:`play` would
    drive against a Python model, generated from the same ``ir_ops`` rather than a second,
    independently-hand-written sequence."""
    state = TapState.RUN_TEST_IDLE
    cycles: List[Tuple[int, int]] = []

    def tick(tms: int, tdi: int) -> None:
        nonlocal state
        cycles.append((tms, tdi))
        state = next_state(state, tms)

    for op in ir_ops:
        if isinstance(op, GotoState):
            for tms in navigation_tms(state, op.state):
                tick(tms, 0)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            fed_bits = bits_from_int(op.tdi, op.bits)
            for tms, bit in zip(shift_tms(op.bits), fed_bits):
                tick(tms, bit)
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                tick(0, 0)
        else:
            raise TapIrPlayError(f"to_cycles() does not support op {op!r}")
    return cycles


def shift_op_ranges(ir_ops: List[_IrOp]) -> List[Tuple[int, int]]:
    """``(start_cycle_index, bits)`` for each :class:`~warptap.tap_ir.ShiftIR`/
    :class:`~warptap.tap_ir.ShiftDR` op in ``ir_ops``, in the same per-cycle numbering
    :func:`to_cycles` produces. Lets a caller correlate a raw per-cycle trace (e.g. observed
    TDO from real RTL, one value per stimulus row) back to the same per-op granularity
    :func:`play` returns, so a RTL-driven run and a `play()`-driven run can be compared at
    the same abstraction level."""
    state = TapState.RUN_TEST_IDLE
    index = 0
    ranges: List[Tuple[int, int]] = []
    for op in ir_ops:
        if isinstance(op, GotoState):
            tms_seq = navigation_tms(state, op.state)
            for tms in tms_seq:
                state = next_state(state, tms)
            index += len(tms_seq)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            ranges.append((index, op.bits))
            for tms in shift_tms(op.bits):
                state = next_state(state, tms)
            index += op.bits
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                state = next_state(state, 0)
            index += op.count
        else:
            raise TapIrPlayError(f"shift_op_ranges() does not support op {op!r}")
    return ranges
