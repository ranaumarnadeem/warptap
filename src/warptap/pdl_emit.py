"""PDL (Procedural Description Language) text emitter (implementation_plan.md §7 Stage 11).
Walks a ``PDLInterpreter``'s own ``self.history`` (:mod:`warptap.pdl_history`) -- not
``self.program``, which is already-lowered low-level ``tap_ir`` ops with no retained
per-instrument identity, see ``pdl_history``'s own module docstring -- producing real PDL
statement text, the third of §3.4's "stateless pretty-printers over one IR/model" family
(after ``tap_ir_svf.py``/``tap_ir_stapl.py``'s ``tap_ir`` emitters and ``icl_emit.py``'s
``icl_model`` emitter).

**No independent PDL validation tool exists anywhere** -- confirmed by an exhaustive search of
the real PDL ecosystem earlier this session, and reconfirmed directly against the vendored
``Honza255/icl_parser`` submodule itself: its ``src/pdl_parser/pdl.g4`` is a bare ANTLR
grammar file with zero generated lexer/parser code, zero Python wiring, and zero other
reference anywhere in that repository (a full-repo grep found nothing beyond that one file and
a spec-quoting comment in an unrelated ICL test) -- confirming, not merely suspecting, that
it's an unwired grammar definition, not a real parser, exactly the "if negative" branch this
stage's own plan anticipated. Unlike ``icl_emit.py``, this emitter therefore has **no
independent oracle to validate against** -- see ``tests/test_pdl_emit_integration.py``'s own
self-consistency-only cross-check, loudly documented there as NOT equivalent-strength to
Stage 7's OpenOCD/Jam-player validation or Stage 10's ``icl_parser`` validation.

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

**``iRunLoop`` renders ``-sck`` or ``-tck`` (Stage 15)** depending on whether
``PdlRunLoopStmt.sck_port`` is set -- matching whichever selector
``PDLInterpreter.iRunLoop(..., sck_port=...)`` was actually called with (Stage 5's original
``-tck``-only rendering was the ``sck_port is None`` case all along). The port name itself is
never rendered -- real PDL's ``-sck``/``-tck`` are bare flags in the text, carrying no port
name.
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
            flag = "-sck" if stmt.sck_port is not None else "-tck"
            lines.append(f"iRunLoop {stmt.count} {flag};")
        elif isinstance(stmt, PdlApplyStmt):
            lines.append("iApply;")
        else:
            raise PdlEmitError(f"to_pdl() does not support statement {stmt!r}")
    return "\n".join(lines) + ("\n" if lines else "")
