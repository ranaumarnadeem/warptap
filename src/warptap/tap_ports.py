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
"""

from __future__ import annotations

TCK = "tck"
TMS = "tms"
TDI = "tdi"
TRST_N = "trst_n"
TDO = "tdo"

TOP_LEVEL_JTAG_PORTS = (TCK, TMS, TDI, TRST_N, TDO)
