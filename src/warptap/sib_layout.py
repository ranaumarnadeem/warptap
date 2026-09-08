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

from warptap.icl_model import PhysicalGraph


def _require_instrument(sib_name: str, instrument) -> None:
    """Every leaf slot (no nested sub-network) needs a real instrument to make sense of --
    the genuinely-incomplete case :class:`~warptap.icl_model.validate_physical_graph` also
    rejects at construction time, re-checked here too rather than crashing on ``None.width``.
    A hierarchy slot (``node.nested`` populated) never reaches this function at all -- see
    ``layout_bit_length``/``compose_bits``'s own recursive branch."""
    if instrument is None:
        raise NotImplementedError(
            f"sib_layout requires every leaf slot to gate a real instrument "
            f"({sib_name!r} has neither an instrument nor a nested network)"
        )


def layout_bit_length(graph: PhysicalGraph, open_sibs: frozenset[str]) -> int:
    """Total live scan-chain length if exactly ``open_sibs`` were open and every other SIB
    in ``graph.chain`` (at any nesting depth) were closed. A name in ``open_sibs`` that
    doesn't match any SIB in ``graph`` is silently irrelevant (mirrors ``compose_bits``'s own
    tolerance -- both functions only ever care about membership tests against real slots).

    An open hierarchy slot (no instrument, a nested sub-network instead) contributes its own
    sub-chain's length (recursively, by the same rule one level deeper) plus its own select
    bit -- a closed SIB's nested content isn't part of the live chain at all, regardless of
    depth, exactly like a closed leaf's instrument content isn't."""

    def _walk(chain: tuple) -> int:
        total = 0
        for node in chain:
            if node.sib_name in open_sibs:
                if node.nested:
                    total += _walk(node.nested) + 1
                else:
                    _require_instrument(node.sib_name, node.instrument)
                    total += node.instrument.width + 1
            else:
                total += 1
        return total

    return _walk(graph.chain)


def compose_bits(
    graph: PhysicalGraph,
    open_now: frozenset[str],
    open_after: frozenset[str],
    *,
    target_sib: str | None = None,
    payload_value: int = 0,
) -> list[int]:
    """TDI-nearest-first bit list (same convention as ``SibNetworkRegister._live_layout()``:
    per slot in ``graph.chain`` order, nearest TDI first) sized against ``open_now`` -- the
    SIBs *physically* open right now, which fixes how many bits actually exist to shift
    through, exactly ``layout_bit_length(graph, open_now)`` bits long -- with each slot's own
    select-bit *value* set to represent ``open_after``: whatever the network's select state
    should read as once this shift's Update-DR commits.

    These two sets are the same value in the common case (a shift that only re-asserts an
    already-open path to deliver payload -- Stage 5's phase 2) but genuinely differ when this
    very shift is the one *changing* which SIBs are open (Stage 5's phase 1: ``open_now`` is
    whatever a prior ``iApply`` left open, ``open_after`` is the new target path, so every
    previously-open sibling's select bit is deliberately fed 0 -- closing it -- while the new
    target's own ancestors' select bits are fed 1, regardless of whether they were in
    ``open_now`` at all).

    Per slot:
    - Physically open, a hierarchy slot (``node.nested`` populated): contributes its own
      sub-chain's bits (recursively, same rule one level deeper), then its own select bit.
    - Physically open, a leaf slot: contributes its instrument's ``width`` content bits, then
      its own select bit. If this slot is ``target_sib``, content bits are the real payload
      (LSB first: bit ``k`` is ``(payload_value >> k) & 1``, nearest ``nested_si`` first --
      matching ``sib_insert.py``'s own per-bit wiring order); otherwise content bits are ``0``
      (don't-care -- ``bc1_shift_only.v`` has no update latch, so garbage shifted through a
      non-target instrument here has no lasting effect).
    - Physically closed (``sib_name not in open_now``), either kind: contributes just its own
      select bit -- there is no content (instrument or nested) in the chain to shift through.
    - Either way, the select bit itself is ``1`` if ``sib_name in open_after`` else ``0``.
    """

    def _walk(chain: tuple) -> list[int]:
        bits: list[int] = []
        for node in chain:
            select_bit = 1 if node.sib_name in open_after else 0
            if node.sib_name in open_now:
                if node.nested:
                    bits.extend(_walk(node.nested))
                else:
                    _require_instrument(node.sib_name, node.instrument)
                    if node.sib_name == target_sib:
                        bits.extend((payload_value >> k) & 1 for k in range(node.instrument.width))
                    else:
                        bits.extend([0] * node.instrument.width)
            bits.append(select_bit)
        return bits

    return _walk(graph.chain)
