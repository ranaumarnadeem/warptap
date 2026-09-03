"""Tests for PDLInterpreter's additive ``self.history`` layer (implementation_plan.md §7
Stage 11). Two concerns, kept separate: (1) history accumulates the right statements in call
order; (2) recording history changes nothing about ``self.program``'s own pre-existing
behavior -- an explicit regression lock against test_pdl_interpreter.py's own fixtures.
"""

from __future__ import annotations

import pytest

from warptap.pdl_history import (
    PdlApplyStmt,
    PdlReadStmt,
    PdlRunLoopStmt,
    PdlTargetStmt,
    PdlWriteStmt,
)
from warptap.pdl_interpreter import PDLError, PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]


def _interpreter() -> PDLInterpreter:
    graph, root = build_sib_plan(_SPECS)
    return PDLInterpreter(graph, root)


def test_history_starts_empty():
    pdl = _interpreter()
    assert pdl.history == []


def test_itarget_records_target_stmt():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    assert pdl.history == [PdlTargetStmt("sensor_a")]


def test_itarget_bad_path_does_not_record():
    from warptap.icl_model import ICLAddressError

    pdl = _interpreter()
    with pytest.raises(ICLAddressError):
        pdl.iTarget("ghost")
    assert pdl.history == []


def test_iwrite_records_write_stmt_with_scope_name_as_field():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iWrite(5)
    assert pdl.history[-1] == PdlWriteStmt("sensor_a", 5)


def test_iwrite_before_itarget_raises_and_does_not_record():
    pdl = _interpreter()
    with pytest.raises(PDLError):
        pdl.iWrite(1)
    assert pdl.history == []


def test_iread_records_read_stmt_with_scope_name_as_field():
    pdl = _interpreter()
    pdl.iTarget("sensor_b")
    pdl.iRead(0b11)
    assert pdl.history[-1] == PdlReadStmt("sensor_b", 0b11)


def test_iread_before_itarget_raises_and_does_not_record():
    pdl = _interpreter()
    with pytest.raises(PDLError):
        pdl.iRead(1)
    assert pdl.history == []


def test_irunloop_records_runloop_stmt_even_without_a_scope():
    pdl = _interpreter()
    pdl.iRunLoop(100)
    assert pdl.history == [PdlRunLoopStmt(100)]


def test_iapply_records_apply_stmt():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iApply()
    assert pdl.history[-1] == PdlApplyStmt()


def test_iapply_before_itarget_raises_and_does_not_record():
    pdl = _interpreter()
    with pytest.raises(PDLError):
        pdl.iApply()
    assert pdl.history == []


def test_history_accumulates_in_call_order_not_batched_like_program():
    """Unlike self.program (which only grows on iRunLoop/iApply), history grows on EVERY
    successful call, in the exact order those calls happened -- iWrite/iRead show up in
    history immediately, not deferred until the iApply that consumes them."""
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iWrite(5)
    pdl.iRunLoop(10)
    pdl.iApply()
    assert pdl.history == [
        PdlTargetStmt("sensor_a"),
        PdlWriteStmt("sensor_a", 5),
        PdlRunLoopStmt(10),
        PdlApplyStmt(),
    ]


def test_history_records_a_second_itarget_and_write_to_a_different_instrument():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iApply()
    pdl.iTarget("sensor_b")
    pdl.iWrite(0b10)
    pdl.iApply()
    assert pdl.history == [
        PdlTargetStmt("sensor_a"),
        PdlApplyStmt(),
        PdlTargetStmt("sensor_b"),
        PdlWriteStmt("sensor_b", 0b10),
        PdlApplyStmt(),
    ]


# --- Regression lock: self.program's pre-existing behavior is unchanged by history recording,
# mirroring the exact assertions test_pdl_interpreter.py already makes on the same fixtures.


def test_program_still_accumulates_across_multiple_commands_with_history_on():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iRunLoop(10)
    first_apply_ops = pdl.iApply()
    assert pdl.program[0].count == 10
    assert pdl.program[1:] == first_apply_ops
    # And history recorded all three calls, program only the two that ever touch it.
    assert len(pdl.history) == 3
    assert len(pdl.program) == 7  # 1 Runtest + 6 iApply ops


def test_pending_writes_and_reads_still_clear_after_iapply_with_history_on():
    pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iWrite(5)
    pdl.iRead(5)
    pdl.iApply()
    assert pdl._pending_writes == {}
    assert pdl._pending_reads == {}
