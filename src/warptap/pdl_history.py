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

from typing import NamedTuple, Union


class PdlTargetStmt(NamedTuple):
    """``iTarget <dotted>;``"""

    dotted: str


class PdlWriteStmt(NamedTuple):
    """``iWrite <field> <value>;``. ``field`` is the current scope's own instrument name --
    real PDL addresses a named sub-field (``TDR_bit``/``UCreg``/ICL ``Alias``), but
    ``PDLInterpreter.iWrite`` has no such argument (documented gap, see its own docstring), so
    there is nothing narrower than the whole instrument to name here either."""

    field: str
    value: int


class PdlReadStmt(NamedTuple):
    """``iRead <field> <expected>;`` -- same ``field`` convention as :class:`PdlWriteStmt`."""

    field: str
    expected: int


class PdlRunLoopStmt(NamedTuple):
    """``iRunLoop <count> -tck;``. No clock-selector field: ``PDLInterpreter.iRunLoop`` always
    drives ``TapState.RUN_TEST_IDLE``, so there is no ``-sck`` case to distinguish yet."""

    count: int


class PdlApplyStmt(NamedTuple):
    """``iApply;`` -- carries no data of its own."""


PdlStatement = Union[PdlTargetStmt, PdlWriteStmt, PdlReadStmt, PdlRunLoopStmt, PdlApplyStmt]
