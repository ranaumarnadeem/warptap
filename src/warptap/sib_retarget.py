"""Static PDL retargeting for SIB networks (implementation_plan.md §7 Stage 4, §3.3). Answers
exactly one question: which SIBs must be OPEN to reach a named instrument. Every other SIB in
the network stays CLOSED during the resulting phase-1 Capture-Shift-Update sequence.

This is a plain static-reachability question, not dynamic reachability -- IEEE 1687's
``existPr``/conditional-instrument-presence mechanism is a provably bounded-horizon SAT-class
problem (implementation_plan.md §1's confirmed finding) and is out of scope here; v1's
canonical single-control-bit SIBs never produce that shape, so a plain DFS is correct and
sufficient."""

from __future__ import annotations

from warptap.icl_model import PhysicalGraph, SibNode


class SibRetargetError(RuntimeError):
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
) -> frozenset[str]:
    """The SIB ancestor chain that must be OPEN to reach ``instrument_name``. For a
    directly-gated instrument (v1's only real case, since networks are flat/static per
    §7 Stage 4's scope), this is exactly ``frozenset({that_sib_name})`` -- the recursion
    into :attr:`~warptap.icl_model.SibNode.nested` never actually goes more than one level
    deep today, not as a special case but as the natural result of walking a currently-shallow
    tree; it only needs to keep working once nested SIB-gating-SIB networks land.

    ``network_configuration`` (implementation_plan.md §3.3) names whatever SIBs are
    *currently* open, ahead of a future SAT-based ``existPr`` extension whose answer would
    genuinely depend on it (dynamic reachability, §1's confirmed finding). v1's static
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
            return frozenset(found)
    raise SibRetargetError(f"no path to instrument {instrument_name!r} in this network")
