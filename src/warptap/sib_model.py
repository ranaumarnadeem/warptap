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

**A nested slot's own ``select`` is gated by its PARENT's ``sib_po`` specifically, not
``sib_po & sib_shift_ff``** -- a real, empirically-found distinction (see
``rtl/sib_cell.v``'s own ``nested_active`` vs ``nested_select`` outputs, and
``tests/test_sib_insert_nested_cross_sim.py``): unlike ``instrument_write.v``'s ``select``
(which gates only its update-latch commit), ``sib_cell.v``'s own ``select`` gates its WHOLE
always-block (capture and shift too), so it must stay asserted for a parent's entire open
window, not flicker with the parent's own ``shift_ff`` while it mirrors varying content
mid-shift. Every recursive method below threads a ``parent_open`` flag downward for exactly
this reason -- ``True`` unconditionally at the top level (every top-level SIB's own ``select``
is hardwired to constant 1), a parent's own ``sib_po`` one level down.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional

from warptap.icl_model import InstrumentDirection, PhysicalGraph, SibNode


class _SlotState:
    __slots__ = (
        "sib_name", "sib_shift_ff", "sib_po",
        "children",  # list[_SlotState] for a hierarchy slot, None for a leaf
        "width", "capture_value", "direction", "inst_shift_ff", "inst_po",  # leaf-only
    )

    def __init__(self, node: SibNode):
        self.sib_name = node.sib_name
        self.sib_shift_ff = 0
        self.sib_po = 0
        if node.nested:
            self.children: Optional[List["_SlotState"]] = [_SlotState(c) for c in node.nested]
            self.width = self.capture_value = self.direction = None
            self.inst_shift_ff = self.inst_po = None
        elif node.instrument is not None:
            self.children = None
            self.width = node.instrument.width
            self.capture_value = node.instrument.capture_value
            self.direction = node.instrument.direction
            self.inst_shift_ff = [0] * self.width
            self.inst_po = [0] * self.width  # WRITE-direction only; unused (stays 0) for READ
        else:
            raise NotImplementedError(
                f"SibNetworkRegister requires every SIB to gate an instrument or a nested "
                f"network ({node.sib_name!r} has neither)"
            )


class _CellRef(NamedTuple):
    slot: _SlotState
    bit: Optional[int]  # index into slot.inst_shift_ff, or None for the SIB's own bit


class SibNetworkRegister:
    """Models the whole inserted network as one ``DataRegister``, matching how ``tap_core.v``
    sees it: a single serial path from TDI, through every slot in ``graph.chain`` order
    (TDI-side-first, recursing into a hierarchy slot's own nested chain the same way), to
    ``external_dr_tdo``."""

    def __init__(self, graph: PhysicalGraph):
        self._slots = [_SlotState(node) for node in graph.chain]

    def reset(self) -> None:
        """Zero every SIB's and instrument bit-cell's state, matching rtl/sib_cell.v's,
        rtl/bc1_shift_only.v's, and rtl/instrument_write.v's own `trst_n` reset behavior --
        unconditional at every depth, since `trst_n` reaches every cell directly, ungated by
        any `select`."""
        self._reset_chain(self._slots)

    def _reset_chain(self, slots: List[_SlotState]) -> None:
        for slot in slots:
            slot.sib_shift_ff = 0
            slot.sib_po = 0
            if slot.children is not None:
                self._reset_chain(slot.children)
            else:
                slot.inst_shift_ff = [0] * slot.width
                slot.inst_po = [0] * slot.width

    def capture(self) -> None:
        """A slot's own self-capture (``sib_shift_ff <= sib_po``) happens only while its own
        ``select`` is live (``parent_open``, see module docstring) -- gated for a nested slot,
        unconditional at the top level. A leaf instrument's own capture_dr response is
        unconditional regardless of depth: neither ``instrument_write.v`` nor
        ``bc1_shift_only.v`` gates capture_dr on ``select`` at all (a named limitation, not a
        bug: see sib_insert.py's module docstring).

        READ slots capture ``capture_value`` (implementation_plan.md §7 Stage 9: for a
        signal-bound READ instrument, the test author sets this to whatever the real host
        signal is expected to hold at capture time -- no separate "live signal" concept
        needed here, matching rtl/bc1_shift_only.v's own fixed-``pi``-at-capture-time shape).
        WRITE slots self-capture ``inst_po`` instead, mirroring rtl/instrument_write.v's own
        ``shift_ff <= po`` -- what's read back is whatever was last committed, not a stub."""
        self._capture_chain(self._slots, parent_open=True)

    def _capture_chain(self, slots: List[_SlotState], *, parent_open: bool) -> None:
        for slot in slots:
            if parent_open:
                slot.sib_shift_ff = slot.sib_po
            if slot.children is not None:
                self._capture_chain(slot.children, parent_open=slot.sib_po)
            else:
                if slot.direction is InstrumentDirection.WRITE:
                    for k in range(slot.width):
                        slot.inst_shift_ff[k] = slot.inst_po[k]
                else:
                    for k in range(slot.width):
                        slot.inst_shift_ff[k] = (slot.capture_value >> k) & 1

    def update(self) -> None:
        """A slot's own ``sib_po <= sib_shift_ff`` commit happens only while its own
        ``select`` is live (``parent_open``), matching ``sib_cell.v``'s own
        ``if(select) ... if(update_dr) po<=shift_ff``. A WRITE leaf's instrument also commits
        its shifted-in bits to ``inst_po`` here, mirroring ``instrument_write.v``'s
        ``po <= shift_ff`` -- gated by ``stays_open`` (``parent_open & sib_po & sib_shift_ff``:
        this slot's own select live, AND open both *before* and *after* this edge), mirroring
        ``nested_select = po & shift_ff & select``'s real combinational timing in RTL
        (rtl/sib_cell.v). ``po`` alone would still let the very edge that *closes* this slot
        (``po`` 1->0) commit garbage -- phase 1's retargeting shift feeds every non-target
        slot's content 0 as "don't care, no lasting effect" (sib_layout.compose_bits's own
        documented assumption, true only for an instrument with no update latch). ``shift_ff``
        is exactly the value about to become the new ``sib_po`` this same edge, so requiring
        both makes this true only while a slot stays open across the edge -- never while it's
        opening or closing (see rtl/instrument_write.v's own docs for the full story). A READ
        slot's instrument has no update-latch at all (rtl/bc1_shift_only.v has no
        ``update_dr`` port) -- nothing written via iWrite to a READ instrument ever persists,
        unchanged from Stage 5.

        A nested slot's own children see this slot's PRE-edge ``sib_po`` (``old_po`` below),
        not the value just committed this same edge -- every flip-flop in the real RTL reads
        pre-edge combinational values simultaneously; a child's own ``select`` is no
        different."""
        self._update_chain(self._slots, parent_open=True)

    def _update_chain(self, slots: List[_SlotState], *, parent_open: bool) -> None:
        for slot in slots:
            old_po = slot.sib_po
            stays_open = parent_open and old_po and slot.sib_shift_ff
            if parent_open:
                slot.sib_po = slot.sib_shift_ff
            if slot.children is not None:
                self._update_chain(slot.children, parent_open=parent_open and old_po)
            else:
                if slot.direction is InstrumentDirection.WRITE and stays_open:
                    for k in range(slot.width):
                        slot.inst_po[k] = slot.inst_shift_ff[k]

    def _live_layout(self) -> list[_CellRef]:
        """TDI-side-first list of every currently-live flip-flop: a closed slot contributes
        just its own bit; an open leaf contributes its instrument's bits (index order,
        nearest nested_si first) then its own bit; an open hierarchy slot recurses into its
        own nested chain (same rule, one level deeper) then its own bit -- mirrors the
        physical scan-splice topology rtl/sib_cell.v's `po ? nested_so : si` mux creates at
        any nesting depth."""
        return self._live_layout_chain(self._slots)

    def _live_layout_chain(self, slots: List[_SlotState]) -> list[_CellRef]:
        layout: list[_CellRef] = []
        for slot in slots:
            if slot.sib_po:
                if slot.children is not None:
                    layout.extend(self._live_layout_chain(slot.children))
                else:
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
