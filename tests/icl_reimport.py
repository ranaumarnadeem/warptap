"""Emit an imported network with :func:`~warptap.icl_emit.to_icl` and import it again: the ICL
import tests' round-trip check."""

from __future__ import annotations

from pathlib import Path

from warptap.icl_emit import to_icl
from warptap.icl_import import import_icl
from warptap.icl_model import InstrumentDirection, PhysicalGraph, ScanMuxNode, SignalBinding


def _bound(instrument):
    if instrument is None or instrument.direction is not InstrumentDirection.WRITE:
        return instrument
    return instrument._replace(
        signal_bits=tuple(SignalBinding(f"{instrument.name}_q", bit) for bit in range(instrument.width))
    )


def _bind(chain) -> tuple:
    nodes = []
    for node in chain:
        if isinstance(node, ScanMuxNode):
            arms = tuple(
                arm._replace(instrument=_bound(arm.instrument), nested=_bind(arm.nested))
                for arm in node.arms
            )
            nodes.append(node._replace(arms=arms))
        else:
            nodes.append(node._replace(instrument=_bound(node.instrument), nested=_bind(node.nested)))
    return tuple(nodes)


def reimport(graph: PhysicalGraph, root, directory: Path, icl_parser_module):
    """``graph`` and ``root``, emitted by warptap into ``directory`` and imported again.

    Each WRITE instrument is first bound to a stand-in host port, one bit per register bit,
    because :func:`to_icl` won't emit a WRITE instrument that drives nothing. Import can't
    recover those bindings (ICL doesn't name the net a DataRegister drives), so the graph that
    comes back has none, like ``graph``."""
    path = Path(directory) / "reemitted.icl"
    icl_text = to_icl(PhysicalGraph(chain=_bind(graph.chain)), root, include_access_link=False)
    path.write_text(icl_text, encoding="utf-8")
    return import_icl([path], root.name, icl_parser_module=icl_parser_module)
