"""High-level PDL statement history (implementation_plan.md §7 Stage 11). ``PDLInterpreter``
lowers straight to bit-packed :mod:`warptap.tap_ir` ops with no retained record of which
instrument/value a given ``iApply`` actually targeted -- that information is proven
unrecoverable from the low-level ops alone (a ``ShiftDR.tdi`` is one flat integer over the
whole physically-open network, not a per-instrument value). ``pdl_emit.to_pdl`` needs the
original high-level intent, so :class:`PDLInterpreter` additionally records one of these per
successful command call, in call order -- separate from (and simpler than) ``self.program``'s
own deferred/batched low-level lowering.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Union


class PdlTargetStmt(NamedTuple):
    """``iTarget <dotted>;``"""

    dotted: str


class PdlWriteStmt(NamedTuple):
    """``iWrite <field> <value>;``. ``field`` is either a declared ICL ``Alias`` name (real
    PDL's named sub-field addressing, ``TDR_bit``/``UCreg``/ICL ``Alias`` -- Stage 15) when
    ``PDLInterpreter.iWrite`` was called with one, or the current scope's own instrument name
    when it wasn't (Stage 5's original whole-instrument case, still fully supported)."""

    field: str
    value: int


class PdlReadStmt(NamedTuple):
    """``iRead <field> <expected>;`` -- same ``field`` convention as :class:`PdlWriteStmt`."""

    field: str
    expected: int


class PdlRunLoopStmt(NamedTuple):
    """``iRunLoop <count> -tck;`` (``sck_port is None``, Stage 5's original, still-default
    case) or ``iRunLoop <count> -sck;`` (``sck_port`` names a real functional-clock port --
    Stage 15's addition to ``PDLInterpreter.iRunLoop``). ``sck_port`` itself is never rendered
    in PDL text -- real PDL's ``-sck``/``-tck`` are bare flags, carrying no port name -- it
    only exists here to tell ``pdl_emit.to_pdl`` which flag to render."""

    count: int
    sck_port: Optional[str] = None


class PdlApplyStmt(NamedTuple):
    """``iApply;`` -- carries no data of its own."""


PdlStatement = Union[PdlTargetStmt, PdlWriteStmt, PdlReadStmt, PdlRunLoopStmt, PdlApplyStmt]
