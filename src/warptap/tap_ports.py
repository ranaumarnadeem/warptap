"""Shared top-level JTAG port-name constants (implementation_plan.md §7 Stages 10-12).

Extracted now because ``icl_emit.py`` (Stage 10) becomes a genuine THIRD occurrence of these
same five literal strings -- previously duplicated verbatim, independently, in
``sib_insert.py`` and ``bsr_insert.py`` (each calling ``top_mod.add_port("tck", "input")``
etc. with its own copy of the names). Crossing this project's own "rule of three" duplication
threshold (the same reasoning ``sib_layout.py``'s and ``sib_insert.py``'s own docstrings
already invoke for NOT extracting at exactly two occurrences) -- a third independent copy is
what actually justifies a shared module, not a general tidiness preference.

These are the top-level pin names warptap itself chooses when adding the TAP's JTAG ports to
a target module (``Module.add_port(...)`` in ``sib_insert.py``/``bsr_insert.py``) -- distinct
from, and not to be confused with, ``rtl/tap_core.v``/``rtl/sib_cell.v``/etc.'s own internal
port names of the same spelling (e.g. ``tap_core.v``'s own ``tck`` input): those are fixed by
the hand-authored RTL templates themselves and matched 1:1 in ``add_cell(...,
port_directions={...})`` calls, and are deliberately NOT sourced from here -- changing them
would mean changing the .v files, an unrelated concern this module has no business touching.

:func:`add_tdo_pin` is the one piece of wiring both insertions share: how ``tap_core``'s
``tdo``/``tdo_oe`` reach the top-level pin, per the ``tdo_drive`` keyword
(:data:`warptap.tap_model.TDO_DRIVES`).
"""

from __future__ import annotations

from warptap.netlist import Bit, Module
from warptap.tap_model import check_tdo_drive

TCK = "tck"
TMS = "tms"
TDI = "tdi"
TRST_N = "trst_n"
TDO = "tdo"

TOP_LEVEL_JTAG_PORTS = (TCK, TMS, TDI, TRST_N, TDO)

# The extra top-level output ``tdo_drive="enable_port"`` adds: tap_core's tdo_oe, for a pad cell.
TDO_OE = "tdo_oe"


def add_tdo_pin(top_mod: Module, tdo_drive: str) -> dict[str, list[Bit]]:
    """Add the top-level ``tdo`` output to ``top_mod`` (and ``tdo_oe`` too, for
    ``"enable_port"``), wired as ``tdo_drive`` says, and return what ``tap_core``'s ``tdo`` and
    ``tdo_oe`` outputs connect to: the caller merges it into that instance's connections. For
    ``"tristate"`` both go to new wires feeding a ``$tribuf`` (``warptap_tdo_tribuf``) that drives
    the pin, the same cell kind :mod:`warptap.bsr_insert` puts on an output3 pin. For ``"low"``
    there is no ``tdo_oe`` entry: it is left unconnected."""
    check_tdo_drive(tdo_drive)
    tdo_bits = top_mod.add_port(TDO, "output")
    if tdo_drive == "low":
        return {"tdo": tdo_bits}
    if tdo_drive == "enable_port":
        return {"tdo": tdo_bits, "tdo_oe": top_mod.add_port(TDO_OE, "output")}
    data_bits = top_mod.new_wire(1, name="warptap_tdo_data")
    oe_bits = top_mod.new_wire(1, name="warptap_tdo_oe")
    top_mod.add_cell(
        "warptap_tdo_tribuf",
        "$tribuf",
        parameters={"WIDTH": 1},
        port_directions={"A": "input", "EN": "input", "Y": "output"},
        connections={"A": data_bits, "EN": oe_bits, "Y": tdo_bits},
    )
    return {"tdo": data_bits, "tdo_oe": oe_bits}
