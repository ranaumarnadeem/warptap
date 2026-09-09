"""Static bit-layout arithmetic for SIB networks (implementation_plan.md §7 Stage 5, §3.3).
Pure functions of a :class:`~warptap.icl_model.PhysicalGraph` plus a *hypothetical* open-SIB
set -- "if exactly these SIBs were open, what would the live scan chain look like" -- with no
:class:`~warptap.sib_model.SibNetworkRegister` instance involved.

Mirrors ``sib_model.SibNetworkRegister._live_layout()``'s exact counting rule (a closed SIB
contributes 1 bit; an open SIB contributes its instrument's width bits then its own bit,
TDI-side-first) but as a *static* function of a caller-given open-set rather than a *live* read
of an already-constructed register's runtime ``sib_po`` state. ``PDLInterpreter.iApply`` needs
this static form specifically because phase 1's shift length must be computed from whatever
was open *before* this ``iApply`` -- before any register object reflecting the new state
exists.

Deliberately not shared with ``_live_layout()`` -- exactly two occurrences (that live read,
this static one) is the textbook "rule of three" case for duplication over premature sharing,
the same reasoning ``sib_insert.py`` already gives for duplicating ``_import_template`` rather
than importing it from ``bsr_insert.py``.
"""

from __future__ import annotations

from typing import Optional, Union

from warptap.icl_model import ChainSlot, PhysicalGraph, ScanArm, ScanMuxNode


def _normalize_open(open_set: Union[frozenset, dict]) -> dict:
    """Accepts either the legacy ``frozenset[str]`` (a name's presence means "open," matching
    a :class:`~warptap.icl_model.SibNode`'s own binary po -- normalized here to value ``1``)
    or the richer ``dict[str, int]`` a :class:`~warptap.icl_model.ScanMuxNode` needs (name ->
    the specific value committed/targeted). A backward-compatible widening, not a breaking
    migration: every existing SIB-only call site keeps passing a plain frozenset/set of names
    unchanged; only new mux-touching code needs to pass a dict."""
    if isinstance(open_set, dict):
        return open_set
    return {name: 1 for name in open_set}


def _require_leaf_or_nested(owner: str, instrument, nested: tuple) -> None:
    """Every leaf slot (no nested sub-network) needs a real instrument to make sense of --
    the genuinely-incomplete case :class:`~warptap.icl_model.validate_physical_graph` also
    rejects at construction time, re-checked here too rather than crashing on ``None.width``.
    A hierarchy slot (``nested`` populated) never reaches the instrument-shaped branch at all
    -- see ``layout_bit_length``/``compose_bits``'s own recursive branch. Shared between a
    :class:`~warptap.icl_model.SibNode` and a :class:`~warptap.icl_model.ScanArm` -- both have
    the identical instrument-xor-nested shape."""
    if instrument is None and not nested:
        raise NotImplementedError(
            f"sib_layout requires every leaf slot to gate a real instrument "
            f"({owner} has neither an instrument nor a nested network)"
        )


def _matching_arm(mux: ScanMuxNode, value: int) -> Optional[ScanArm]:
    """The arm claiming ``value``, if any -- ``None`` means ``value`` is implicit bypass (no
    declared arm matches), the direct generalization of a closed SibNode's own unmatched
    state."""
    return next((arm for arm in mux.arms if value in arm.values), None)


def layout_bit_length(graph: PhysicalGraph, open_sibs: Union[frozenset, dict]) -> int:
    """Total live scan-chain length if exactly ``open_sibs`` were open/targeted (accepts
    either shape -- see :func:`_normalize_open`) and every other slot in ``graph.chain`` (at
    any nesting depth) were closed/bypassing. A name in ``open_sibs`` that doesn't match any
    slot in ``graph`` is silently irrelevant (mirrors ``compose_bits``'s own tolerance -- both
    functions only ever care about membership tests against real slots).

    An open hierarchy SIB (no instrument, a nested sub-network instead) contributes its own
    sub-chain's length (recursively, by the same rule one level deeper) plus its own select
    bit -- a closed SIB's nested content isn't part of the live chain at all, regardless of
    depth, exactly like a closed leaf's instrument content isn't.

    A :class:`~warptap.icl_model.ScanMuxNode` contributes its own ``select_width`` bits
    always, plus -- if ``open_sibs``'s value for this mux matches a declared arm -- that arm's
    own recursive content length. This is always the *currently matched* arm (``open_sibs``'s
    own value), never some other arm: a genuine finding from the RTL spike
    (``tests/test_scan_mux_cell_cross_sim.py``), not an assumption -- switching a mux to a
    different arm in one round still has to carry the currently-matched arm's own content
    width as extra (dummy) cycles, exactly mirroring how closing an open SIB still carries
    that SIB's own soon-to-be-abandoned content width rather than 0."""
    open_map = _normalize_open(open_sibs)

    def _walk(chain: tuple[ChainSlot, ...]) -> int:
        total = 0
        for node in chain:
            if isinstance(node, ScanMuxNode):
                total += node.select_width
                value = open_map.get(node.mux_name)
                if value is not None:
                    arm = _matching_arm(node, value)
                    if arm is not None:
                        if arm.nested:
                            total += _walk(arm.nested)
                        else:
                            _require_leaf_or_nested(
                                f"ScanMux {node.mux_name!r}'s matched arm (value {value})",
                                arm.instrument, arm.nested,
                            )
                            total += arm.instrument.width
            elif node.sib_name in open_map:
                if node.nested:
                    total += _walk(node.nested) + 1
                else:
                    _require_leaf_or_nested(f"SIB {node.sib_name!r}", node.instrument, node.nested)
                    total += node.instrument.width + 1
            else:
                total += 1
        return total

    return _walk(graph.chain)


def compose_bits(
    graph: PhysicalGraph,
    open_now: Union[frozenset, dict],
    open_after: Union[frozenset, dict],
    *,
    target_sib: str | None = None,
    payload_value: int = 0,
) -> list[int]:
    """TDI-nearest-first bit list (same convention as ``SibNetworkRegister._live_layout()``:
    per slot in ``graph.chain`` order, nearest TDI first) sized against ``open_now`` -- what's
    *physically* open/targeted right now (accepts either shape -- see :func:`_normalize_open`),
    which fixes how many bits actually exist to shift through, exactly
    ``layout_bit_length(graph, open_now)`` bits long -- with each slot's own select bit(s)
    *value* set to represent ``open_after``: whatever the network's select state should read
    as once this shift's Update-DR commits.

    These two sets are the same value in the common case (a shift that only re-asserts an
    already-open path to deliver payload -- Stage 5's phase 2) but genuinely differ when this
    very shift is the one *changing* which SIBs/muxes are open (Stage 5's phase 1: ``open_now``
    is whatever a prior ``iApply`` left open, ``open_after`` is the new target path, so every
    previously-open sibling's select bit is deliberately fed 0 -- closing it -- while the new
    target's own ancestors' select bits are fed 1, regardless of whether they were in
    ``open_now`` at all).

    Per SIB slot:
    - Physically open, a hierarchy slot (``node.nested`` populated): contributes its own
      sub-chain's bits (recursively, same rule one level deeper), then its own select bit.
    - Physically open, a leaf slot: contributes its instrument's ``width`` content bits, then
      its own select bit. If this slot is ``target_sib``, content bits are the real payload
      (LSB first: bit ``k`` is ``(payload_value >> k) & 1``, nearest ``nested_si`` first --
      matching ``sib_insert.py``'s own per-bit wiring order); otherwise content bits are ``0``
      (don't-care -- ``bc1_shift_only.v`` has no update latch, so garbage shifted through a
      non-target instrument here has no lasting effect).
    - Physically closed (``sib_name not in open_now``): contributes just its own select bit --
      there is no content (instrument or nested) in the chain to shift through.
    - Either way, the select bit itself is ``1`` if ``sib_name in open_after`` else ``0``.

    Per :class:`~warptap.icl_model.ScanMuxNode` slot:
    - Content bits, if ``open_now``'s value matches a declared arm (the *currently matched*
      arm -- see ``layout_bit_length``'s own docstring for why, confirmed by the RTL spike):
      that arm's own recursive content (or ``0``s if it isn't ``target_sib``/doesn't contain
      it), same rules as a SIB leaf/hierarchy slot above. No content at all if unmatched
      (bypassing), exactly like a closed SIB.
    - Then ``select_width`` select-field bits encoding ``open_after``'s value for this mux (0
      if absent, i.e. bypass) -- **MSB first** in this position-ordered list, the reverse of
      the LSB-first instrument-content convention above. Confirmed via
      ``tap_ir.py``'s own documented "position order is the exact reverse of chronological
      feed order" rule: the select field is one shared internal shift register (mirrors
      ``rtl/tap_core.v``'s own ``ir_shift``), not N independently-wired 1-bit cells the way
      instrument content is, so it has its own, genuinely different bit-to-position mapping --
      not an inconsistency, a real structural difference confirmed against
      ``tests/test_scan_mux_cell_cross_sim.py``'s own passing stimulus.
    """
    open_now_map = _normalize_open(open_now)
    open_after_map = _normalize_open(open_after)

    def _leaf_content_bits(instrument, owner: str, is_target: bool) -> list[int]:
        _require_leaf_or_nested(owner, instrument, ())
        if is_target:
            return [(payload_value >> k) & 1 for k in range(instrument.width)]
        return [0] * instrument.width

    def _walk(chain: tuple[ChainSlot, ...]) -> list[int]:
        bits: list[int] = []
        for node in chain:
            if isinstance(node, ScanMuxNode):
                value = open_now_map.get(node.mux_name)
                arm = _matching_arm(node, value) if value is not None else None
                if arm is not None:
                    if arm.nested:
                        bits.extend(_walk(arm.nested))
                    else:
                        owner = f"ScanMux {node.mux_name!r}'s matched arm (value {value})"
                        is_target = node.mux_name == target_sib
                        bits.extend(_leaf_content_bits(arm.instrument, owner, is_target))
                target = open_after_map.get(node.mux_name, 0)
                bits.extend((target >> k) & 1 for k in range(node.select_width - 1, -1, -1))
            else:
                select_bit = 1 if node.sib_name in open_after_map else 0
                if node.sib_name in open_now_map:
                    if node.nested:
                        bits.extend(_walk(node.nested))
                    else:
                        is_target = node.sib_name == target_sib
                        bits.extend(
                            _leaf_content_bits(node.instrument, f"SIB {node.sib_name!r}", is_target)
                        )
                bits.append(select_bit)
        return bits

    return _walk(graph.chain)
