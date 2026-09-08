"""Pure-Python tests for SIB network planning (implementation_plan.md §7 Stage 4, §3.2).
No Yosys involved -- mirrors test_bsr_plan.py's style."""

from __future__ import annotations

import pytest

from warptap.icl_model import ICLModelError
from warptap.sib_plan import HierarchySpec, InstrumentSpec, build_sib_plan


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


def test_hierarchy_spec_produces_a_nested_sib_with_no_instrument():
    graph, _root = build_sib_plan(
        [HierarchySpec("bank_a", children=[InstrumentSpec("deep", width=1, capture_value=0)])]
    )
    assert len(graph.chain) == 1
    outer = graph.chain[0]
    assert outer.sib_name == "sib_bank_a"
    assert outer.instrument is None
    assert len(outer.nested) == 1
    inner = outer.nested[0]
    assert inner.sib_name == "sib_deep"
    assert inner.instrument.name == "deep"
    assert inner.nested == ()


def test_hierarchy_spec_can_nest_arbitrarily_deep():
    graph, _root = build_sib_plan(
        [
            HierarchySpec(
                "level1",
                children=[
                    HierarchySpec(
                        "level2",
                        children=[InstrumentSpec("leaf", width=1, capture_value=0)],
                    )
                ],
            )
        ]
    )
    level1 = graph.chain[0]
    level2 = level1.nested[0]
    leaf = level2.nested[0]
    assert (level1.sib_name, level2.sib_name, leaf.sib_name) == (
        "sib_level1", "sib_level2", "sib_leaf",
    )
    assert leaf.instrument.name == "leaf"


def test_hierarchy_spec_contributes_no_module_instance_of_its_own():
    """The ModuleInstance tree stays flat regardless of SIB-nesting depth -- only leaf
    instruments are ever direct siblings of the root, matching the pre-existing sibling-
    addressing convention every other build_sib_plan test already pins down."""
    _graph, root = build_sib_plan(
        [
            HierarchySpec(
                "bank_a",
                children=[
                    InstrumentSpec("deep_a", width=1, capture_value=0),
                    InstrumentSpec("deep_b", width=1, capture_value=0),
                ],
            ),
            InstrumentSpec("top_level", width=1, capture_value=0),
        ]
    )
    assert {c.name for c in root.children} == {"deep_a", "deep_b", "top_level"}
    assert all(c.children == () for c in root.children)


def test_duplicate_name_within_the_same_level_raises_value_error():
    with pytest.raises(ValueError, match="dup"):
        build_sib_plan(
            [
                HierarchySpec("bank_a", children=[InstrumentSpec("x", width=1, capture_value=0)]),
                HierarchySpec("dup", children=[InstrumentSpec("y", width=1, capture_value=0)]),
                InstrumentSpec("dup", width=1, capture_value=0),
            ]
        )


def test_duplicate_name_across_different_levels_raises_icl_model_error():
    """The per-level ValueError check above can't see this: "shared" appears once under
    branch_a and once under branch_b -- two SEPARATE sibling lists, each internally
    duplicate-free, so the per-level check never compares them against each other. Both
    still produce SIB name "sib_shared", caught only by validate_physical_graph's
    whole-tree pass."""
    with pytest.raises(ICLModelError, match="shared"):
        build_sib_plan(
            [
                HierarchySpec(
                    "branch_a", children=[InstrumentSpec("shared", width=1, capture_value=0)]
                ),
                HierarchySpec(
                    "branch_b", children=[InstrumentSpec("shared", width=1, capture_value=0)]
                ),
            ]
        )
