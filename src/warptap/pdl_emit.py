"""PDL (Procedural Description Language) text emitter (implementation_plan.md §7 Stage 11).
Walks a ``PDLInterpreter``'s own ``self.history`` (:mod:`warptap.pdl_history`) -- not
``self.program``, which is already-lowered low-level ``tap_ir`` ops with no retained
per-instrument identity, see ``pdl_history``'s own module docstring -- producing real PDL
statement text, the third of §3.4's "stateless pretty-printers over one IR/model" family
(after ``tap_ir_svf.py``/``tap_ir_stapl.py``'s ``tap_ir`` emitters and ``icl_emit.py``'s
``icl_model`` emitter).

**No independent PDL *semantic* validation tool exists anywhere** -- confirmed by an exhaustive
search of the real PDL ecosystem, and reconfirmed directly against the vendored
``Honza255/icl_parser`` submodule's own retargeting engine (``IclRegisterModel``/``Ijtag.
iWrite``/``iApply``/``getiApplyVectors``): tried directly against a warptap-canonical SIB
network and confirmed it doesn't compute a correct result for one -- that engine's own README
states plainly that DataRegister retargeting ("Data registers"/"Retargeting for data
registers") is explicitly **not supported**, exactly the register kind a WRITE instrument's
own committed value lives in.

**A real, independent GRAMMAR *does* now exist**, though: that same submodule's
``src/pdl_parser/pdl.g4`` was a bare, never-compiled ANTLR grammar (a real "PDL0 grammar
v20130806", not a stub -- it covers every statement kind this emitter renders) with zero
generated lexer/parser code and zero Python wiring anywhere in that repository. Compiled here
via ANTLR 4.7.2 (matching the exact version already pinned for the ICL grammar) after fixing
several real bugs in the grammar file itself (an unterminated rule, an undefined rule
reference, two rule names colliding with Python builtins in the Python3 target, a missing
``WS`` token) -- see the vendored fork's own commit history for the full list. This closes the
gap at the same tier Stage 7's SVF/STAPL validation already sits at: independent *grammar*
correctness (this emitter's output parses as real PDL syntax), not independent *retargeting-
semantics* correctness (which nothing here checks, for any format, including the ones with
real external validators) -- see ``tests/test_pdl_emit_iclparser_validation.py``.

Live-validating against this newly-compiled grammar caught one real, genuine bug this way:
this emitter used to render ``-sck`` as a bare flag with no port name, reasoning (at the time)
that neither PDL text nor ICL gave the port a confirmed place to live. The compiled grammar's
own ``irunloop_def`` rule proves that reasoning incomplete for the PDL-text half of that claim
-- ``-sck`` takes a mandatory port-name operand (``WS '-sck' WS port``, the last ``WS`` itself
a real grammar bug fixed above) -- and ``pdl_emit.py`` already has that name in hand
(``PdlRunLoopStmt.sck_port``), so omitting it produced text that fails to parse as real PDL.
Fixed by rendering it: ``iRunLoop <count> -sck <port>;``.

**Field addressing (Stage 15)**: real PDL addresses a named sub-field (``TDR_bit``/``UCreg``/
ICL ``Alias``) inside ``iWrite``/``iRead``. ``pdl_history.PdlWriteStmt``/``PdlReadStmt.field``
is either a declared :class:`~warptap.icl_model.Alias` name (when ``PDLInterpreter.iWrite``/
``iRead`` was called with one) or the whole target instrument's own name (Stage 5's original,
still-supported case) -- this emitter renders exactly that string either way, not a fabricated
sub-field; ``_instrument_width`` resolves the correct hex-padding width by searching the
target instrument's own ``aliases`` first, falling back to its whole width.

**Value formatting**: ``iWrite``/``iRead`` values render as a zero-padded ``0x``-hex literal
sized to the addressed field's own width (``ceil(width / 4)`` hex digits -- the *alias*'s
width when one was addressed, else the whole instrument's) -- matching real PDL usage this
session's research found, and this project's own "always emit every field explicitly" house
style (``tap_ir_svf.py``, ``tap_ir_stapl.py``).

**``iRunLoop`` renders ``-sck <port>`` or bare ``-tck`` (Stage 15)** depending on whether
``PdlRunLoopStmt.sck_port`` is set -- matching whichever selector
``PDLInterpreter.iRunLoop(..., sck_port=...)`` was actually called with (Stage 5's original
``-tck``-only rendering was the ``sck_port is None`` case all along). ``-tck`` stays a bare
flag (TCK is always the one, implicit TAP clock, needing no name); ``-sck`` renders its own
port name, per the newly-compiled grammar's own requirement (see module docstring above).
"""

from __future__ import annotations

from typing import List

from warptap.errors import WarptapError
from warptap.icl_model import PhysicalGraph
from warptap.pdl_history import (
    PdlApplyStmt,
    PdlReadStmt,
    PdlRunLoopStmt,
    PdlStatement,
    PdlTargetStmt,
    PdlWriteStmt,
)


class PdlEmitError(WarptapError):
    """Raised when ``to_pdl`` is given a statement it can't render -- currently only an
    ``iWrite``/``iRead`` field naming an instrument absent from ``graph`` (a caller passing a
    ``history``/``graph`` pair that didn't come from the same ``PDLInterpreter``)."""


def _instrument_width(graph: PhysicalGraph, field: str) -> int:
    """Hex-padding width for ``field``: the addressed :class:`~warptap.icl_model.Alias`'s own
    width (Stage 15) when ``field`` names one, else the whole instrument's width when
    ``field`` names the instrument directly (Stage 5's original, still-supported case)."""
    for node in graph.chain:
        if node.instrument is None:
            continue
        if node.instrument.name == field:
            return node.instrument.width
        for alias in node.instrument.aliases:
            if alias.name == field:
                return alias.high_bit - alias.low_bit + 1
    raise PdlEmitError(f"no instrument or alias named {field!r} in this network's graph")


def _hex(value: int, width_bits: int) -> str:
    """Zero-pad ``value`` to the full ``ceil(width_bits / 4)``-hex-digit width, matching
    ``tap_ir_svf.py``'s own ``_hex`` "always emit every field explicitly" precedent."""
    digits = (width_bits + 3) // 4
    return f"0x{value:0{digits}X}"


def to_pdl(history: List[PdlStatement], graph: PhysicalGraph) -> str:
    """Render ``history`` as PDL text, one statement per line, in the exact call order they
    were recorded. Raises :class:`PdlEmitError` if an ``iWrite``/``iRead`` statement names an
    instrument not present in ``graph`` -- never happens for a ``history``/``graph`` pair that
    came from the same :class:`~warptap.pdl_interpreter.PDLInterpreter`, but guards against a
    caller passing mismatched arguments rather than silently emitting an unsized literal."""
    lines: List[str] = []
    for stmt in history:
        if isinstance(stmt, PdlTargetStmt):
            lines.append(f"iTarget {stmt.dotted};")
        elif isinstance(stmt, PdlWriteStmt):
            width = _instrument_width(graph, stmt.field)
            lines.append(f"iWrite {stmt.field} {_hex(stmt.value, width)};")
        elif isinstance(stmt, PdlReadStmt):
            width = _instrument_width(graph, stmt.field)
            lines.append(f"iRead {stmt.field} {_hex(stmt.expected, width)};")
        elif isinstance(stmt, PdlRunLoopStmt):
            if stmt.sck_port is not None:
                lines.append(f"iRunLoop {stmt.count} -sck {stmt.sck_port};")
            else:
                lines.append(f"iRunLoop {stmt.count} -tck;")
        elif isinstance(stmt, PdlApplyStmt):
            lines.append("iApply;")
        else:
            raise PdlEmitError(f"to_pdl() does not support statement {stmt!r}")
    return "\n".join(lines) + ("\n" if lines else "")
