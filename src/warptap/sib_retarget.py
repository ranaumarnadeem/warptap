"""Static PDL retargeting for SIB networks (implementation_plan.md §7 Stage 4, §3.3). Answers
exactly one question: which SIBs must be OPEN to reach a named instrument. Every other SIB in
the network stays CLOSED during the resulting phase-1 Capture-Shift-Update sequence.

This is a plain static-reachability question, not dynamic reachability -- IEEE 1687's
``existPr``/conditional-instrument-presence mechanism is a provably bounded-horizon SAT-class
problem (implementation_plan.md §1's confirmed finding) and is out of scope here; v1's
canonical single-control-bit SIBs never produce that shape, so a plain DFS is correct and
sufficient."""

from __future__ import annotations

from warptap.errors import WarptapError
from warptap.icl_model import PhysicalGraph, SibNode


class SibRetargetError(WarptapError):
    """Raised when no path to the named instrument exists in this network."""


def _search(node: SibNode, instrument_name: str, path: tuple[str, ...]) -> tuple[str, ...] | None:
    here = path + (node.sib_name,)
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
) -> tuple[str, ...]:
    """The SIB ancestor chain that must be OPEN to reach ``instrument_name``, root-to-leaf
    (index 0 nearest TDI, the same top-level order ``graph.chain`` itself uses; the last
    element is always the leaf SIB that directly gates the instrument). For a directly-gated
    top-level instrument (the only shape possible before nested SIB-gating-SIB networks), this
    is exactly ``(that_sib_name,)``.

    ``network_configuration`` (implementation_plan.md §3.3) names whatever SIBs are
    *currently* open, ahead of a future SAT-based ``existPr`` extension whose answer would
    genuinely depend on it (dynamic reachability, §1's confirmed finding). The static
    ancestor-chain answer never depends on it -- accepted and ignored here -- so this is a
    pure signature seam, not yet load-bearing for this function's own return value. (It *is*
    load-bearing elsewhere: ``pdl_interpreter.PDLInterpreter`` tracks the same "currently
    open" set itself to size phase 1 of each ``iApply``, per implementation_plan.md §7
    Stage 5 §3.)

    Raises :class:`SibRetargetError`, naming ``instrument_name``, if no SIB in ``graph``
    gates an instrument by that name."""
    del network_configuration  # v1: accepted for the future existPr seam, unused today
    for node in graph.chain:
        found = _search(node, instrument_name, ())
        if found is not None:
            return found
    raise SibRetargetError(f"no path to instrument {instrument_name!r} in this network")


def _search_by_sib_name(node: SibNode, sib_name: str, path: tuple[str, ...]) -> tuple[str, ...] | None:
    here = path + (node.sib_name,)
    if node.sib_name == sib_name:
        return here
    for child in node.nested:
        found = _search_by_sib_name(child, sib_name, here)
        if found is not None:
            return found
    return None


def path_to_sib_name(graph: PhysicalGraph, sib_name: str) -> tuple[str, ...]:
    """Like :func:`open_path_to`, but resolves a SIB's own name directly (leaf or hierarchy)
    rather than an instrument it gates -- ``sib_overshift.py``'s connectivity checks target
    SIBs by name, which may name a hierarchy node directly (checking a whole nested subtree's
    reachability) rather than always naming a leaf instrument's own gating SIB.

    Raises :class:`SibRetargetError`, naming ``sib_name``, if no SIB in ``graph`` has that
    name."""
    for node in graph.chain:
        found = _search_by_sib_name(node, sib_name, ())
        if found is not None:
            return found
    raise SibRetargetError(f"no SIB named {sib_name!r} in this network")


def stage_open_sequence(graph: PhysicalGraph, target_open: frozenset[str]) -> list[frozenset[str]]:
    """Splits an arbitrary (possibly multi-branch, arbitrary-depth) target open-set into the
    ordered sequence of ``open_after`` states needed to physically reach it -- one entry per
    required Shift-DR/Update-DR round.

    A closed SIB's nested content isn't physically part of the live scan chain yet (confirmed
    against real RTL, ``tests/test_sib_cell_nested_cross_sim.py``), so a node at tree-depth
    ``d`` can be newly opened no earlier than round ``d`` -- its own ancestors must already be
    physically open. Round ``r``'s ``open_after`` is exactly every required node (every name
    in ``target_open`` plus every one of its own ancestors) at depth ``<= r``.

    Also correct for closing: a currently-open SIB that isn't in the final required set is
    simply never included at any round (closing has no physical-existence constraint, unlike
    opening), so it's closed as early as round 1.

    Returns ``[frozenset()]`` (a single all-closed round) for an empty ``target_open``. The
    caller pairs round ``i``'s ``open_now`` with round ``i - 1``'s result (or the real
    currently-open set for round 0) when calling ``layout_bit_length``/``compose_bits``."""
    if not target_open:
        return [frozenset()]
    paths = {name: path_to_sib_name(graph, name) for name in target_open}
    depth_of: dict[str, int] = {}
    for path in paths.values():
        for depth, name in enumerate(path, start=1):
            depth_of[name] = depth
    max_depth = max(depth_of.values())
    return [frozenset(n for n, d in depth_of.items() if d <= r) for r in range(1, max_depth + 1)]
