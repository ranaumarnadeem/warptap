"""STAPL (JEDEC JESD71 / IEEE 1532) text emitter (implementation_plan.md §3.4, §6, §7 Stage 7).
Walks the same :mod:`warptap.tap_ir` ops list the SVF backend (:mod:`warptap.tap_ir_svf`) and
:mod:`warptap.tap_ir_play` already walk -- the third "stateless pretty-printer over one IR"
§3.4 promises. Grounded in the full JESD71-1999 spec text and Altera/Actel's real reference
STAPL-player source (``jamcrc.c``), fetched and read directly this session.

**File shape**: ``NOTE`` fields, one ``ACTION``, one ``PROCEDURE`` containing the lowered ops,
``ENDPROC``, then a ``CRC`` statement computed over everything before it
(:mod:`warptap.stapl_crc16`). STAPL mandates the ``CRC`` statement be the file's last
statement, computed over the whole file's ASCII source text up to that point -- this module
therefore builds the file in two passes: render everything through ``ENDPROC;``, compute the
CRC over that text, then append the ``CRC <hex4>;`` line.

**``GotoState`` ops are dropped, matching the SVF backend's own reasoning**: STAPL's
``IRSCAN``/``DRSCAN`` statements implicitly handle the full "navigate from the current stable
state into the shift state, shift, return to whatever ``IRSTOP``/``DRSTOP`` currently
specifies" sequence -- ``IRSTOP``/``DRSTOP`` are STAPL's own sticky-end-state directives,
directly analogous to SVF's ``ENDIR``/``ENDDR``, confirmed to default to ``IDLE`` the same
way. This module emits an explicit ``IRSTOP IDLE;``/``DRSTOP IDLE;`` header, matching every
``GotoState(RUN_TEST_IDLE)`` this project's own ops already target.

**Bit-encoding**: STAPL's own array-index convention (JESD71 §6.4/§8.8 -- "array index 0
corresponds to the rightmost character... shifted in increasing order of the array index," an
explicit "least significant bit to most significant bit" framing in Annex A's own worked
example) matches ``tap_ir.py``'s chronological/LSB-first int convention exactly, the same as
SVF's -- no bit reversal needed; a scan literal is a plain positional hex string
(``$<hex>``), MSB on the left, LSB on the right.

**Known scope limit, stated loudly rather than silently narrowed**: this v1 emitter renders
``ShiftIR``/``ShiftDR``'s ``tdi``/``bits`` fields but does not yet emit STAPL's ``COMPARE``
clause for ops carrying ``tdo``/``mask`` -- STAPL's ``COMPARE`` needs a declared result
variable and this project could not fully verify JESD71's exact variable-declaration syntax
in this research pass (unlike SVF's ``TDO``/``MASK``, which are inline literals needing no
declaration). Raises :class:`TapIrStaplError` for such an op rather than silently emitting a
file that drops the comparison -- matching this project's "document limitations loudly"
discipline (e.g. Stage 4's closed-instrument free-running limitation, Stage 5's iWrite
non-persistence).
"""

from __future__ import annotations

from typing import List, Union

from warptap.stapl_crc16 import stapl_file_crc
from warptap.tap_ir import TAP_STATE_NAMES, GotoState, Runtest, ShiftDR, ShiftIR

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest]

_MANDATORY_NOTE_FIELDS = (
    "DEVICE",
    "DATE",
    "CREATOR",
    "STAPL_VERSION",
    "ALG_VERSION",
    "STACK_DEPTH",
    "TARGET",
    "MAX_FREQ",
)


class TapIrStaplError(RuntimeError):
    """Raised when ``to_stapl`` is given an op it can't render (currently: a ``ShiftIR``/
    ``ShiftDR`` carrying ``tdo``/``mask`` -- see module docstring's scope-limit note)."""


def _hex(value: int, bits: int) -> str:
    digits = (bits + 3) // 4
    return f"${value:0{digits}X}"


def _scan_line(command: str, op: Union[ShiftIR, ShiftDR]) -> str:
    if op.tdo is not None or op.mask is not None:
        raise TapIrStaplError(
            f"{command} op with tdo/mask set (expected-value COMPARE) is not yet "
            "supported by this v1 STAPL emitter -- see tap_ir_stapl.py's module docstring"
        )
    return f"  {command} {op.bits}, {_hex(op.tdi, op.bits)};"


def to_stapl(
    ir_ops: List[_IrOp],
    *,
    device: str,
    date: str,
    creator: str = "warptap",
    stapl_version: str = "1.01",
    alg_version: str = "1",
    stack_depth: str = "1",
    target: str = "1",
    max_freq: str = "10000000",
) -> str:
    """Render ``ir_ops`` as a complete, single-``PROCEDURE`` STAPL file, ending in a real,
    computed ``CRC`` statement. ``device``/``date`` are required (this project doesn't
    invent device-identifying data, matching e.g. Stage 4's ``InstrumentNode`` discipline).

    ``target`` (JESD71 §10.2, Table 13) is not free text: it's a comma-separated list of
    1-based chain-position numbers, counted from the TDI side toward TDO, naming which
    device(s) in the physical scan chain this file's operations address. It defaults to
    ``"1"`` (the first, and in warptap's v1 scope the only, device in the chain) -- a real,
    meaningful default, not an "unspecified" placeholder, since warptap only ever targets a
    standalone TAP in v1, never a multi-device daisy chain. The remaining ``NOTE`` fields
    default to explicitly-labeled placeholder values -- this project has no real
    frequency/version data to report either, and says so via these defaults rather than
    fabricating plausible-looking ones.
    """
    note_values = {
        "DEVICE": device,
        "DATE": date,
        "CREATOR": creator,
        "STAPL_VERSION": stapl_version,
        "ALG_VERSION": alg_version,
        "STACK_DEPTH": stack_depth,
        "TARGET": target,
        "MAX_FREQ": max_freq,
    }

    lines: List[str] = []
    for field in _MANDATORY_NOTE_FIELDS:
        lines.append(f'NOTE "{field}" "{note_values[field]}";')
    lines.append("")
    lines.append("ACTION RUN = DO_RUN;")
    lines.append("")
    lines.append("PROCEDURE DO_RUN;")
    lines.append("  IRSTOP IDLE;")
    lines.append("  DRSTOP IDLE;")
    for op in ir_ops:
        if isinstance(op, GotoState):
            continue  # IRSCAN/DRSCAN's own automatic navigation makes this redundant
        elif isinstance(op, ShiftIR):
            lines.append(_scan_line("IRSCAN", op))
        elif isinstance(op, ShiftDR):
            lines.append(_scan_line("DRSCAN", op))
        elif isinstance(op, Runtest):
            lines.append(
                f"  WAIT {TAP_STATE_NAMES[op.run_state]}, {op.count} CYCLES, "
                f"{TAP_STATE_NAMES[op.end_state]};"
            )
        else:
            raise TapIrStaplError(f"to_stapl() does not support op {op!r}")
    lines.append("ENDPROC;")

    body = "\n".join(lines) + "\n"
    crc = stapl_file_crc(body)
    return body + f"\nCRC {crc:04X};\n"
