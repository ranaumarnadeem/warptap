"""STAPL (JEDEC JESD71 / IEEE 1532) text emitter (implementation_plan.md §3.4, §6, §7 Stage 7).
Walks the same :mod:`warptap.tap_ir` ops list the SVF backend (:mod:`warptap.tap_ir_svf`) and
:mod:`warptap.tap_ir_play` already walk -- the third "stateless pretty-printer over one IR"
§3.4 promises. Grounded in the full JESD71-1999 spec text and Altera/Actel's real reference
STAPL-player source (``jamcrc.c``), fetched and read directly during Stage 7's own
implementation session.

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

**``COMPARE`` clause, for ops carrying ``tdo``/``mask``.** Grammar confirmed this session
against five independently-converging real sources -- the full JESD71-1999 spec text (read
directly, §8.8 ``DRSCAN``/§8.18 ``IRSCAN``), real vendor-generated ``.jam``/``.stp`` files
(Altera Quartus Prime, Altera's own "Chain Interrogation" utility, Microsemi FlashPro), and
two independent copies of the Altera "Jam STAPL Player" reference C source
(``jamexec.c``, sibling of the ``jamcrc.c`` already used for :mod:`warptap.stapl_crc16`) --
all agreeing on the same grammar, unlike an earlier, lower-confidence secondary summary that
wrongly implied a separate ``MASK`` keyword:

    DRSCAN <length>, <data> [,COMPARE <compare array>, <mask array>, <result>];
    IRSCAN <length>, <data> [,COMPARE <compare array>, <mask array>, <result>];

``COMPARE`` is not a standalone statement -- it is a trailing clause on ``DRSCAN``/``IRSCAN``,
taking exactly three positional (unlabeled) arguments: the expected value, a mask (bit 1 =
compare this position, 0 = don't-care), and a **previously-declared scalar ``BOOLEAN``**
variable that receives the result. ``<compare array>``/``<mask array>`` may be bare hex
literals (confirmed in a real Microsemi FlashPro file: ``DRSCAN 8, $00,COMPARE $00,$80,
PASS;``) -- this emitter uses that form, avoiding any need to also declare/populate BOOLEAN
*array* variables for the expected/mask data, matching this module's existing bare-hex-literal
``TDI`` convention.

Per JESD71's own text, a failed ``COMPARE`` sets ``<result>`` ``FALSE`` but does **not** abort
execution by itself -- every real file this session found follows a ``COMPARE`` with an
explicit check (``IF <result> == 0 THEN ...``) to actually act on failure, exactly the pattern
this emitter reproduces (``IF <result_var> == 0 THEN EXIT (1);``, real syntax confirmed
directly in Altera's IDCODE.JAM: ``IF (stuck_tdo_flag == 1) THEN EXIT (1);``). Without this,
STAPL's ``COMPARE`` would silently NOT enforce what SVF's ``TDO``/``MASK`` enforces
automatically (SVF's own player checks and reports a mismatch with no extra statement needed
in the file, confirmed directly against a real OpenOCD ``svf`` run this session) -- the two
emitted formats would otherwise diverge in behavior for identical ``ir_ops`` input. Each
``COMPARE``-bearing op gets its own uniquely-named result variable
(``compare_result_<n>``), declared with a plain ``BOOLEAN <name>;`` statement immediately
before its first use, directly inside ``PROCEDURE DO_RUN`` -- the Jam 1.1 language spec's own
inline example (``BOOLEAN verify_error; DRSCAN ..., COMPARE ..., verify_error;``) shows a
local declaration immediately preceding use with no surrounding ``DATA``/``USES`` block, which
this module follows since its own ``PROCEDURE DO_RUN`` has no such block either; real
vendor-generated files instead declare variables in a separate ``DATA`` block referenced via
``USES`` on the ``PROCEDURE`` line, an alternative, more elaborate structure not adopted here.
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
    """Raised when ``to_stapl`` is given an op it can't render -- an unsupported op type, or
    a ``ShiftIR``/``ShiftDR`` with exactly one of ``tdo``/``mask`` set (STAPL's ``COMPARE``
    clause needs both together, see module docstring)."""


def _hex(value: int, bits: int) -> str:
    digits = (bits + 3) // 4
    return f"${value:0{digits}X}"


def _scan_line(command: str, op: Union[ShiftIR, ShiftDR], *, result_var: str | None) -> List[str]:
    """Render one ``DRSCAN``/``IRSCAN`` statement. ``result_var`` is ``None`` for a bare
    scan (no ``COMPARE``); otherwise renders the three-line ``BOOLEAN`` declaration +
    ``COMPARE``-bearing scan + enforcement ``IF`` sequence (see module docstring's
    ``COMPARE`` section for the grammar and why the ``IF`` is needed)."""
    if result_var is None:
        return [f"  {command} {op.bits}, {_hex(op.tdi, op.bits)};"]
    return [
        f"  BOOLEAN {result_var};",
        f"  {command} {op.bits}, {_hex(op.tdi, op.bits)}, "
        f"COMPARE {_hex(op.tdo, op.bits)}, {_hex(op.mask, op.bits)}, {result_var};",
        f"  IF {result_var} == 0 THEN EXIT (1);",
    ]


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
    compare_count = 0
    for op in ir_ops:
        if isinstance(op, GotoState):
            continue  # IRSCAN/DRSCAN's own automatic navigation makes this redundant
        elif isinstance(op, (ShiftIR, ShiftDR)):
            command = "IRSCAN" if isinstance(op, ShiftIR) else "DRSCAN"
            if (op.tdo is None) != (op.mask is None):
                raise TapIrStaplError(
                    f"{command} op has exactly one of tdo/mask set (tdo={op.tdo!r}, "
                    f"mask={op.mask!r}) -- STAPL's COMPARE clause needs both together"
                )
            result_var = None
            if op.tdo is not None:
                result_var = f"compare_result_{compare_count}"
                compare_count += 1
            lines.extend(_scan_line(command, op, result_var=result_var))
        elif isinstance(op, Runtest):
            lines.append(
                f"  WAIT {TAP_STATE_NAMES[op.run_state]}, {op.count} CYCLES, "
                f"{TAP_STATE_NAMES[op.end_state]};"
            )
        else:
            raise TapIrStaplError(f"to_stapl() does not support op {op!r}")
    lines.append("ENDPROC;")

    body = "\n".join(lines) + "\n"
    # The emitted file has a blank line between ENDPROC; and CRC (below) -- that blank
    # line's own newline is itself covered by the CRC (confirmed directly against the real
    # Altera Jam STAPL Player reference source, jamcrc.c's jam_check_crc(): it rewinds its
    # running CRC to the state right after the single whitespace character immediately
    # preceding the "CRC" keyword token, which for this file's layout is that second
    # newline, not the first). `body` itself ends in only one newline (ENDPROC;'s own), so
    # the second is added explicitly here before hashing -- omitting it was a real,
    # previously-undetected bug (this module's own tests only re-derived the same wrong
    # boundary independently, self-consistent but never checked against ground truth) caught
    # by feeding a real emitted file through a compiled copy of that same reference player,
    # which reported a CRC mismatch (own-computed 0x8C8 vs. its independently-recomputed
    # 0x156E) until this fix.
    crc = stapl_file_crc(body + "\n")
    return body + f"\nCRC {crc:04X};\n"
