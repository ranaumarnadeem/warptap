"""Pure planning phase for SIB network insertion (implementation_plan.md §7 Stage 4, §3.2).
Mirrors bsr_plan.py's split: this module only decides *what* the network looks like: one
top-level SIB per requested instrument, chained TDI-side-first in caller-given order. The
impure surgery that actually inserts cells into a netlist lives in ``sib_insert.py``.

Unlike ``build_bsr_plan``, this isn't derived from a target module's existing ports --
instruments are explicitly caller-specified, since warptap doesn't invent instrument content
(a real instrument is TAM/DFT logic outside this project's scope; v1 wraps a fixed-value stub,
see ``sib_insert.py``)."""

from __future__ import annotations

from typing import NamedTuple

from warptap.icl_model import (
    Alias,
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    SibNode,
    SignalBinding,
)


class InstrumentSpec(NamedTuple):
    name: str
    width: int
    capture_value: int
    direction: InstrumentDirection = InstrumentDirection.READ
    signal_bits: tuple[SignalBinding, ...] = ()
    aliases: tuple[Alias, ...] = ()  # named sub-fields PDL's iWrite/iRead can address (Stage 15)


def build_sib_plan(
    specs: list[InstrumentSpec], *, top_name: str = "top"
) -> tuple[PhysicalGraph, ModuleInstance]:
    """One top-level SIB per instrument, chained in ``specs`` order (index 0 nearest TDI --
    ``PhysicalGraph.chain``'s own documented ordering). Each SIB is named ``f"sib_{spec.name}"``,
    deterministic and human-legible in emitted RTL/attributes.

    Returns a flat :class:`ModuleInstance` tree: every instrument a direct sibling child of the
    root, matching the confirmed sibling-addressing convention (§3.2) -- SIBs themselves aren't
    represented as ModuleInstance nodes, since dotted-address resolution only ever needs to
    reach an instrument, and each instrument's gating SIB is already recorded on its
    :class:`~warptap.icl_model.SibNode` in the returned graph.

    ``top_name`` defaults to the placeholder ``"top"`` every prior stage's own tests already
    use, but a real caller wanting the returned tree's root to name the actual target module
    (implementation_plan.md §7 Stage 10's own need: ``icl_emit.to_icl()`` renders
    ``root.name`` as the emitted ICL file's real ``Module <name> { ... }`` name) should pass
    the same string given to ``sib_insert.insert_sib_network``'s own ``top`` argument --
    nothing today enforces the two calls agree, since there is no single orchestration entry
    point yet threading one value to both; document the invariant here rather than silently
    assume it.

    Raises :class:`ValueError` on a duplicate instrument name -- silently letting two
    instruments share a name would make both ``resolve_dotted_address`` and
    ``sib_retarget.open_path_to`` ambiguous."""
    seen: set[str] = set()
    chain: list[SibNode] = []
    children: list[ModuleInstance] = []

    for spec in specs:
        if spec.name in seen:
            raise ValueError(f"duplicate instrument name {spec.name!r} in specs")
        seen.add(spec.name)

        instrument = InstrumentNode(
            name=spec.name,
            width=spec.width,
            capture_value=spec.capture_value,
            direction=spec.direction,
            signal_bits=spec.signal_bits,
            aliases=spec.aliases,
        )
        chain.append(SibNode(sib_name=f"sib_{spec.name}", instrument=instrument))
        children.append(ModuleInstance(name=spec.name))

    graph = PhysicalGraph(chain=tuple(chain))
    root = ModuleInstance(name=top_name, children=tuple(children))
    return graph, root
