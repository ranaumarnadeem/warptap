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


class PulsePin(NamedTuple):
    """Pulse a named signal OTHER than TCK/TMS/TDI/TDO (implementation_plan.md §7 Stage 13)
    -- e.g. a DUT's functional/system clock -- ``count`` times, independent of TCK's own
    timing domain. TAP pins (TCK/TMS/TDI) stay static throughout. ``hold_pins`` optionally
    drives other named signals (e.g. a faultflow pattern's ``capture_pi_values``) to fixed
    values for the same duration -- both only ever meaningful together, at the moment of
    forcing a functional capture edge, hence one op rather than two.

    Exists because gating this pulse from TCK itself (an earlier design considered and
    rejected) is unsound for a real class of external ATPG patterns: a transition/delay fault
    needs an at-speed capture edge, which TCK -- externally, slowly, ATE-driven -- cannot
    provide. Deliberately NOT expressible via SVF/STAPL (a JTAG-pins-only vocabulary, no
    concept of a second clock domain) -- both emitters raise their own existing
    ``TapIrSvfError``/``TapIrStaplError`` for it; only :mod:`warptap.tap_ir_stil` can render
    it, since STIL's own ``WaveformTable`` mechanism lets an independently-timed signal
    coexist with the JTAG pins in one pattern."""

    port: str
    count: int
    hold_pins: tuple[tuple[str, int], ...] = ()


#: Canonical SVF/STAPL state names for each of the 16 IEEE 1149.1 TAP states -- verbatim from
#: the SVF Specification Rev. E p.6 and JESD71 (STAPL) Annex A, which name all 16 states
#: identically (only the *stable_state* argument of STATE/RUNTEST/ENDIR/ENDDR/IRSTOP/DRSTOP is
#: restricted to the 4-member subset RESET/IDLE/IRPAUSE/DRPAUSE). Shared here rather than
#: duplicated per emitter -- unlike sib_layout.py's live-vs-static counting rule (a genuine
#: algorithmic duplication case), this is a single static lookup table with one unambiguous
#: correct answer per standard, so two independently-hand-copied dicts would only ever be a
#: drift risk, not a meaningful separation of concerns.
TAP_STATE_NAMES: dict[TapState, str] = {
    TapState.TEST_LOGIC_RESET: "RESET",
    TapState.RUN_TEST_IDLE: "IDLE",
    TapState.SELECT_DR_SCAN: "DRSELECT",
    TapState.CAPTURE_DR: "DRCAPTURE",
    TapState.SHIFT_DR: "DRSHIFT",
    TapState.EXIT1_DR: "DREXIT1",
    TapState.PAUSE_DR: "DRPAUSE",
    TapState.EXIT2_DR: "DREXIT2",
    TapState.UPDATE_DR: "DRUPDATE",
    TapState.SELECT_IR_SCAN: "IRSELECT",
    TapState.CAPTURE_IR: "IRCAPTURE",
    TapState.SHIFT_IR: "IRSHIFT",
    TapState.EXIT1_IR: "IREXIT1",
    TapState.PAUSE_IR: "IRPAUSE",
    TapState.EXIT2_IR: "IREXIT2",
    TapState.UPDATE_IR: "IRUPDATE",
}


def bits_to_int(bits: list[int]) -> int:
    """Pack a chronological-order bit list (index 0 = cycle 1) into this module's
    LSB-first int convention."""
    return sum(bit << i for i, bit in enumerate(bits))


def bits_from_int(value: int, width: int) -> list[int]:
    """Inverse of :func:`bits_to_int`: unpack ``value`` into a ``width``-long
    chronological-order bit list."""
    return [(value >> i) & 1 for i in range(width)]
