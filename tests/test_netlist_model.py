"""Unit tests for the Module/Netlist object-model layer (implementation_plan.md §4.2).
Pure Python — no Yosys subprocess involved, these should run instantly."""

from __future__ import annotations

import pytest

from warptap.netlist import Module, Netlist, PortInfo


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


def test_ports_lists_in_declaration_order():
    mod = _module_with_bits()  # clk (input) then y (output), dict-insertion order
    assert mod.ports() == [
        PortInfo("clk", "input", [2]),
        PortInfo("y", "output", [5]),
    ]


def test_ports_does_not_mutate():
    mod = _module_with_bits()
    before = dict(mod.data["ports"])
    mod.ports()
    assert mod.data["ports"] == before


def test_port_bits_returns_current_bits():
    mod = _module_with_bits()
    assert mod.port_bits("y") == [5]
    mod.detach_port("y")
    assert mod.port_bits("y") != [5]


def test_add_port_allocates_fresh_bits():
    mod = _module_with_bits()
    bits = mod.add_port("tck", "input")
    assert bits == [6]  # next free bit after clk=2/y=5
    assert mod.data["ports"]["tck"] == {"direction": "input", "bits": [6]}


def test_add_port_rejects_duplicate_name():
    mod = _module_with_bits()
    with pytest.raises(ValueError):
        mod.add_port("clk", "input")


def test_detach_port_returns_old_bits_and_assigns_fresh_ones():
    mod = _module_with_bits()
    old_bits = mod.detach_port("y")
    assert old_bits == [5]
    new_bits = mod.data["ports"]["y"]["bits"]
    assert new_bits != [5]
    assert new_bits[0] not in {2, 5}  # freshly allocated, no collision


def test_detach_port_gives_old_bits_a_netname_so_they_stay_legible():
    mod = _module_with_bits()
    old_bits = mod.detach_port("y")
    assert mod.data["netnames"]["y_pre_bsr"]["bits"] == old_bits


def test_detach_port_resyncs_an_existing_same_named_netname():
    """Yosys typically emits a netnames entry mirroring each port's own bits (e.g.
    netnames["y"] alongside ports["y"]). If detach_port left that stale, "y" would
    mean two different bit-vectors depending on whether ports or netnames is
    consulted -- write_verilog would render that as two conflicting drivers."""
    mod = _module_with_bits()
    mod.data["netnames"]["y"] = {"hide_name": 0, "bits": [5], "attributes": {}}
    mod.detach_port("y")
    assert mod.data["netnames"]["y"]["bits"] == mod.data["ports"]["y"]["bits"]


def test_detach_port_does_not_disturb_other_references_to_old_bits():
    mod = _module_with_bits(extra_connection_bits=[5])  # c0.A references y's old bit
    mod.detach_port("y")
    assert mod.data["cells"]["c0"]["connections"]["A"] == [5]


def test_set_module_attribute():
    mod = _module_with_bits()
    mod.set_module_attribute("keep_hierarchy")
    mod.set_module_attribute("some_value", 42)
    assert mod.data["attributes"]["keep_hierarchy"] == 1
    assert mod.data["attributes"]["some_value"] == 42


def test_netlist_add_module_imports_and_wraps():
    nl = Netlist.from_json({"modules": {}})
    template = {"ports": {"a": {"direction": "input", "bits": [2]}}, "cells": {}}
    mod = nl.add_module("bc1_shift_only", template)
    assert mod.name == "bc1_shift_only"
    assert nl.module("bc1_shift_only") is mod
    assert nl.to_json()["modules"]["bc1_shift_only"] is template


def test_netlist_add_module_rejects_duplicate_name():
    nl = Netlist.from_json({"modules": {"tap_core": {"ports": {}, "cells": {}}}})
    with pytest.raises(ValueError):
        nl.add_module("tap_core", {"ports": {}, "cells": {}})


def test_netlist_add_module_does_not_mutate_the_source_dict():
    template = {"ports": {}, "cells": {}}
    import copy

    before = copy.deepcopy(template)
    nl = Netlist.from_json({"modules": {}})
    nl.add_module("m", template)
    assert template == before
