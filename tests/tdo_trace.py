"""The inserted TAP's ``tdo`` pin as a testbench trace prints it (``$display("%0d", tdo)``),
checked against IEEE 1149.1's rule for the pin: it carries a bit only while the TAP is resident
in Shift-IR or Shift-DR, and is released, high impedance (printed ``z``), in every other state.

An insertion's default ``tdo_drive="tristate"`` does exactly that, which
``tests/test_tdo_drive_cross_sim.py`` proves on its own, edges included. Every other cross-sim
that reads the pin parses it with :func:`tdo_bit`, beside the TAP state its testbench prints, so
each of them checks the rule on every cycle it samples as well.
"""

from __future__ import annotations

from typing import Optional

from warptap.tap_fsm import TapState
from warptap.tap_model import drives_tdo


def tdo_bit(state: object, tdo: str) -> Optional[int]:
    """``tdo`` as sampled in a cycle spent in TAP state ``state`` (the state the trace printed
    beside it): the bit, 0 or 1, in Shift-IR/Shift-DR, and ``None`` in every other state, where
    the pin must read ``z``. Anything else fails: a released pin in a shift state, a driven one
    outside them, or an unknown value (``x``)."""
    tap_state = TapState(int(state))
    if drives_tdo(tap_state):
        assert tdo in ("0", "1"), f"tdo reads {tdo!r} in {tap_state.name}: it must carry a bit there"
        return int(tdo)
    assert tdo == "z", (
        f"tdo reads {tdo!r} in {tap_state.name}: IEEE 1149.1 has it released (z) outside "
        "Shift-IR/Shift-DR"
    )
    return None


def model_tdo(state: TapState, tdo: int) -> Optional[int]:
    """What :func:`tdo_bit` returns for a cycle a Python model spent in ``state`` with ``tdo`` its
    TDO value (:meth:`warptap.tap_model.TapModel.tick`'s): the bit in a shift state, else
    ``None``, so a model trace compares equal to an RTL trace parsed with :func:`tdo_bit`."""
    return tdo if drives_tdo(state) else None
