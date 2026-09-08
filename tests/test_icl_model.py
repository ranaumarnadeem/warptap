"""Pure-Python tests for the ICL data model (implementation_plan.md §7 Stage 4, §3.2).
No Yosys involved -- synthetic ModuleInstance trees only, mirroring test_bsr_plan.py's style.
"""

from __future__ import annotations

import pytest

from warptap.icl_model import (
    ICLAddressError,
    ICLModelError,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    SibNode,
    resolve_dotted_address,
    validate_physical_graph,
)


def _instrument(name: str) -> InstrumentNode:
    return InstrumentNode(name=name, width=1, capture_value=0)


def test_resolve_single_segment_child():
    root = ModuleInstance("top", children=(ModuleInstance("sensor_a"),))
    resolved = resolve_dotted_address(root, "sensor_a")
    assert resolved.name == "sensor_a"


def test_resolve_multi_segment_path():
    leaf = ModuleInstance("temp_probe")
    wrapper = ModuleInstance("wrapper", children=(leaf,))
    root = ModuleInstance("top", children=(wrapper,))
    resolved = resolve_dotted_address(root, "wrapper.temp_probe")
    assert resolved is leaf


def test_resolve_picks_the_matching_sibling_not_the_first():
    a = ModuleInstance("sensor_a")
    b = ModuleInstance("sensor_b")
    root = ModuleInstance("top", children=(a, b))
    assert resolve_dotted_address(root, "sensor_b") is b


def test_unresolvable_segment_raises_named_error():
    root = ModuleInstance("top", children=(ModuleInstance("sensor_a"),))
    with pytest.raises(ICLAddressError, match="sensor_z"):
        resolve_dotted_address(root, "sensor_z")


def test_unresolvable_mid_path_segment_raises_named_error():
    wrapper = ModuleInstance("wrapper", children=(ModuleInstance("temp_probe"),))
    root = ModuleInstance("top", children=(wrapper,))
    with pytest.raises(ICLAddressError, match="missing_child"):
        resolve_dotted_address(root, "wrapper.missing_child")


def test_validate_physical_graph_accepts_a_flat_network():
    graph = PhysicalGraph(chain=(SibNode("sib_a", _instrument("a")),))
    validate_physical_graph(graph)  # must not raise


def test_validate_physical_graph_accepts_a_nested_network():
    inner = SibNode("sib_inner", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    validate_physical_graph(PhysicalGraph(chain=(outer,)))  # must not raise


def test_validate_physical_graph_rejects_both_instrument_and_nested():
    both = SibNode("sib_both", instrument=_instrument("a"), nested=(SibNode("sib_x", None),))
    with pytest.raises(ICLModelError, match="sib_both"):
        validate_physical_graph(PhysicalGraph(chain=(both,)))


def test_validate_physical_graph_rejects_neither_instrument_nor_nested():
    empty = SibNode("sib_empty", instrument=None)
    with pytest.raises(ICLModelError, match="sib_empty"):
        validate_physical_graph(PhysicalGraph(chain=(empty,)))


def test_validate_physical_graph_rejects_a_duplicate_name_at_the_top_level():
    graph = PhysicalGraph(chain=(SibNode("sib_dup", _instrument("a")), SibNode("sib_dup", _instrument("b"))))
    with pytest.raises(ICLModelError, match="sib_dup"):
        validate_physical_graph(graph)


def test_validate_physical_graph_rejects_a_duplicate_name_across_nesting_depths():
    """A name repeating between a top-level SIB and one nested arbitrarily deep inside a
    DIFFERENT top-level SIB -- global uniqueness, not just per-level or per-branch."""
    inner = SibNode("sib_shared", _instrument("deep"))
    outer = SibNode("sib_outer", instrument=None, nested=(inner,))
    sibling = SibNode("sib_shared", _instrument("shallow"))
    graph = PhysicalGraph(chain=(outer, sibling))
    with pytest.raises(ICLModelError, match="sib_shared"):
        validate_physical_graph(graph)
