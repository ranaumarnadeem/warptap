"""TAP-transaction IR (implementation_plan.md §3.4, §7 Stage 5). One internal representation
for a scan-test sequence, independent of output format -- built now, not deferred to Stage 7,
because §3.4's own stated justification ("reduces header+payload+trailer composition from the
SIB-tree retargeting result to shift/OR/mask arithmetic") describes exactly the composition
problem ``pdl_interpreter.PDLInterpreter.iApply`` already has to solve. A Stage-5-private
alternative would need the same fields anyway; Stage 7's SVF/STAPL emitters and Stage 5's own
``tap_ir_play.play()`` become two stateless backends walking the same list.

Scan data is an arbitrary-precision Python ``int`` with an explicit ``bits`` width and an
optional ``mask`` (which bits of ``tdo`` to actually compare), not a bit-list -- matches SVF/
STAPL's own LSB-first hex conventions and keeps composition to shift/OR/mask arithmetic.

**Bit-encoding convention** for ``tdi``/``tdo``/``mask``: bit ``i`` is the value shifted on
cycle ``i + 1`` of that op -- chronological/feed order, matching how this project's own tests
already represent an observed shift-out sequence as an int
(``sum(bit << i for i, bit in enumerate(observed_bits))``). Any code composing these ints from
:mod:`warptap.sib_layout`'s *position*-ordered bit lists (index 0 = the chain position nearest
TDI, i.e. what that position ends up holding once the whole shift completes) must reverse the
list first -- the two orders are exact reverses of each other, since the last bit fed ends up
at the position nearest TDI (the same "last fed bit lands at position 0" rule every prior
stage's directed tests already rely on).

``GotoState``/``Runtest`` reuse :class:`~warptap.tap_fsm.TapState` directly rather than a
parallel enum -- the same "one canonical table, never two that could drift apart" reasoning
implementation_plan.md §7 Stage 2 already gives for sharing the FSM table between the RTL
codegen and the Python model.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from warptap.tap_fsm import TapState


class GotoState(NamedTuple):
    state: TapState


class ShiftIR(NamedTuple):
    bits: int
    tdi: int
    tdo: Optional[int] = None
    mask: Optional[int] = None


class ShiftDR(NamedTuple):
    bits: int
    tdi: int
    tdo: Optional[int] = None
    mask: Optional[int] = None


class Runtest(NamedTuple):
    count: int
    run_state: TapState = TapState.RUN_TEST_IDLE
    end_state: TapState = TapState.RUN_TEST_IDLE


def bits_to_int(bits: list[int]) -> int:
    """Pack a chronological-order bit list (index 0 = cycle 1) into this module's
    LSB-first int convention."""
    return sum(bit << i for i, bit in enumerate(bits))


def bits_from_int(value: int, width: int) -> list[int]:
    """Inverse of :func:`bits_to_int`: unpack ``value`` into a ``width``-long
    chronological-order bit list."""
    return [(value >> i) & 1 for i in range(width)]
