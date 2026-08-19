"""Unit tests for the Module/Netlist object-model layer (implementation_plan.md §4.2).
Pure Python — no Yosys subprocess involved, these should run instantly."""

from __future__ import annotations

import pytest

from warptap.netlist import Module, Netlist


def _module_with_bits(extra_connection_bits=None) -> Module:
    data = {
        "ports": {
            "clk": {"direction": "input", "bits": [2]},
            "y": {"direction": "output", "bits": [5]},
        },
        "cells": {},
        "netnames": {},
    }
    if extra_connection_bits:
        data["cells"]["c0"] = {
            "type": "$dummy",
            "connections": {"A": extra_connection_bits},
        }
    return Module("m", data)


def test_wrapping_a_module_does_not_mutate_its_dict():
    """The exact bug the golden-file round-trip test (plan §4.3) exists to catch:
    wrapping must never fill in missing/empty keys as a side effect of loading."""
    data = {"ports": {}, "cells": {}}  # deliberately missing "netnames"
    Module("m", data)
    assert data == {"ports": {}, "cells": {}}


def test_alloc_bit_avoids_existing_ids_including_constants():
    mod = _module_with_bits(extra_connection_bits=[7, "1", 3])
    seen = set()
    for _ in range(5):
        b = mod.alloc_bit()
        assert b not in {2, 5, 7, 3}
        assert b not in seen
        seen.add(b)


def test_new_wire_allocates_unique_bits_and_registers_netname():
    mod = _module_with_bits()
    bits = mod.new_wire(4, name="tdi_chain")
    assert len(bits) == 4
    assert len(set(bits)) == 4
    assert mod.data["netnames"]["tdi_chain"]["bits"] == bits


def test_new_wire_without_name_does_not_touch_netnames():
    mod = _module_with_bits()
    before = dict(mod.data["netnames"])
    mod.new_wire(2)
    assert mod.data["netnames"] == before


def test_add_cell_and_connect():
    mod = _module_with_bits()
    sel = mod.new_wire(1, name="sib_select")
    mod.add_cell(
        "sib0",
        "MUX2_NI",
        port_directions={"A0": "input", "A1": "input", "S": "input", "Y": "output"},
        connections={"A0": ["0"], "A1": ["1"], "S": sel},
    )
    out = mod.new_wire(1, name="sib0_y")
    mod.connect("sib0", "Y", out)
    assert mod.data["cells"]["sib0"]["connections"]["Y"] == out
    assert mod.data["cells"]["sib0"]["connections"]["S"] == sel
    assert mod.cell_count() == 1


def test_add_cell_rejects_duplicate_name():
    mod = _module_with_bits()
    mod.add_cell("c1", "INV")
    with pytest.raises(ValueError):
        mod.add_cell("c1", "INV")


def test_set_keep_on_wire_and_cell():
    mod = _module_with_bits()
    mod.new_wire(1, name="tap_state")
    mod.add_cell("tap_fsm", "$dff")
    mod.set_keep(wire_name="tap_state")
    mod.set_keep(cell_name="tap_fsm")
    assert mod.data["netnames"]["tap_state"]["attributes"]["keep"] == 1
    assert mod.data["cells"]["tap_fsm"]["attributes"]["keep"] == 1


def test_netlist_wraps_multiple_modules_without_mutating():
    raw = {
        "modules": {
            "a": {"ports": {}, "cells": {}},
            "b": {"ports": {}, "cells": {}},
        }
    }
    import copy

    before = copy.deepcopy(raw)
    nl = Netlist.from_json(raw)
    assert set(nl.module_names()) == {"a", "b"}
    assert nl.module("a").name == "a"
    assert raw == before
    assert nl.to_json() is raw
