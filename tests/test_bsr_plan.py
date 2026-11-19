"""Pure-Python tests for the BSR planning phase (implementation_plan.md §7 Stage 3, §3.1).
No Yosys involved: synthetic Module JSON only, mirroring test_netlist_model.py's style."""

from __future__ import annotations

import pytest

from warptap.bsr_plan import BsrFunction, build_bsr_plan
from warptap.netlist import Module


def _module_with_ports(port_specs: dict[str, str]) -> Module:
    """port_specs: {name: direction}, inserted in dict-iteration (declaration) order."""
    bit = 2
    ports = {}
    for name, direction in port_specs.items():
        ports[name] = {"direction": direction, "bits": [bit]}
        bit += 1
    return Module("m", {"ports": ports, "cells": {}, "netnames": {}})


def test_input_port_gets_one_bc1_input_cell():
    mod = _module_with_ports({"a": "input"})
    plan = build_bsr_plan(mod)
    assert len(plan.cells) == 1
    cell = plan.cells[0]
    assert cell.function is BsrFunction.INPUT
    assert cell.cell_type == "BC_1"
    assert cell.port_name == "a"
    assert cell.cell_number == 0
    assert cell.disable_cell_index is None


def test_output_port_gets_control_then_output3_pair():
    mod = _module_with_ports({"y": "output"})
    plan = build_bsr_plan(mod)
    assert len(plan.cells) == 2
    control, output3 = plan.cells
    assert control.function is BsrFunction.CONTROL
    assert control.cell_type == "BC_1"
    assert control.port_name == "y_oe"
    assert control.cell_number == 0
    assert control.disable_cell_index is None
    assert output3.function is BsrFunction.OUTPUT3
    assert output3.cell_type == "BC_1"
    assert output3.port_name == "y"
    assert output3.cell_number == 1
    assert output3.disable_cell_index == control.cell_number


def test_inout_port_gets_control_then_bidir_pair():
    mod = _module_with_ports({"io": "inout"})
    plan = build_bsr_plan(mod)
    assert len(plan.cells) == 2
    control, bidir = plan.cells
    assert control.function is BsrFunction.CONTROL
    assert bidir.function is BsrFunction.BIDIR
    assert bidir.cell_type == "BC_7"
    assert bidir.port_name == "io"
    assert bidir.disable_cell_index == control.cell_number


def test_cell_numbers_are_sequential_across_multiple_ports():
    mod = _module_with_ports({"a": "input", "b": "output", "c": "input"})
    plan = build_bsr_plan(mod)
    assert [c.cell_number for c in plan.cells] == [0, 1, 2, 3]
    # b's control cell (1) immediately precedes b's output3 cell (2).
    control = next(c for c in plan.cells if c.port_name == "b_oe")
    output3 = next(c for c in plan.cells if c.port_name == "b")
    assert output3.disable_cell_index == control.cell_number == 1


def test_walk_order_matches_port_declaration_order():
    mod = _module_with_ports({"z": "input", "a": "input", "m": "input"})
    plan = build_bsr_plan(mod)
    assert [c.port_name for c in plan.cells] == ["z", "a", "m"]


def test_safe_value_is_uniformly_zero():
    mod = _module_with_ports({"a": "input", "b": "output", "c": "inout"})
    plan = build_bsr_plan(mod)
    assert all(cell.safe_value == 0 for cell in plan.cells)


def test_safe_value_zero_is_the_disabled_tristate_value():
    """The specific claim bsr_plan.py's docstring makes: under bsr_insert.py's mux
    polarity (pin_out = extest_mode ? po : func_in, a control cell's pin_out tied
    directly to its paired $tribuf's EN, no inversion), safe_value=0 preloaded into
    a control cell's po is exactly the value that disables (Z-states) its paired
    driver. This is an algebraic property of that specific wiring, not something
    bsr_plan.py itself can vary: locked in here as documentation-as-test, with the
    real hardware-level proof in rtl/bc1_full.v's reset behavior and
    tests/test_bsr_insert_equivalence.py."""
    safe_value = 0
    extest_mode = 1
    po = safe_value
    pin_out = po if extest_mode else None  # mirrors `extest_mode ? po : func_in`
    tribuf_en = pin_out  # mirrors bsr_insert.py's direct (uninverted) EN wiring
    assert tribuf_en == 0  # $tribuf drives Z when EN=0


def test_clock_ports_are_skipped_not_scanned():
    mod = _module_with_ports({"clk": "input", "a": "input"})
    plan = build_bsr_plan(mod, clock_ports=frozenset({"clk"}))
    assert plan.skipped_ports == ["clk"]
    assert [c.port_name for c in plan.cells] == ["a"]


def test_excluded_ports_are_skipped_not_scanned():
    mod = _module_with_ports({"debug_only": "input", "a": "input"})
    plan = build_bsr_plan(mod, excluded_ports=frozenset({"debug_only"}))
    assert plan.skipped_ports == ["debug_only"]
    assert [c.port_name for c in plan.cells] == ["a"]


def test_skipped_ports_consume_no_cell_number():
    mod = _module_with_ports({"clk": "input", "a": "input", "b": "input"})
    plan = build_bsr_plan(mod, clock_ports=frozenset({"clk"}))
    assert [c.cell_number for c in plan.cells] == [0, 1]


def test_unrecognized_direction_raises_immediately():
    """The deliberately-broken case: an unrecognized `direction` value (something
    a real Yosys JSON should never produce, per this session's confirmed
    input/output/inout-only schema, but a malformed/synthetic module might) must
    raise rather than silently vanish from both the plan and skipped_ports."""
    mod = Module(
        "m",
        {"ports": {"weird": {"direction": "bogus", "bits": [2]}}, "cells": {}, "netnames": {}},
    )
    with pytest.raises(ValueError, match="bogus"):
        build_bsr_plan(mod)


def test_empty_module_produces_empty_plan():
    mod = _module_with_ports({})
    plan = build_bsr_plan(mod)
    assert plan.cells == []
    assert plan.skipped_ports == []


def test_v1_never_emits_clock_or_internal_function():
    mod = _module_with_ports({"a": "input", "b": "output", "c": "inout"})
    plan = build_bsr_plan(mod)
    functions = {c.function for c in plan.cells}
    assert BsrFunction.CLOCK not in functions
    assert BsrFunction.INTERNAL not in functions


def _vector_module() -> Module:
    """``input [7:4] b``, ``input [0:1] c`` (declared [lo:hi]), ``output [1:0] y``, ``input s``,
    in Yosys's JSON shape: ``offset``/``upto`` as Yosys writes them."""
    ports = {
        "b": {"direction": "input", "bits": [2, 3, 4, 5], "offset": 4},
        "c": {"direction": "input", "bits": [6, 7], "upto": 1},
        "y": {"direction": "output", "bits": [8, 9]},
        "s": {"direction": "input", "bits": [10]},
    }
    return Module("m", {"ports": ports, "cells": {}, "netnames": {}})


def test_every_bit_of_a_multi_bit_port_gets_its_own_cells_named_by_its_hdl_bit():
    plan = build_bsr_plan(_vector_module())
    assert [(c.port_name, c.function, c.bit) for c in plan.cells] == [
        ("b", BsrFunction.INPUT, 4), ("b", BsrFunction.INPUT, 5),
        ("b", BsrFunction.INPUT, 6), ("b", BsrFunction.INPUT, 7),
        # c is [0:1]: its first bit in Yosys order is c[1]
        ("c", BsrFunction.INPUT, 1), ("c", BsrFunction.INPUT, 0),
        ("y_oe", BsrFunction.CONTROL, 0), ("y", BsrFunction.OUTPUT3, 0),
        ("y_oe", BsrFunction.CONTROL, 1), ("y", BsrFunction.OUTPUT3, 1),
        ("s", BsrFunction.INPUT, None),
    ]
    assert [c.cell_number for c in plan.cells] == list(range(11))
    outputs = [c for c in plan.cells if c.function is BsrFunction.OUTPUT3]
    assert [c.disable_cell_index for c in outputs] == [6, 8]


def test_the_plan_records_every_port_and_its_declared_range():
    plan = build_bsr_plan(_vector_module(), clock_ports=frozenset({"s"}))
    assert [(p.name, p.direction, p.width, p.offset, p.upto) for p in plan.ports] == [
        ("b", "input", 4, 4, False),
        ("c", "input", 2, 0, True),
        ("y", "output", 2, 0, False),
        ("s", "input", 1, 0, False),
    ]
    assert plan.skipped_ports == ["s"]
    b, c = plan.ports[0], plan.ports[1]
    assert [b.hdl_bit(p) for p in range(4)] == [4, 5, 6, 7]
    assert [c.hdl_bit(p) for p in range(2)] == [1, 0]
    assert all(port.position(port.hdl_bit(p)) == p for port in (b, c) for p in range(port.width))
