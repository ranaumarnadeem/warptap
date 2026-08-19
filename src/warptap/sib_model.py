"""Python behavioral model of an inserted SIB network (implementation_plan.md §7 Stage 4),
implementing ``warptap.tap_model``'s ``DataRegister`` protocol so it can be plugged into
``TapModel.register_data_register(Instruction.EXTEST, ...)`` -- the same extension point
``bsr_model.BoundaryScanRegister`` uses. Used by ``tests/test_sib_insert_cross_sim.py`` to
cross-simulate against the real inserted RTL; not used by ``sib_insert.py`` itself.

Unlike ``BoundaryScanRegister`` (a fixed-length chain), the live bit sequence here has a
*dynamic* length: a closed SIB contributes 1 bit, an open SIB contributes its instrument's
bits followed by its own bit. ``shift()`` therefore rebuilds the current layout fresh on every
call rather than caching a fixed-length list once -- simpler and always correct regardless of
when a slot's ``po`` last changed, at the cost of doing that O(network size) walk every shift,
which is fine at this project's scale.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from warptap.icl_model import PhysicalGraph, SibNode


class _SlotState:
    __slots__ = ("sib_name", "width", "capture_value", "sib_shift_ff", "sib_po", "inst_shift_ff")

    def __init__(self, node: SibNode):
        if node.instrument is None or node.nested:
            raise NotImplementedError(
                "SibNetworkRegister does not yet model instrument-less or nested SIBs "
                "(implementation_plan.md §7 Stage 4 scopes v1 to flat/static networks)"
            )
        self.sib_name = node.sib_name
        self.width = node.instrument.width
        self.capture_value = node.instrument.capture_value
        self.sib_shift_ff = 0
        self.sib_po = 0
        self.inst_shift_ff = [0] * self.width


class _CellRef(NamedTuple):
    slot: _SlotState
    bit: Optional[int]  # index into slot.inst_shift_ff, or None for the SIB's own bit


class SibNetworkRegister:
    """Models the whole inserted network as one ``DataRegister``, matching how ``tap_core.v``
    sees it: a single serial path from TDI, through every slot in ``graph.chain`` order
    (TDI-side-first), to ``external_dr_tdo``."""

    def __init__(self, graph: PhysicalGraph):
        self._slots = [_SlotState(node) for node in graph.chain]

    def reset(self) -> None:
        """Zero every SIB's and instrument bit-cell's state, matching rtl/sib_cell.v's and
        rtl/bc1_shift_only.v's own `trst_n` reset behavior."""
        for slot in self._slots:
            slot.sib_shift_ff = 0
            slot.sib_po = 0
            slot.inst_shift_ff = [0] * slot.width

    def capture(self) -> None:
        """Unconditional for every slot, open or closed -- neither sib_cell's self-capture
        nor the instrument stub's capture_dr response is gated by nested_select in v1 (a
        named limitation, not a bug: see sib_insert.py's module docstring)."""
        for slot in self._slots:
            slot.sib_shift_ff = slot.sib_po
            for k in range(slot.width):
                slot.inst_shift_ff[k] = (slot.capture_value >> k) & 1

    def update(self) -> None:
        for slot in self._slots:
            slot.sib_po = slot.sib_shift_ff

    def _live_layout(self) -> list[_CellRef]:
        """TDI-side-first list of every currently-live flip-flop: a closed slot
        contributes just its own bit; an open slot contributes its instrument's bits
        (index order, nearest nested_si first) then its own bit -- mirrors the physical
        scan-splice topology rtl/sib_cell.v's `po ? nested_so : si` mux creates."""
        layout: list[_CellRef] = []
        for slot in self._slots:
            if slot.sib_po:
                layout.extend(_CellRef(slot, k) for k in range(slot.width))
            layout.append(_CellRef(slot, None))
        return layout

    @staticmethod
    def _read(ref: _CellRef) -> int:
        return ref.slot.sib_shift_ff if ref.bit is None else ref.slot.inst_shift_ff[ref.bit]

    @staticmethod
    def _write(ref: _CellRef, value: int) -> None:
        if ref.bit is None:
            ref.slot.sib_shift_ff = value
        else:
            ref.slot.inst_shift_ff[ref.bit] = value

    def shift(self, tdi: int) -> int:
        layout = self._live_layout()
        if not layout:
            return tdi
        values = [self._read(ref) for ref in layout]
        tdo = values[-1]
        incoming = tdi
        for ref, val in zip(layout, values):
            self._write(ref, incoming)
            incoming = val
        return tdo
