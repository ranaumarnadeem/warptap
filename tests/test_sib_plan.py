"""Pure-Python tests for SIB network planning (implementation_plan.md §7 Stage 4, §3.2).
No Yosys involved -- mirrors test_bsr_plan.py's style."""

from __future__ import annotations

import pytest

from warptap.sib_plan import InstrumentSpec, build_sib_plan


def test_single_instrument_gets_one_top_level_sib():
    graph, root = build_sib_plan([InstrumentSpec("sensor_a", width=4, capture_value=0xA)])
    assert len(graph.chain) == 1
    node = graph.chain[0]
    assert node.sib_name == "sib_sensor_a"
    assert node.instrument.name == "sensor_a"
    assert node.instrument.width == 4
    assert node.instrument.capture_value == 0xA
    assert node.nested == ()


def test_chain_order_matches_specs_order_index_zero_nearest_tdi():
    graph, _root = build_sib_plan(
        [
            InstrumentSpec("first", width=1, capture_value=0),
            InstrumentSpec("second", width=1, capture_value=1),
            InstrumentSpec("third", width=1, capture_value=0),
        ]
    )
    assert [n.instrument.name for n in graph.chain] == ["first", "second", "third"]


def test_heterogeneous_instrument_widths_preserved():
    graph, _root = build_sib_plan(
        [
            InstrumentSpec("narrow", width=1, capture_value=1),
            InstrumentSpec("wide", width=8, capture_value=0xAB),
        ]
    )
    widths = {n.instrument.name: n.instrument.width for n in graph.chain}
    assert widths == {"narrow": 1, "wide": 8}


def test_module_instance_tree_is_flat_with_every_instrument_a_sibling():
    _graph, root = build_sib_plan(
        [
            InstrumentSpec("a", width=1, capture_value=0),
            InstrumentSpec("b", width=1, capture_value=0),
        ]
    )
    assert {c.name for c in root.children} == {"a", "b"}
    assert all(c.children == () for c in root.children)


def test_duplicate_instrument_name_raises():
    with pytest.raises(ValueError, match="dup"):
        build_sib_plan(
            [
                InstrumentSpec("dup", width=1, capture_value=0),
                InstrumentSpec("dup", width=1, capture_value=1),
            ]
        )


def test_empty_specs_produces_empty_network():
    graph, root = build_sib_plan([])
    assert graph.chain == ()
    assert root.children == ()
