"""Structural post-synthesis check (implementation_plan.md §7 Stage 4, testing layer 3):
every planned SIB and instrument bit-cell must survive Yosys's real default synthesis recipe
(synth -top trivial, flatten included) as a distinct, individually-identifiable instance.
Stage 4 is a *sharper* case of the opt_merge risk (yosys#855) than Stage 3: multiple SIBs are
structurally near-identical, and within one instrument, bit-cells whose `pi` ties to the same
constant bit are structurally identical too -- mirrors test_bsr_insert_structural.py's per-cell
attribute-survival check.
"""

from __future__ import annotations

from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import HierarchySpec, InstrumentSpec, build_sib_plan
from warptap.yosys_io import ingest, synthesize

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=3, capture_value=0b101),  # deliberately identical to a
    InstrumentSpec("sensor_c", width=2, capture_value=0b11),  # both bits tie `pi` to constant 1
]


def _decode_int_attr(value) -> int:
    """Yosys round-trips integer-valued attributes as zero-padded binary strings --
    string attributes (e.g. warptap_sib_name) pass through unchanged (same convention
    test_bsr_insert_structural.py already confirmed)."""
    if isinstance(value, int):
        return value
    return int(value, 2)


def _build_inserted_network(fixtures_dir, yosys_command, specs):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(specs)
    insert_sib_network(netlist, "trivial", graph, yosys_command=yosys_command)
    return netlist, graph, root


def _synthesize(netlist, yosys_command):
    synthesized = synthesize(
        netlist.to_json(), synth_script="synth -top trivial", yosys_command=yosys_command
    )
    return synthesized["modules"]["trivial"]["cells"]


def test_every_sib_survives_synth_with_flatten(fixtures_dir, yosys_command):
    netlist, graph, _root = _build_inserted_network(fixtures_dir, yosys_command, _SPECS)
    top_cells = _synthesize(netlist, yosys_command)

    surviving_sib_names = {
        cell["attributes"]["warptap_sib_name"]
        for cell in top_cells.values()
        if "warptap_sib_name" in cell.get("attributes", {})
        and "warptap_instrument_bit" not in cell.get("attributes", {})
    }
    assert surviving_sib_names == {node.sib_name for node in graph.chain}


def test_no_sib_is_merged_with_another(fixtures_dir, yosys_command):
    netlist, graph, _root = _build_inserted_network(fixtures_dir, yosys_command, _SPECS)
    top_cells = _synthesize(netlist, yosys_command)

    counts: dict[str, int] = {}
    for cell in top_cells.values():
        attrs = cell.get("attributes", {})
        if "warptap_sib_name" in attrs and "warptap_instrument_bit" not in attrs:
            counts[attrs["warptap_sib_name"]] = counts.get(attrs["warptap_sib_name"], 0) + 1

    assert counts == {node.sib_name: 1 for node in graph.chain}


def test_every_instrument_bit_cell_survives_individually(fixtures_dir, yosys_command):
    """The sharper opt_merge case named in this file's docstring: sensor_c's two
    bit-cells both tie `pi` to the constant 1 -- structurally identical inputs, exactly
    opt_merge's documented collapse target -- and must still survive as two distinct
    instances, not one shared one."""
    netlist, graph, _root = _build_inserted_network(fixtures_dir, yosys_command, _SPECS)
    top_cells = _synthesize(netlist, yosys_command)

    surviving = {
        (
            cell["attributes"]["warptap_sib_name"],
            _decode_int_attr(cell["attributes"]["warptap_instrument_bit"]),
        )
        for cell in top_cells.values()
        if "warptap_instrument_bit" in cell.get("attributes", {})
    }
    expected = {
        (node.sib_name, k) for node in graph.chain for k in range(node.instrument.width)
    }
    assert surviving == expected


def test_tap_core_instance_survives_synth(fixtures_dir, yosys_command):
    netlist, _graph, _root = _build_inserted_network(fixtures_dir, yosys_command, _SPECS)
    top_cells = _synthesize(netlist, yosys_command)
    tap_core_instances = [c for c in top_cells.values() if c["type"] == "tap_core"]
    assert len(tap_core_instances) == 1


def _all_sib_names(chain) -> set:
    """Recursive, unlike the flat tests above's own {node.sib_name for node in graph.chain}
    -- a nested network's own sib_names live at every depth, not just the top level."""
    names = set()
    for node in chain:
        names.add(node.sib_name)
        names |= _all_sib_names(node.nested)
    return names


_NESTED_SPECS = [
    HierarchySpec(
        "bank_a",
        children=[
            # Deliberately identical to each other -- the same opt_merge risk this file's own
            # docstring names, one level deeper than the flat _SPECS above ever reach.
            InstrumentSpec("nested_a", width=3, capture_value=0b101),
            InstrumentSpec("nested_b", width=3, capture_value=0b101),
        ],
    ),
    InstrumentSpec("top_level", width=3, capture_value=0b101),  # identical to both, one level up
]


def test_nested_sib_survives_synth_distinctly_from_its_siblings_and_ancestor(
    fixtures_dir, yosys_command
):
    """The recursively-inserted nested SIB cell is the same sib_cell module type as every
    top-level one, and structurally identical to its own nested sibling and to the unrelated
    top-level SIB besides -- confirms set_keep is applied at every recursion depth, not just
    the top level, so none of the three collapse into one shared instance."""
    netlist, graph, _root = _build_inserted_network(fixtures_dir, yosys_command, _NESTED_SPECS)
    top_cells = _synthesize(netlist, yosys_command)

    counts: dict[str, int] = {}
    for cell in top_cells.values():
        attrs = cell.get("attributes", {})
        if "warptap_sib_name" in attrs and "warptap_instrument_bit" not in attrs:
            counts[attrs["warptap_sib_name"]] = counts.get(attrs["warptap_sib_name"], 0) + 1

    assert counts == {name: 1 for name in _all_sib_names(graph.chain)}
    assert len(counts) == 4  # sib_bank_a, sib_nested_a, sib_nested_b, sib_top_level
