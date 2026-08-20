"""SVF (Serial Vector Format) text emitter (implementation_plan.md §3.4, §6, §7 Stage 7).
Walks the same :mod:`warptap.tap_ir` ops list :func:`warptap.tap_ir_play.play`/``to_cycles``
already walk, producing real SVF text -- the second of §3.4's "stateless pretty-printers over
one IR." Grounded directly in the *SVF Specification, Revision E* (ASSET InterTech / Texas
Instruments, 8 March 1999) -- freely copyable/quotable per its own copyright notice -- and
cross-checked against OpenOCD's real ``svf.c`` parser/executor source.

**Bit-encoding**: SVF's own bit-order rule ("the least significant bit... is the first bit
scanned into the hardware for TDI... and is the first bit scanned out for TDO", spec p.5) is
*identical* to ``tap_ir.py``'s own chronological/LSB-first int convention -- confirmed by
direct comparison against the spec's own worked example (``SIR 8 TDI (41);`` shifts LSB-first,
cycle 1 = bit 0 of ``0x41``, exactly matching ``bits_from_int(0x41, 8)``). No bit reversal is
needed anywhere in this module -- ``op.tdi``/``op.tdo``/``op.mask`` format directly as hex.

**``GotoState`` ops are dropped, not translated.** SVF's own ``SIR``/``SDR`` statements
implicitly handle the *entire* "navigate from the current stable state into Shift-IR/DR, shift,
return to whatever ``ENDIR``/``ENDDR`` currently specifies" sequence -- confirmed directly by
the spec's own worked example, which never precedes a scan with an explicit ``STATE`` command.
Since every ``GotoState`` this project's own ops ever emit only ever brackets a shift (matching
``tap_ir_play.py``'s own narrow navigation model), an explicit ``STATE`` translation would be
redundant, not merely an optimization -- this module emits a single ``ENDIR IDLE;``/
``ENDDR IDLE;`` header instead (SVF's own default, per spec p.8, but stated explicitly here to
match this stage's "always emit every field, no sticky-default optimization yet" discipline),
which is exactly what every ``GotoState(RUN_TEST_IDLE)`` in this project's own ops already
wants.

**Deliberately emits every ``TDI``/``TDO``/``MASK`` field on every ``SIR``/``SDR``** -- SVF's
own field "stickiness" (reusing the last value for an omitted field) is a real, legal
compression this module doesn't use, matching §7 Stage 7's explicit instruction.

**``HDR``/``HIR``/``TDR``/``TIR`` (multi-device chain header/trailer padding) are omitted
entirely** -- confirmed safe by both the spec's own semantics ("A header can be removed by
setting length to 0") and OpenOCD's own ``svf_para_init``, which statically zeroes them before
any file is even read. ``TRST`` is likewise never emitted -- warptap's own ``tap_ir`` ops have
no TRST-equivalent op type, matching ``tap_ir_play.play()``/``to_cycles()``'s own "neither
function drives a reset itself" precedent (Stage 5).
"""

from __future__ import annotations

from typing import List, Union

from warptap.tap_ir import TAP_STATE_NAMES, GotoState, Runtest, ShiftDR, ShiftIR

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest]


class TapIrSvfError(RuntimeError):
    """Raised when ``to_svf`` is given an op it can't render."""


def _hex(value: int, bits: int) -> str:
    """Zero-pad ``value`` to the full ``ceil(bits / 4)``-hex-digit width -- explicit,
    matching this module's "always emit every field" discipline (SVF also accepts
    under-padded hex, spec p.11/19, but full-width is the least surprising choice)."""
    digits = (bits + 3) // 4
    return f"{value:0{digits}X}"


def _scan_line(command: str, op: Union[ShiftIR, ShiftDR]) -> str:
    parts = [command, str(op.bits), f"TDI ({_hex(op.tdi, op.bits)})"]
    if op.tdo is not None:
        parts.append(f"TDO ({_hex(op.tdo, op.bits)})")
    if op.mask is not None:
        parts.append(f"MASK ({_hex(op.mask, op.bits)})")
    return " ".join(parts) + ";"


def to_svf(ir_ops: List[_IrOp]) -> str:
    """Render ``ir_ops`` as SVF text, one statement per line, prefixed by an explicit
    ``ENDIR IDLE;``/``ENDDR IDLE;`` header. Raises :class:`TapIrSvfError` for an op this
    emitter can't represent (currently none of the ops this project's own callers
    produce trigger this -- ``GotoState`` is unconditionally droppable, see module
    docstring -- but a genuinely unsupported future op type would land here rather than
    silently emitting something wrong)."""
    lines: List[str] = ["ENDIR IDLE;", "ENDDR IDLE;"]
    for op in ir_ops:
        if isinstance(op, GotoState):
            continue  # SIR/SDR's own automatic navigation makes this redundant; see docstring
        elif isinstance(op, ShiftIR):
            lines.append(_scan_line("SIR", op))
        elif isinstance(op, ShiftDR):
            lines.append(_scan_line("SDR", op))
        elif isinstance(op, Runtest):
            lines.append(
                f"RUNTEST {TAP_STATE_NAMES[op.run_state]} {op.count} TCK "
                f"ENDSTATE {TAP_STATE_NAMES[op.end_state]};"
            )
        else:
            raise TapIrSvfError(f"to_svf() does not support op {op!r}")
    return "\n".join(lines) + "\n"
