"""Static PDL retargeting for SIB networks (implementation_plan.md §7 Stage 4, §3.3). Answers
exactly one question: which SIBs/muxes must be OPEN (and, for a mux, which arm) to reach a
named instrument. Every other slot in the network stays CLOSED/bypassing during the resulting
phase-1 Capture-Shift-Update sequence.

This is a plain static-reachability question, not dynamic reachability -- IEEE 1687's
``existPr``/conditional-instrument-presence mechanism is a provably bounded-horizon SAT-class
problem (implementation_plan.md §1's confirmed finding) and is out of scope here; v1's
canonical single-control-bit SIBs and N-arm ScanMuxes never produce that shape, so a plain DFS
is correct and sufficient."""

from __future__ import annotations

from typing import NamedTuple, Union

from warptap.errors import WarptapError
from warptap.icl_model import ChainSlot, PhysicalGraph, ScanMuxNode


class SibRetargetError(WarptapError):
    """Raised when no path to the named instrument (or named slot) exists in this network."""


class PathStep(NamedTuple):
    """One ancestor on the way to a target: ``name`` (a SIB's ``sib_name`` or a mux's
    ``mux_name``) plus the ``value`` that ancestor must carry -- ``1`` for a SIB (its own
    binary po, matching the pre-ScanMux convention exactly), the specific arm's value for a
    mux. Generalizes the plain ``tuple[str, ...]`` this codebase used before ScanMuxNode
    existed -- a real breaking change to :func:`open_path_to`/:func:`path_to_sib_name`'s
    return shape, accepted as in-scope (this exact function's contract already evolved once
    before, during the nested-SIB work)."""

    name: str
    value: int


def _normalize_open(open_set: Union[frozenset, dict]) -> dict:
    """Same widening as ``sib_layout._normalize_open`` (duplicated, not imported -- exactly
    two occurrences, this codebase's own "rule of three" threshold for sharing): a plain
    ``frozenset[str]`` of names (implicit value ``1``, matching a SIB's own binary po) or a
    richer ``dict[str, int]`` naming a specific value per entry -- needed here so a caller can
    target a :class:`~warptap.icl_model.ScanMuxNode` with a specific arm directly, not just a
    plain SIB name."""
    if isinstance(open_set, dict):
        return open_set
    return {name: 1 for name in open_set}


def _search(
    node: ChainSlot, instrument_name: str, path: tuple[PathStep, ...]
) -> tuple[PathStep, ...] | None:
    if isinstance(node, ScanMuxNode):
        for arm in node.arms:
            value = arm.values[0]  # v1: exactly one value per arm (validated upstream)
            here = path + (PathStep(node.mux_name, value),)
            if arm.instrument is not None and arm.instrument.name == instrument_name:
                return here
            for child in arm.nested:
                found = _search(child, instrument_name, here)
                if found is not None:
                    return found
        return None
    here = path + (PathStep(node.sib_name, 1),)
    if node.instrument is not None and node.instrument.name == instrument_name:
        return here
    for child in node.nested:
        found = _search(child, instrument_name, here)
        if found is not None:
            return found
    return None


def open_path_to(
    graph: PhysicalGraph,
    instrument_name: str,
    *,
    network_configuration: frozenset[str] = frozenset(),
) -> tuple[PathStep, ...]:
    """The ancestor chain that must be OPEN (and, for any mux ancestor, the specific arm) to
    reach ``instrument_name``, root-to-leaf (index 0 nearest TDI, the same top-level order
    ``graph.chain`` itself uses; the last element always names the leaf slot that directly
    gates the instrument). For a directly-gated top-level instrument on a plain SibNode (the
    only shape possible before nested/ScanMux networks), this is exactly
    ``(PathStep(that_sib_name, 1),)``.

    ``network_configuration`` (implementation_plan.md §3.3) names whatever SIBs are
    *currently* open, ahead of a future SAT-based ``existPr`` extension whose answer would
    genuinely depend on it (dynamic reachability, §1's confirmed finding). The static
    ancestor-chain answer never depends on it -- accepted and ignored here -- so this is a
    pure signature seam, not yet load-bearing for this function's own return value. (It *is*
    load-bearing elsewhere: ``pdl_interpreter.PDLInterpreter`` tracks the same "currently
    open" state itself to size phase 1 of each ``iApply``, per implementation_plan.md §7
    Stage 5 §3.)

    Raises :class:`SibRetargetError`, naming ``instrument_name``, if no slot in ``graph``
    gates an instrument by that name."""
    del network_configuration  # v1: accepted for the future existPr seam, unused today
    for node in graph.chain:
        found = _search(node, instrument_name, ())
        if found is not None:
            return found
    raise SibRetargetError(f"no path to instrument {instrument_name!r} in this network")


def _search_by_sib_name(
    node: ChainSlot, sib_name: str, path: tuple[PathStep, ...], target_value: int
) -> tuple[PathStep, ...] | None:
    if isinstance(node, ScanMuxNode):
        if node.mux_name == sib_name:
            return path + (PathStep(node.mux_name, target_value),)
        for arm in node.arms:
            value = arm.values[0]
            here = path + (PathStep(node.mux_name, value),)
            for child in arm.nested:
                found = _search_by_sib_name(child, sib_name, here, target_value)
                if found is not None:
                    return found
        return None
    here = path + (PathStep(node.sib_name, 1),)
    if node.sib_name == sib_name:
        return here
    for child in node.nested:
        found = _search_by_sib_name(child, sib_name, here, target_value)
        if found is not None:
            return found
    return None


def path_to_sib_name(
    graph: PhysicalGraph, sib_name: str, *, target_value: int = 1
) -> tuple[PathStep, ...]:
    """Like :func:`open_path_to`, but resolves a slot's own name directly (SIB, leaf or
    hierarchy, or a :class:`~warptap.icl_model.ScanMuxNode`) rather than an instrument it
    gates -- ``sib_overshift.py``'s connectivity checks target slots by name, which may name a
    hierarchy node directly (checking a whole nested subtree's reachability) rather than
    always naming a leaf instrument's own gating SIB.

    ``target_value`` is the value the *resolved target itself* should carry -- meaningless for
    a SIB (whose own step is always value ``1``, the only real "open" state) but the only way
    to say *which arm* when ``sib_name`` names a mux directly: a mux has no single canonical
    "open" value the way a SIB's po does, since the whole point is choosing among >= 2 arms,
    so this can't be inferred the way an *ancestor* mux's own value is (found by trying each
    arm in turn while searching for ``sib_name`` deeper inside it). Ignored when ``sib_name``
    resolves to a SIB.

    Raises :class:`SibRetargetError`, naming ``sib_name``, if no slot in ``graph`` has that
    name."""
    for node in graph.chain:
        found = _search_by_sib_name(node, sib_name, (), target_value)
        if found is not None:
            return found
    raise SibRetargetError(f"no SIB named {sib_name!r} in this network")


def stage_open_sequence(
    graph: PhysicalGraph, target_open: Union[frozenset, dict]
) -> list[dict[str, int]]:
    """Splits an arbitrary (possibly multi-branch, arbitrary-depth) target open-map into the
    ordered sequence of ``open_after`` states needed to physically reach it -- one entry per
    required Shift-DR/Update-DR round. Accepts either shape -- see :func:`_normalize_open` --
    a plain ``frozenset[str]`` of names (implicit value ``1`` each) or a ``dict[str, int]``
    naming a specific arm value for any mux entries.

    A closed SIB's nested content isn't physically part of the live scan chain yet (confirmed
    against real RTL, ``tests/test_sib_cell_nested_cross_sim.py``), so a node at tree-depth
    ``d`` can be newly opened no earlier than round ``d`` -- its own ancestors must already be
    physically open/matched. Round ``r``'s ``open_after`` is exactly every required node
    (every name in ``target_open`` plus every one of its own ancestors, each with its own
    resolved value) at depth ``<= r``.

    Also correct for closing: a currently-open slot that isn't in the final required set is
    simply never included at any round (closing has no physical-existence constraint, unlike
    opening), so it's closed/bypassed as early as round 1.

    If two different ``target_open`` entries share a common mux ancestor and (inconsistently)
    imply different values for it, the last one processed silently wins -- not detected or
    rejected. This mirrors the pre-ScanMux code's own lack of any such check (impossible to
    trigger there, since every ancestor was always value ``1``); a real conflict-detection
    pass is not yet needed by any caller.

    Returns ``[{}]`` (a single all-closed round) for an empty ``target_open``. The caller
    pairs round ``i``'s ``open_now`` with round ``i - 1``'s result (or the real currently-open
    state for round 0) when calling ``layout_bit_length``/``compose_bits``."""
    open_map = _normalize_open(target_open)
    if not open_map:
        return [{}]
    paths = {
        name: path_to_sib_name(graph, name, target_value=value)
        for name, value in open_map.items()
    }
    depth_and_value_of: dict[str, tuple[int, int]] = {}
    for path in paths.values():
        for depth, step in enumerate(path, start=1):
            depth_and_value_of[step.name] = (depth, step.value)
    max_depth = max(depth for depth, _value in depth_and_value_of.values())
    return [
        {name: value for name, (depth, value) in depth_and_value_of.items() if depth <= r}
        for r in range(1, max_depth + 1)
    ]
