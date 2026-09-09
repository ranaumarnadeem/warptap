"""Structural post-synthesis check for a ScanMuxNode-containing network (multi-arm ScanMux
plan Phase 5), mirroring test_sib_insert_structural.py's own convention: every planned mux
instance and arm bit-cell must survive Yosys's real default synthesis recipe (synth -top
<top>, flatten included) as a distinct, individually-identifiable instance, tagged
``warptap_mux_name`` (not ``warptap_sib_name``) so it's never confused with a plain SIB.
"""

from __future__ import annotations

from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
)
from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.yosys_io import ingest, synthesize


def _instrument(name: str, width: int, *, capture_value: int = 0) -> InstrumentNode:
    return InstrumentNode(name=name, width=width, capture_value=capture_value)


def _two_mux_graph() -> PhysicalGraph:
    """Two DISTINCT muxes -- deliberately different (arms, select_width) shapes, so this
    also confirms Phase 5's own per-instance-imported-module design doesn't collide (each
    gets its own uniquely-named module type; see sib_insert.py's own module docstring)."""
    mux_a = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            _leaf_arm(1, "a1", width=2, capture_value=0b11),
            _leaf_arm(2, "a2", width=2, capture_value=0b11),  # identical shape to a1 --
            # the sharper opt_merge case test_sib_insert_structural.py's own docstring names
        ),
    )
    mux_b = ScanMuxNode(
        "mux_b", select_width=2,
        arms=(
            _leaf_arm(1, "b1", width=1),
            _leaf_arm(2, "b2", width=1),
            _leaf_arm(3, "b3", width=1),
        ),
    )
    return PhysicalGraph(chain=(mux_a, mux_b))


def _leaf_arm(value: int, name: str, *, width: int, capture_value: int = 0) -> ScanArm:
    return ScanArm(values=(value,), instrument=_instrument(name, width, capture_value=capture_value))


def _build_inserted_network(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph = _two_mux_graph()
    insert_sib_network(netlist, "trivial", graph, yosys_command=yosys_command)
    return netlist, graph


def _synthesize(netlist, yosys_command):
    synthesized = synthesize(
        netlist.to_json(), synth_script="synth -top trivial", yosys_command=yosys_command
    )
    return synthesized["modules"]["trivial"]["cells"]


def test_every_mux_survives_synth_with_flatten(fixtures_dir, yosys_command):
    netlist, graph = _build_inserted_network(fixtures_dir, yosys_command)
    top_cells = _synthesize(netlist, yosys_command)

    surviving_mux_names = {
        cell["attributes"]["warptap_mux_name"]
        for cell in top_cells.values()
        if "warptap_mux_name" in cell.get("attributes", {})
        and "warptap_arm_index" not in cell.get("attributes", {})
    }
    assert surviving_mux_names == {node.mux_name for node in graph.chain}


def test_no_mux_is_merged_with_another(fixtures_dir, yosys_command):
    """mux_a's two arms are structurally identical (same width, same capture_value) --
    exactly opt_merge's documented collapse target -- and the two DISTINCT mux instances
    themselves are ALSO structurally similar (both select_width=2). None of it may merge."""
    netlist, graph = _build_inserted_network(fixtures_dir, yosys_command)
    top_cells = _synthesize(netlist, yosys_command)

    counts: dict[str, int] = {}
    for cell in top_cells.values():
        attrs = cell.get("attributes", {})
        if "warptap_mux_name" in attrs and "warptap_arm_index" not in attrs:
            counts[attrs["warptap_mux_name"]] = counts.get(attrs["warptap_mux_name"], 0) + 1

    assert counts == {node.mux_name: 1 for node in graph.chain}


def test_every_arm_bit_cell_survives_individually(fixtures_dir, yosys_command):
    """mux_a's own two arms (a1, a2) are structurally identical leaf instruments (same width,
    same fixed capture_value, no signal_bits) -- the sharper opt_merge case
    test_sib_insert_structural.py's own docstring names, now for mux arms specifically."""
    netlist, graph = _build_inserted_network(fixtures_dir, yosys_command)
    top_cells = _synthesize(netlist, yosys_command)

    surviving = {
        (
            cell["attributes"]["warptap_mux_name"],
            cell["attributes"]["warptap_arm_index"]
            if isinstance(cell["attributes"]["warptap_arm_index"], int)
            else int(cell["attributes"]["warptap_arm_index"], 2),
        )
        for cell in top_cells.values()
        if "warptap_arm_index" in cell.get("attributes", {})
    }
    expected = {
        (node.mux_name, k)
        for node in graph.chain
        for k in range(len(node.arms))
    }
    assert surviving == expected


def test_tap_core_instance_survives_synth(fixtures_dir, yosys_command):
    netlist, _graph = _build_inserted_network(fixtures_dir, yosys_command)
    top_cells = _synthesize(netlist, yosys_command)
    tap_core_instances = [c for c in top_cells.values() if c["type"] == "tap_core"]
    assert len(tap_core_instances) == 1
