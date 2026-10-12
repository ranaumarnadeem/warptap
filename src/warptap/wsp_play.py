"""Scan operations lowered to cycles on an IEEE 1500 wrapper serial port, and played
against the model (:mod:`warptap.wsp1500_model`).

One row is one tester cycle: the WSP's inputs applied, WSO strobed -- against the value
the operation expects there, where it expects one -- then WRCK pulsed, rising and falling
(the falling edge is what the WIR's update stage takes).

- :class:`~warptap.wsp_ir.WspReset` is a cycle with WRSTN low and no pulse.
- ``ShiftIR(bits, tdi)`` is a WIR scan: SelectWIR and ShiftWR high for each bit -- bit 0
  of ``tdi`` first -- then one cycle of SelectWIR and UpdateWR. The WIR has no capture.
- ``ShiftDR(bits, tdi)`` is a data-register scan, the WIR deselected: a cycle of
  CaptureWR, ``bits`` of ShiftWR, a cycle of UpdateWR.
- ``Runtest(count)`` is ``count`` idle cycles with WRCK pulsing; ``GotoState`` has no
  meaning here and is skipped.

``tdo`` and ``mask`` of a scan are the TAP's: bit ``i`` of ``tdo`` is the WSO the
``i``-th shift cycle should strobe, where bit ``i`` of ``mask`` (all, without one) says
it is checked. ``cfi`` gives the boundary cells' functional inputs (each port bit, or the
core's output, in serial order) for every cycle.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

from warptap.tap_ir import GotoState, Runtest, ShiftDR, ShiftIR
from warptap.wsp1500_model import Value, Wsp1500Chain, WspCycle
from warptap.wsp_ir import WspReset


class WspPlayError(ValueError):
    """An operation a WSP can't play."""


class WspRow(NamedTuple):
    """A cycle, and the WSO its strobe should show (None: not checked)."""

    cycle: WspCycle
    wso: Optional[int] = None
    label: str = ""


def _expected(
    tdo: Optional[int], mask: Optional[int], bit: int
) -> Optional[int]:
    if tdo is None:
        return None
    if mask is not None and not (mask >> bit) & 1:
        return None
    return (tdo >> bit) & 1


def to_wsp_rows(
    ops: Sequence[object], *, cfi: tuple[Value, ...] = ()
) -> list[WspRow]:
    """``ops`` as WSP cycles (see the module docstring)."""
    rows: list[WspRow] = []
    for op in ops:
        if isinstance(op, WspReset):
            rows.append(WspRow(WspCycle(wrstn=0, pulse=False, cfi=cfi), None, "reset"))
        elif isinstance(op, GotoState):
            continue
        elif isinstance(op, Runtest):
            rows += [WspRow(WspCycle(cfi=cfi), None, "idle")] * op.count
        elif isinstance(op, (ShiftIR, ShiftDR)):
            wir = isinstance(op, ShiftIR)
            select = 1 if wir else 0
            label = "ir" if wir else "dr"
            if not wir:
                rows.append(
                    WspRow(WspCycle(selectwir=0, capturewr=1, cfi=cfi), None, label)
                )
            for bit in range(op.bits):
                rows.append(
                    WspRow(
                        WspCycle(
                            selectwir=select,
                            shiftwr=1,
                            wsi=(op.tdi >> bit) & 1,
                            cfi=cfi,
                        ),
                        _expected(op.tdo, op.mask, bit),
                        label,
                    )
                )
            rows.append(
                WspRow(WspCycle(selectwir=select, updatewr=1, cfi=cfi), None, label)
            )
        else:
            raise WspPlayError(
                f"{type(op).__name__} has no meaning on a wrapper serial port"
            )
    return rows


def play(chain: Wsp1500Chain, rows: Sequence[WspRow]) -> list[Value]:
    """Run ``rows`` against ``chain``; the WSO at each row's strobe."""
    return [chain.step(row.cycle) for row in rows]


def mismatches(
    rows: Sequence[WspRow], observed: Sequence[Value]
) -> list[int]:
    """The rows whose strobe isn't the WSO they expect: an unknown (None) where a value
    is expected counts."""
    return [
        number
        for number, (row, got) in enumerate(zip(rows, observed))
        if row.wso is not None and got != row.wso
    ]
