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
    ScanArm,
    ScanMuxNode,
    SibNode,
    resolve_dotted_address,
    slot_name,
    validate_physical_graph,
)


def _instrument(name: str) -> InstrumentNode:
    return InstrumentNode(name=name, width=1, capture_value=0)


def _leaf_arm(value: int, instrument_name: str) -> ScanArm:
    return ScanArm(values=(value,), instrument=_instrument(instrument_name))


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


def test_validate_physical_graph_accepts_a_minimal_scan_mux():
    mux = ScanMuxNode("mux_a", select_width=1, arms=(_leaf_arm(0, "a"), _leaf_arm(1, "b")))
    validate_physical_graph(PhysicalGraph(chain=(mux,)))  # must not raise


def test_validate_physical_graph_accepts_a_scan_mux_with_a_nested_arm():
    inner = SibNode("sib_inner", _instrument("deep"))
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(ScanArm(values=(0,), nested=(inner,)), _leaf_arm(1, "b")),
    )
    validate_physical_graph(PhysicalGraph(chain=(mux,)))  # must not raise


def test_validate_physical_graph_accepts_a_sib_nesting_a_scan_mux():
    """The widened ChainSlot typing lets a SibNode's own ``nested`` hold a ScanMuxNode, not
    just another SibNode -- mixed nesting works in both directions."""
    mux = ScanMuxNode("mux_inner", select_width=1, arms=(_leaf_arm(0, "a"), _leaf_arm(1, "b")))
    outer = SibNode("sib_outer", instrument=None, nested=(mux,))
    validate_physical_graph(PhysicalGraph(chain=(outer,)))  # must not raise


def test_validate_physical_graph_rejects_a_scan_mux_with_one_arm():
    mux = ScanMuxNode("mux_a", select_width=1, arms=(_leaf_arm(0, "a"),))
    with pytest.raises(ICLModelError, match="mux_a"):
        validate_physical_graph(PhysicalGraph(chain=(mux,)))


def test_validate_physical_graph_rejects_a_scan_mux_with_zero_select_width():
    mux = ScanMuxNode("mux_a", select_width=0, arms=(_leaf_arm(0, "a"), _leaf_arm(1, "b")))
    with pytest.raises(ICLModelError, match="mux_a"):
        validate_physical_graph(PhysicalGraph(chain=(mux,)))


def test_validate_physical_graph_rejects_an_out_of_range_arm_value():
    mux = ScanMuxNode("mux_a", select_width=1, arms=(_leaf_arm(0, "a"), _leaf_arm(2, "b")))
    with pytest.raises(ICLModelError, match="mux_a"):
        validate_physical_graph(PhysicalGraph(chain=(mux,)))


def test_validate_physical_graph_rejects_two_arms_sharing_a_value():
    mux = ScanMuxNode("mux_a", select_width=1, arms=(_leaf_arm(1, "a"), _leaf_arm(1, "b")))
    with pytest.raises(ICLModelError, match="mux_a"):
        validate_physical_graph(PhysicalGraph(chain=(mux,)))


def test_validate_physical_graph_rejects_an_arm_with_no_values():
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(ScanArm(values=(), instrument=_instrument("a")), _leaf_arm(1, "b")),
    )
    with pytest.raises(ICLModelError, match="mux_a"):
        validate_physical_graph(PhysicalGraph(chain=(mux,)))


def test_validate_physical_graph_rejects_an_arm_with_more_than_one_value():
    """v1 scope: ScanArm.values is forward-compatible with a real multi-value catch-all arm,
    but nothing consumes that shape yet -- validate_physical_graph pins it closed for now."""
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(ScanArm(values=(1, 2), instrument=_instrument("a")), _leaf_arm(3, "b")),
    )
    with pytest.raises(ICLModelError, match="mux_a"):
        validate_physical_graph(PhysicalGraph(chain=(mux,)))


def test_validate_physical_graph_rejects_an_arm_with_both_instrument_and_nested():
    both = ScanArm(values=(0,), instrument=_instrument("a"), nested=(SibNode("sib_x", None),))
    mux = ScanMuxNode("mux_a", select_width=1, arms=(both, _leaf_arm(1, "b")))
    with pytest.raises(ICLModelError, match="mux_a"):
        validate_physical_graph(PhysicalGraph(chain=(mux,)))


def test_validate_physical_graph_rejects_an_arm_with_neither_instrument_nor_nested():
    empty = ScanArm(values=(0,))
    mux = ScanMuxNode("mux_a", select_width=1, arms=(empty, _leaf_arm(1, "b")))
    with pytest.raises(ICLModelError, match="mux_a"):
        validate_physical_graph(PhysicalGraph(chain=(mux,)))


def test_validate_physical_graph_rejects_a_scan_mux_name_colliding_with_a_sib_name():
    """Global name uniqueness spans both node kinds -- a ScanMuxNode and a SibNode sharing one
    name is exactly as invalid as two SibNodes sharing one, since every name-keyed lookup
    throughout retargeting/layout is flat and kind-agnostic."""
    mux = ScanMuxNode("shared_name", select_width=1, arms=(_leaf_arm(0, "a"), _leaf_arm(1, "b")))
    sib = SibNode("shared_name", _instrument("c"))
    with pytest.raises(ICLModelError, match="shared_name"):
        validate_physical_graph(PhysicalGraph(chain=(mux, sib)))


def test_slot_name_reads_the_right_field_for_each_kind():
    mux = ScanMuxNode("mux_a", select_width=1, arms=(_leaf_arm(0, "a"), _leaf_arm(1, "b")))
    sib = SibNode("sib_a", _instrument("a"))
    assert slot_name(mux) == "mux_a"
    assert slot_name(sib) == "sib_a"
