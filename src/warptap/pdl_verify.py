"""PDL functional verification (implementation_plan.md §7 Stage 9, §3.3's own promised
"replay a retargeted sequence, compare against the PDL-declared expected value").

Core insight needing no new "expected value" concept: ``PDLInterpreter.iApply()`` already
populates a ``ShiftDR``'s ``tdo``/``mask`` fields whenever a prior ``iRead()`` was queued
(``pdl_interpreter.py``'s own docstring: "not evaluated here") -- this module is that
consumer. Pure comparison, no simulation involved; :func:`correlate_observed` is the only
piece that touches a raw trace, and even that's just index bookkeeping via
``tap_ir_play.shift_op_ranges()``.

Deliberately does **not** predict what a real external signal's value *should* be --
that's the whole point of Stage 9's two-oracle split (see ``sib_insert.py``/
``sib_model.py``'s Stage 9 docs): scan-mechanics correctness is
``SibNetworkRegister``/``tap_ir_play.play()``'s job; functional content correctness is
checked here, against real RTL, with "expected" coming only from what the PDL author
declared via ``iRead``.
"""

from __future__ import annotations

from typing import List, NamedTuple, Union

from warptap.tap_ir import GotoState, Runtest, ShiftDR, ShiftIR, bits_to_int
from warptap.tap_ir_play import shift_op_ranges

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest]


class PDLVerifyError(RuntimeError):
    """Raised when ``observed_by_shift_op`` doesn't have enough entries to cover every
    shift op ``ir_ops`` actually contains -- a mismatched trace/ops pair, not a failed
    comparison (a failed comparison is a normal, non-exceptional ``ReadCheckResult``)."""


class ReadCheckResult(NamedTuple):
    shift_op_index: int  # index among ShiftIR/ShiftDR ops only, matching
                          # tap_ir_play.play()/shift_op_ranges()'s own indexing
    expected: int
    observed: int
    mask: int
    passed: bool


def check_reads(
    ir_ops: List[_IrOp], observed_by_shift_op: List[int]
) -> List[ReadCheckResult]:
    """Compare each shift op's PDL-declared expectation (``op.tdo``/``op.mask``, populated
    only when an ``iRead`` was queued for that ``iApply``) against the corresponding entry
    of ``observed_by_shift_op`` (one int per ``ShiftIR``/``ShiftDR`` op, e.g. from
    :func:`correlate_observed` or ``tap_ir_play.play()``'s own return value).

    A shift op with ``tdo is None`` (a bare write, no ``iRead`` queued) produces no result
    -- masked equality, matching ``PDLInterpreter``'s own ``_read_mask_bits`` convention: an
    ``iRead`` only ever asserts about its own instrument's content bits within a possibly-
    longer phase-2 shift, never the whole chain."""
    results: List[ReadCheckResult] = []
    index = 0
    for op in ir_ops:
        if not isinstance(op, (ShiftIR, ShiftDR)):
            continue
        if op.tdo is not None:
            if index >= len(observed_by_shift_op):
                raise PDLVerifyError(
                    f"observed_by_shift_op has {len(observed_by_shift_op)} entries, but "
                    f"shift op {index} (with a pending read) needs one"
                )
            observed = observed_by_shift_op[index]
            mask = op.mask if op.mask is not None else -1
            results.append(
                ReadCheckResult(
                    shift_op_index=index,
                    expected=op.tdo,
                    observed=observed,
                    mask=mask,
                    passed=(observed & mask) == (op.tdo & mask),
                )
            )
        index += 1
    return results


def correlate_observed(ir_ops: List[_IrOp], tdo_by_cycle: List[int]) -> List[int]:
    """Turn a raw per-cycle TDO trace (reset lead-in already stripped by the caller,
    matching ``tap_ir_play.to_cycles()``'s own precondition) into one int per
    ``ShiftIR``/``ShiftDR`` op, via ``tap_ir_play.shift_op_ranges()``."""
    ranges = shift_op_ranges(ir_ops)
    return [bits_to_int(tdo_by_cycle[start : start + bits]) for start, bits in ranges]
