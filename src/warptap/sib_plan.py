"""Pure planning phase for SIB network insertion (implementation_plan.md §7 Stage 4, §3.2).
Mirrors bsr_plan.py's split: this module only decides *what* the network looks like: one
top-level SIB per requested instrument or nested sub-network, chained TDI-side-first in
caller-given order. The impure surgery that actually inserts cells into a netlist lives in
``sib_insert.py``.

Unlike ``build_bsr_plan``, this isn't derived from a target module's existing ports --
instruments are explicitly caller-specified, since warptap doesn't invent instrument content
(a real instrument is TAM/DFT logic outside this project's scope; v1 wraps a fixed-value stub,
see ``sib_insert.py``)."""

from __future__ import annotations

from typing import NamedTuple, Sequence, Union

from warptap.icl_model import (
    Alias,
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    SibNode,
    SignalBinding,
    validate_physical_graph,
)


class InstrumentSpec(NamedTuple):
    name: str
    width: int
    capture_value: int
    direction: InstrumentDirection = InstrumentDirection.READ
    signal_bits: tuple[SignalBinding, ...] = ()
    aliases: tuple[Alias, ...] = ()  # named sub-fields PDL's iWrite/iRead can address (Stage 15)


class HierarchySpec(NamedTuple):
    """A SIB that gates a nested sub-network instead of one instrument directly -- real IEEE
    1687 (a SIB's ``fromSO`` binds to another SIB's own ``SO`` rather than an instrument's).
    Its own SIB name is derived the same way an instrument's is (``f"sib_{name}"``), for
    naming consistency and so :func:`~warptap.icl_model.validate_physical_graph`'s global
    uniqueness check treats both kinds of node the same way. Contributes no
    :class:`~warptap.icl_model.ModuleInstance` node of its own -- only leaf instruments ever
    do, flattened into one sibling list under the root regardless of nesting depth (SIBs
    themselves were never part of the dotted-address space, matching v1's existing
    sibling-addressing convention)."""

    name: str
    children: tuple["_Spec", ...]


_Spec = Union[InstrumentSpec, HierarchySpec]


def _build_node(spec: _Spec) -> tuple[SibNode, list[ModuleInstance]]:
    """One spec -> one SibNode, plus whatever flat ModuleInstance children it (or its own
    nested specs, recursively) contribute -- a HierarchySpec contributes none of its own,
    only its descendants' leaf instruments do."""
    if isinstance(spec, HierarchySpec):
        nested, module_children = _build_chain(spec.children)
        return SibNode(sib_name=f"sib_{spec.name}", instrument=None, nested=nested), module_children

    instrument = InstrumentNode(
        name=spec.name,
        width=spec.width,
        capture_value=spec.capture_value,
        direction=spec.direction,
        signal_bits=spec.signal_bits,
        aliases=spec.aliases,
    )
    node = SibNode(sib_name=f"sib_{spec.name}", instrument=instrument)
    return node, [ModuleInstance(name=spec.name)]


def _build_chain(specs: Sequence[_Spec]) -> tuple[tuple[SibNode, ...], list[ModuleInstance]]:
    """One level of a chain: duplicate-name checking here is local to this level's own
    ``specs`` (matching this function's original, pre-nesting behavior exactly for a flat
    list) -- :func:`~warptap.icl_model.validate_physical_graph` is the authoritative,
    whole-tree check that also catches a duplicate across different levels."""
    seen: set[str] = set()
    chain: list[SibNode] = []
    module_children: list[ModuleInstance] = []

    for spec in specs:
        if spec.name in seen:
            raise ValueError(f"duplicate name {spec.name!r} in specs")
        seen.add(spec.name)

        node, node_module_children = _build_node(spec)
        chain.append(node)
        module_children.extend(node_module_children)

    return tuple(chain), module_children


def build_sib_plan(
    specs: Sequence[_Spec], *, top_name: str = "top"
) -> tuple[PhysicalGraph, ModuleInstance]:
    """One top-level SIB per spec, chained in ``specs`` order (index 0 nearest TDI --
    ``PhysicalGraph.chain``'s own documented ordering). A :class:`HierarchySpec` recurses,
    gating a nested sub-network instead of a single instrument -- built the same way at every
    depth. Each SIB is named ``f"sib_{spec.name}"``, deterministic and human-legible in
    emitted RTL/attributes.

    Returns a flat :class:`ModuleInstance` tree regardless of nesting depth: every instrument
    a direct sibling child of the root, matching the confirmed sibling-addressing convention
    (§3.2) -- SIBs themselves aren't represented as ModuleInstance nodes, since dotted-address
    resolution only ever needs to reach an instrument, and each instrument's gating SIB is
    already recorded on its :class:`~warptap.icl_model.SibNode` in the returned graph.

    ``top_name`` defaults to the placeholder ``"top"`` every prior stage's own tests already
    use, but a real caller wanting the returned tree's root to name the actual target module
    (implementation_plan.md §7 Stage 10's own need: ``icl_emit.to_icl()`` renders
    ``root.name`` as the emitted ICL file's real ``Module <name> { ... }`` name) should pass
    the same string given to ``sib_insert.insert_sib_network``'s own ``top`` argument --
    nothing today enforces the two calls agree, since there is no single orchestration entry
    point yet threading one value to both; document the invariant here rather than silently
    assume it.

    Raises :class:`ValueError` on a duplicate name within the same level -- silently letting
    two specs share a name would make both ``resolve_dotted_address`` and
    ``sib_retarget.open_path_to`` ambiguous. Raises
    :class:`~warptap.icl_model.ICLModelError` (via :func:`~warptap.icl_model.validate_physical_graph`)
    for a name that repeats across different levels, which this function's own per-level
    check can't see."""
    chain, module_children = _build_chain(specs)
    graph = PhysicalGraph(chain=chain)
    validate_physical_graph(graph)
    root = ModuleInstance(name=top_name, children=tuple(module_children))
    return graph, root
