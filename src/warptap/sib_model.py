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
this reason -- ``True`` unconditionally at the top level (every top-level slot's own ``select``
is hardwired to constant 1), a parent's own ``sib_po`` (or, for a mux parent, whether its own
currently-matched arm is the one being walked) one level down.

**A mux arm generalizes this the same way ``rtl/scan_mux_cell.v`` does** (multi-arm ScanMux
plan Phase 0/3): ``sib_shift_ff``/``sib_po`` become ``shift_ff``/``po`` lists of
``select_width`` bits; a hierarchy arm's own gating uses ``arm_active`` (matched *before* this
edge, gating the whole recursive walk, mirroring ``nested_active = old_po_match & select``)
for capture/the live layout, and ``stays_matched`` (matched both *before and after* this edge,
mirroring ``nested_select``/``arm_select = old_po_match & new_po_match & select``) for gating a
WRITE leaf arm's own update-latch commit -- re-derived from Phase 0's confirmed RTL semantics,
not assumed by analogy to the binary case. A leaf arm's own instrument capture is unconditional
regardless of which arm is matched, exactly like a plain leaf SIB's (neither
``instrument_write.v`` nor ``bc1_shift_only.v`` gates capture_dr on ``select`` at all).
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Union

from warptap.icl_model import InstrumentDirection, PhysicalGraph, ScanArm, ScanMuxNode, SibNode


def _value_of(bits: List[int]) -> int:
    """``bits[k]`` = bit ``k`` (LSB at index 0), matching how ``inst_shift_ff``/``inst_po``
    are already indexed for instrument content."""
    return sum(b << k for k, b in enumerate(bits))


def _make_chain(nodes: tuple) -> List[Union["_SlotState", "_MuxSlotState"]]:
    return [_MuxSlotState(n) if isinstance(n, ScanMuxNode) else _SlotState(n) for n in nodes]


class _SlotState:
    __slots__ = (
        "sib_name", "sib_shift_ff", "sib_po",
        "children",  # list[_SlotState | _MuxSlotState] for a hierarchy slot, None for a leaf
        "width", "capture_value", "direction", "inst_shift_ff", "inst_po",  # leaf-only
    )

    def __init__(self, node: SibNode):
        self.sib_name = node.sib_name
        self.sib_shift_ff = 0
        self.sib_po = 0
        if node.nested:
            self.children: Optional[List] = _make_chain(node.nested)
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


class _ArmState:
    """One arm of a ``_MuxSlotState`` -- the same leaf-xor-hierarchy duality as ``_SlotState``,
    minus its own independent open/closed state: an arm's own "liveness" is entirely a
    function of the enclosing mux's current ``po`` decode, never tracked here directly."""

    __slots__ = (
        "values", "children",  # list[_SlotState | _MuxSlotState] for a hierarchy arm, else None
        "width", "capture_value", "direction", "inst_shift_ff", "inst_po",  # leaf-only
    )

    def __init__(self, arm: ScanArm):
        self.values = arm.values
        if arm.nested:
            self.children: Optional[List] = _make_chain(arm.nested)
            self.width = self.capture_value = self.direction = None
            self.inst_shift_ff = self.inst_po = None
        elif arm.instrument is not None:
            self.children = None
            self.width = arm.instrument.width
            self.capture_value = arm.instrument.capture_value
            self.direction = arm.instrument.direction
            self.inst_shift_ff = [0] * self.width
            self.inst_po = [0] * self.width
        else:
            raise NotImplementedError(
                f"SibNetworkRegister requires every ScanMux arm to gate an instrument or a "
                f"nested network (an arm of mux {arm!r} has neither)"
            )


class _MuxSlotState:
    __slots__ = ("mux_name", "select_width", "shift_ff", "po", "arms")

    def __init__(self, node: ScanMuxNode):
        self.mux_name = node.mux_name
        self.select_width = node.select_width
        self.shift_ff = [0] * node.select_width
        self.po = [0] * node.select_width
        self.arms = [_ArmState(arm) for arm in node.arms]


class _CellRef(NamedTuple):
    slot: object  # _SlotState, _ArmState, or _MuxSlotState -- see _read/_write
    bit: Optional[int]  # index into the owning object's own bit array; None only for a
                          # _SlotState's own single sib_shift_ff (never for a mux or an arm)


class SibNetworkRegister:
    """Models the whole inserted network as one ``DataRegister``, matching how ``tap_core.v``
    sees it: a single serial path from TDI, through every slot in ``graph.chain`` order
    (TDI-side-first, recursing into a hierarchy slot's own nested chain the same way -- or, for
    a mux, into its currently-matched arm's own content -- to ``external_dr_tdo``."""

    def __init__(self, graph: PhysicalGraph):
        self._slots: List[Union[_SlotState, _MuxSlotState]] = _make_chain(graph.chain)

    def reset(self) -> None:
        """Zero every slot's and instrument bit-cell's state, matching rtl/sib_cell.v's,
        rtl/scan_mux_cell.v's, rtl/bc1_shift_only.v's, and rtl/instrument_write.v's own
        `trst_n` reset behavior -- unconditional at every depth/every arm, since `trst_n`
        reaches every cell directly, ungated by any `select`."""
        self._reset_chain(self._slots)

    def _reset_chain(self, slots: List) -> None:
        for slot in slots:
            if isinstance(slot, _MuxSlotState):
                slot.shift_ff = [0] * slot.select_width
                slot.po = [0] * slot.select_width
                for arm in slot.arms:
                    if arm.children is not None:
                        self._reset_chain(arm.children)
                    else:
                        arm.inst_shift_ff = [0] * arm.width
                        arm.inst_po = [0] * arm.width
            else:
                slot.sib_shift_ff = 0
                slot.sib_po = 0
                if slot.children is not None:
                    self._reset_chain(slot.children)
                else:
                    slot.inst_shift_ff = [0] * slot.width
                    slot.inst_po = [0] * slot.width

    def capture(self) -> None:
        """A slot's own self-capture (``sib_shift_ff <= sib_po``, or a mux's whole
        ``shift_ff <= po`` list) happens only while its own ``select`` is live
        (``parent_open``, see module docstring) -- gated for a nested slot/arm, unconditional
        at the top level. A leaf instrument's own capture_dr response is unconditional
        regardless of depth or which arm is matched: neither ``instrument_write.v`` nor
        ``bc1_shift_only.v`` gates capture_dr on ``select`` at all (a named limitation, not a
        bug: see sib_insert.py's module docstring).

        READ slots capture ``capture_value`` (implementation_plan.md §7 Stage 9: for a
        signal-bound READ instrument, the test author sets this to whatever the real host
        signal is expected to hold at capture time -- no separate "live signal" concept
        needed here, matching rtl/bc1_shift_only.v's own fixed-``pi``-at-capture-time shape).
        WRITE slots self-capture ``inst_po`` instead, mirroring rtl/instrument_write.v's own
        ``shift_ff <= po`` -- what's read back is whatever was last committed, not a stub."""
        self._capture_chain(self._slots, parent_open=True)

    def _capture_chain(self, slots: List, *, parent_open: bool) -> None:
        for slot in slots:
            if isinstance(slot, _MuxSlotState):
                if parent_open:
                    slot.shift_ff = list(slot.po)
                matched_value = _value_of(slot.po)
                for arm in slot.arms:
                    arm_active = parent_open and matched_value in arm.values
                    if arm.children is not None:
                        self._capture_chain(arm.children, parent_open=arm_active)
                    elif arm.direction is InstrumentDirection.WRITE:
                        for k in range(arm.width):
                            arm.inst_shift_ff[k] = arm.inst_po[k]
                    else:
                        for k in range(arm.width):
                            arm.inst_shift_ff[k] = (arm.capture_value >> k) & 1
            else:
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
        """A slot's own ``sib_po <= sib_shift_ff`` commit (or a mux's whole ``po <= shift_ff``
        list) happens only while its own ``select`` is live (``parent_open``), matching
        ``sib_cell.v``'s/``scan_mux_cell.v``'s own ``if(select) ... if(update_dr) po<=shift_ff``.
        A WRITE leaf's instrument also commits its shifted-in bits to ``inst_po`` here,
        mirroring ``instrument_write.v``'s ``po <= shift_ff`` -- gated by ``stays_open``
        (``parent_open & sib_po & sib_shift_ff`` for a SIB, or ``parent_open & arm_matched_
        before & arm_matched_after`` for a mux arm -- Phase 0's confirmed ``arm_select``
        semantics): this slot's own select live, AND open/matched both *before* and *after*
        this edge. ``po``/matched-before alone would still let the very edge that *closes* a
        SIB (or *switches a mux away from* an arm) commit garbage -- phase 1's retargeting
        shift feeds every non-target slot's content 0 as "don't care, no lasting effect"
        (sib_layout.compose_bits's own documented assumption, true only for an instrument with
        no update latch). ``shift_ff``/the about-to-commit value is exactly what's about to
        become the new po this same edge, so requiring both makes this true only while a slot
        stays open/an arm stays matched across the edge -- never while it's opening, closing,
        or switching (see rtl/instrument_write.v's own docs for the full story). A READ slot's
        instrument has no update-latch at all (rtl/bc1_shift_only.v has no ``update_dr`` port)
        -- nothing written via iWrite to a READ instrument ever persists, unchanged from
        Stage 5.

        A nested slot's/arm's own children see this slot's PRE-edge state (``old_po``/
        ``matched_before`` below), not the value just committed this same edge -- every
        flip-flop in the real RTL reads pre-edge combinational values simultaneously; a
        child's own ``select`` is no different."""
        self._update_chain(self._slots, parent_open=True)

    def _update_chain(self, slots: List, *, parent_open: bool) -> None:
        for slot in slots:
            if isinstance(slot, _MuxSlotState):
                matched_before = _value_of(slot.po)
                matched_after = _value_of(slot.shift_ff)
                if parent_open:
                    slot.po = list(slot.shift_ff)
                for arm in slot.arms:
                    arm_matched_before = parent_open and matched_before in arm.values
                    stays_matched = arm_matched_before and matched_after in arm.values
                    if arm.children is not None:
                        self._update_chain(arm.children, parent_open=arm_matched_before)
                    elif arm.direction is InstrumentDirection.WRITE and stays_matched:
                        for k in range(arm.width):
                            arm.inst_po[k] = arm.inst_shift_ff[k]
            else:
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
        """TDI-side-first list of every currently-live flip-flop: a closed/bypassing slot
        contributes just its own bit(s); an open leaf contributes its instrument's bits (index
        order, nearest nested_si first) then its own bit; an open hierarchy slot recurses into
        its own nested chain (same rule, one level deeper) then its own bit; a mux contributes
        its currently-matched arm's own content (recursively, same rules) then its own
        select-field bits -- mirrors the physical scan-splice topology rtl/sib_cell.v's
        `po ? nested_so : si` mux (and rtl/scan_mux_cell.v's own N-arm generalization) creates
        at any nesting depth."""
        return self._live_layout_chain(self._slots)

    def _live_layout_chain(self, slots: List) -> list[_CellRef]:
        layout: list[_CellRef] = []
        for slot in slots:
            if isinstance(slot, _MuxSlotState):
                matched_value = _value_of(slot.po)
                arm = next((a for a in slot.arms if matched_value in a.values), None)
                if arm is not None:
                    if arm.children is not None:
                        layout.extend(self._live_layout_chain(arm.children))
                    else:
                        layout.extend(_CellRef(arm, k) for k in range(arm.width))
                # MSB first (descending k): rtl/scan_mux_cell.v's own shift register loads
                # fresh content at shift_ff[SEL_WIDTH-1] every cycle, cascading toward bit 0
                # (mirrors rtl/tap_core.v's ir_shift) -- the opposite of instrument content's
                # own ascending/LSB-first convention just above, which is N independently
                # chained 1-bit cells, not one shared register. Getting this backwards was
                # caught directly by this file's own tests (test_sib_model_scan_mux.py),
                # not just reasoned here -- see that file's own docstring.
                layout.extend(_CellRef(slot, k) for k in range(slot.select_width - 1, -1, -1))
            else:
                if slot.sib_po:
                    if slot.children is not None:
                        layout.extend(self._live_layout_chain(slot.children))
                    else:
                        layout.extend(_CellRef(slot, k) for k in range(slot.width))
                layout.append(_CellRef(slot, None))
        return layout

    @staticmethod
    def _live_so(slots: List) -> int:
        """What the given chain's own final `so` (feeding whatever's next -- a gating SIB's
        own nested_so, or a mux arm's own arm_so[k]) currently reads as, without building a
        full ``_live_layout_chain`` list -- mirrors rtl/scan_mux_cell.v's own `so` assign
        exactly: the matched arm's own relayed so while matched, else the register's own
        LSB. Needed because a mux's own select field is ONE shared register (unlike an
        instrument's N independently-chained bit cells): `so` is always wired to bit 0 (or
        the matched arm's relay) specifically, never `shift_ff[k]` for any other k -- getting
        this backwards (giving every select-field bit position independent, unconditional
        read access to its own `shift_ff[k]`) was a real bug this method fixes, caught by a
        real RTL cross-sim mismatch (tests/test_sib_insert_scan_mux_cross_sim.py), not just
        reasoned here: Phase 3's own pure-Python tests happened to never exercise a case
        where the distinction was observable."""
        last = slots[-1]
        if isinstance(last, _MuxSlotState):
            matched_value = _value_of(last.po)
            matched_arm = next((a for a in last.arms if matched_value in a.values), None)
            if matched_arm is not None:
                if matched_arm.children is not None:
                    return SibNetworkRegister._live_so(matched_arm.children)
                return matched_arm.inst_shift_ff[matched_arm.width - 1]
            return last.shift_ff[0]
        # A SibNode's own `so` is always its own sib_shift_ff (rtl/sib_cell.v's `assign
        # so = shift_ff`, unconditional) -- REGARDLESS of what's nested inside it, leaf or
        # deeper. Whatever's nested only ever affects this SIB's own shift_ff indirectly,
        # through the one-cycle relay (`shift_ff <= po ? nested_so : si`); it's never this
        # SIB's own externally-visible `so` directly.
        return last.sib_shift_ff

    @staticmethod
    def _read(ref: _CellRef) -> int:
        if isinstance(ref.slot, _MuxSlotState):
            if ref.bit == 0:
                # The only select-field position that's ever externally observable (rtl/
                # scan_mux_cell.v's own `so`) -- every other bit is purely internal,
                # cascaded-through-but-never-read-directly, so it's unaffected.
                return SibNetworkRegister._live_so([ref.slot])
            return ref.slot.shift_ff[ref.bit]
        if ref.bit is None:
            return ref.slot.sib_shift_ff
        return ref.slot.inst_shift_ff[ref.bit]

    @staticmethod
    def _write(ref: _CellRef, value: int) -> None:
        if isinstance(ref.slot, _MuxSlotState):
            ref.slot.shift_ff[ref.bit] = value
        elif ref.bit is None:
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
