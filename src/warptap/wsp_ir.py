"""Operations on an IEEE 1500 wrapper serial port that have no TAP counterpart.

The scan operations are :mod:`warptap.tap_ir`'s -- ``ShiftIR`` is a WIR scan, ``ShiftDR``
a data-register scan, ``Runtest`` idle WRCK cycles -- so the PDL layer above produces the
same operations whichever access it retargets onto. The one operation a WSP adds is
WRSTN: a cycle with the wrapper reset asserted, the only way to a known WIR.
"""

from __future__ import annotations

from typing import NamedTuple


class WspReset(NamedTuple):
    """One cycle with WRSTN low and WRCK still: the wrapper's WIR update stage and WBY
    are cleared, WS_BYPASS in force."""
