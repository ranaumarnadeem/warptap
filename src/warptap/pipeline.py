"""End-to-end insertion convenience (library-quality pass, post-Stage 14). Extracts the exact
ingest -> build network -> insert -> re-emit sequence this project's own test suite otherwise
repeats by hand in every cross-sim test (e.g. ``tests/test_sib_insert_write_instrument_cross_sim.py``'s
own ``_build_and_insert`` helper, ``tests/test_mem_subsystem_mbist_pdl_verify.py``'s inline
equivalent) -- the single most common orchestration a real caller needs, now available as one
function instead of five.

Deliberately scoped to the SIB/IJTAG path only. Boundary-scan register insertion
(:func:`~warptap.bsr_insert.insert_bsr`) stays a separate, independent step -- this project has
always treated BSR and SIB/IJTAG insertion as two distinct concerns, and this function doesn't
change that.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple, Union

from warptap.icl_model import ModuleInstance, PhysicalGraph
from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import HierarchySpec, InstrumentSpec, build_sib_plan
from warptap.yosys_io import ingest, write_verilog_from_json

# Public alias for what build_sib_plan itself calls the private ``_Spec`` union
# (sib_plan.py) -- this function's own ``specs`` parameter needs the same union
# (a HierarchySpec composes with plain InstrumentSpecs in the same list, see
# docs/guide/nested-sib-networks.md), but sib_plan._Spec is module-internal by
# its leading underscore, so this module names its own public equivalent rather
# than reaching across the boundary for a private name.
TestAccessSpec = Union[InstrumentSpec, HierarchySpec]


def insert_test_access(
    sources: List[Union[Path, str]],
    top_module: str,
    specs: List[TestAccessSpec],
    *,
    yosys_command: str | None = None,
    use_sv: bool = False,
    port_renames: dict[str, str] | None = None,
) -> Tuple[str, PhysicalGraph, ModuleInstance]:
    """Ingest ``sources``, build the SIB/instrument network ``specs`` describes, insert it into
    ``top_module``, and return the synthesizable inserted Verilog text plus the
    ``(graph, root)`` pair needed to drive a :class:`~warptap.pdl_interpreter.PDLInterpreter`
    against that same network.

    ``specs`` may freely mix plain :class:`~warptap.sib_plan.InstrumentSpec` leaves with
    :class:`~warptap.sib_plan.HierarchySpec` entries (a SIB gating a nested sub-network instead
    of one instrument, at any depth -- see docs/guide/nested-sib-networks.md); this function
    was flat-only before this parameter widening, purely by never having offered the choice --
    :func:`~warptap.sib_plan.build_sib_plan` (called below, unchanged) already accepted the
    same union, so no behavioral change was needed to support this, only the type and this
    docstring were out of date with what the function actually did.

    Every leaf instrument's own name becomes one of ``root``'s children 1:1, flattened
    regardless of nesting depth (:func:`~warptap.sib_plan.build_sib_plan`'s own documented
    sibling-addressing convention -- a ``HierarchySpec`` never contributes a
    :class:`~warptap.icl_model.ModuleInstance` of its own), so the returned ``root`` is ready to
    pass straight into ``PDLInterpreter(graph, root)`` -- no separate bookkeeping needed to keep
    the module-instantiation tree in sync with the physical network, since both come from this
    one call.

    ``port_renames`` (``{old_name: new_name}``) is applied to ``top_module`` via
    :meth:`~warptap.netlist.Module.rename_port`, right after ingest and BEFORE
    ``insert_sib_network`` claims its own hardcoded top-level JTAG pin names
    (``tap_ports.TDI``/``TDO``/etc.) -- needed when the design being wrapped already has a
    top-level port under one of those names (e.g. a faultflow compression/compaction composed
    netlist's own ``tdi``/``tdo`` channel bus). Every ``InstrumentSpec.signal_bits`` referencing
    a renamed port must use the NEW (post-rename) name -- this function doesn't rewrite
    ``specs`` for the caller.
    """
    raw = ingest(sources, top_module, yosys_command=yosys_command, use_sv=use_sv)
    netlist = Netlist.from_json(raw)
    if port_renames:
        top_mod = netlist.module(top_module)
        for old_name, new_name in port_renames.items():
            top_mod.rename_port(old_name, new_name)
    graph, root = build_sib_plan(specs, top_name=top_module)
    insert_sib_network(netlist, top_module, graph, yosys_command=yosys_command)
    inserted_verilog = write_verilog_from_json(
        netlist.to_json(), yosys_command=yosys_command
    )
    return inserted_verilog, graph, root
