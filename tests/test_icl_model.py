"""Pure-Python tests for the ICL data model (implementation_plan.md §7 Stage 4, §3.2).
No Yosys involved -- synthetic ModuleInstance trees only, mirroring test_bsr_plan.py's style.
"""

from __future__ import annotations

import pytest

from warptap.icl_model import ICLAddressError, ModuleInstance, resolve_dotted_address


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
