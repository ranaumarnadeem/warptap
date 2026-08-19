"""Structural post-synthesis check (implementation_plan.md §4.3, refined per-cell not
aggregate, per this session's research): every planned BsrCell must survive Yosys's real
default synthesis recipe as a distinct, individually-identifiable instance — not merged
(yosys#855's opt_merge risk), not deleted."""

from __future__ import annotations

from warptap.bsr_insert import insert_bsr
from warptap.bsr_plan import build_bsr_plan
from warptap.netlist import Netlist
from warptap.yosys_io import ingest, synthesize


def _decode_int_attr(value) -> int:
    """Yosys round-trips integer-valued attributes as zero-padded binary strings
    (confirmed empirically against this project's own inserted netlists) — string
    attributes (e.g. warptap_bsr_port_name) pass through unchanged."""
    if isinstance(value, int):
        return value
    return int(value, 2)


def _build_inserted_plan(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    plan = build_bsr_plan(
        netlist.module("trivial"),
        clock_ports=frozenset({"clk"}),
        excluded_ports=frozenset({"rst_n"}),
    )
    insert_bsr(netlist, "trivial", plan, yosys_command=yosys_command)
    return netlist, plan


def test_every_bsr_cell_survives_synth_with_flatten(fixtures_dir, yosys_command):
    """Deliberately the worst case: Yosys's default `synth` recipe includes
    `flatten`, not a synth script hand-picked to dodge the risk."""
    netlist, plan = _build_inserted_plan(fixtures_dir, yosys_command)

    synthesized = synthesize(
        netlist.to_json(), synth_script="synth -top trivial", yosys_command=yosys_command
    )
    top_cells = synthesized["modules"]["trivial"]["cells"]

    surviving = {
        _decode_int_attr(cell["attributes"]["warptap_bsr_cell_number"])
        for cell in top_cells.values()
        if "warptap_bsr_cell_number" in cell.get("attributes", {})
    }
    expected = {c.cell_number for c in plan.cells}
    assert surviving == expected


def test_no_bsr_cell_is_merged_with_another(fixtures_dir, yosys_command):
    """Identity, not just count: exactly one surviving cell per cell_number — a raw
    aggregate count would miss an opt_merge collapse of two cells into one shared
    instance (structurally-identical repeated cells, e.g. two INPUT cells, are
    exactly opt_merge's documented collapse target)."""
    netlist, plan = _build_inserted_plan(fixtures_dir, yosys_command)

    synthesized = synthesize(
        netlist.to_json(), synth_script="synth -top trivial", yosys_command=yosys_command
    )
    top_cells = synthesized["modules"]["trivial"]["cells"]

    counts: dict[int, int] = {}
    for cell in top_cells.values():
        attrs = cell.get("attributes", {})
        if "warptap_bsr_cell_number" in attrs:
            num = _decode_int_attr(attrs["warptap_bsr_cell_number"])
            counts[num] = counts.get(num, 0) + 1

    assert counts == {c.cell_number: 1 for c in plan.cells}


def test_tap_core_instance_survives_synth(fixtures_dir, yosys_command):
    """The TAP itself is also `set_keep`-tagged (bsr_insert.py) — confirm it
    survives the same worst-case synth pass as a single, undamaged instance."""
    netlist, _plan = _build_inserted_plan(fixtures_dir, yosys_command)

    synthesized = synthesize(
        netlist.to_json(), synth_script="synth -top trivial", yosys_command=yosys_command
    )
    top_cells = synthesized["modules"]["trivial"]["cells"]

    tap_core_instances = [c for c in top_cells.values() if c["type"] == "tap_core"]
    assert len(tap_core_instances) == 1
