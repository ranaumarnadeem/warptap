"""Pure-Python tests for the ICL emitter's multi-arm ScanMux support (multi-arm ScanMux plan
Phase 7). No external tool involved here -- structural/textual assertions against hand-built
PhysicalGraph/ScanMuxNode objects, mirroring test_icl_emit.py's own style closely. Live
validation against the vendored icl_parser lives in test_icl_emit_iclparser_validation.py,
alongside every other external-tool-validated shape.
"""

from __future__ import annotations

import pytest

from warptap.icl_emit import (
    IclEmitError,
    render_scan_mux_module,
    render_sib_instances,
    to_icl,
)
from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SibNode,
    SignalBinding,
)


def _instrument(name="a", width=1, capture_value=0, signal_bits=()):
    return InstrumentNode(name=name, width=width, capture_value=capture_value, signal_bits=signal_bits)


def _mux(name="mux_a", select_width=2, arms=None):
    if arms is None:
        arms = (
            ScanArm(values=(1,), instrument=_instrument(name="a1", width=3)),
            ScanArm(values=(2,), instrument=_instrument(name="a2", width=1)),
        )
    return ScanMuxNode(mux_name=name, select_width=select_width, arms=arms)


def test_module_block_has_no_endmodule_keyword():
    block = render_scan_mux_module(_mux())
    assert "endmodule" not in block
    assert block.startswith("Module warptap_scan_mux_mux_a {")
    assert block.rstrip().endswith("}")


def test_has_classifiable_select_ports():
    """Same real requirement render_sib_module_type's own test confirms: a ScanInterface with
    no TMSPort/ShiftEnPort/SelectPort is rejected by the vendored icl_parser."""
    block = render_scan_mux_module(_mux())
    assert "SelectPort SEL;" in block
    assert "ToSelectPort toSEL0" in block
    assert "ToSelectPort toSEL1" in block


def test_scan_out_port_width_matches_selreg_width():
    """Regression test for a real bug caught by live-validating against the vendored
    icl_parser: SO's own declared width must match its Source (SELREG)'s width, or the
    real tool's source_check() raises a bare AssertionError (port_size == source_size)."""
    block = render_scan_mux_module(_mux(select_width=3))
    assert "ScanOutPort SO[2:0] { Source SELREG[2:0]; }" in block
    assert "ScanOutPort SO {" not in block  # must NOT be left unbracketed at width>1


def test_scan_out_port_unbracketed_at_select_width_one():
    block = render_scan_mux_module(
        _mux(
            select_width=1,
            arms=(
                ScanArm(values=(0,), instrument=_instrument(name="a1")),
                ScanArm(values=(1,), instrument=_instrument(name="a2")),
            ),
        )
    )
    assert "ScanOutPort SO { Source SELREG; }" in block


def test_each_arm_gets_its_own_host_interface_not_one_shared_interface():
    """Regression test for a real, confirmed-by-live-validation grammar rule: a host
    ScanInterface allows at most one ScanInPort (real rule f1) -- a single shared interface
    listing every fromArmK at once fails that check as soon as there are 2+ arms. Fixed by
    giving each arm its own host{k} interface instead (see render_scan_mux_module's own
    docstring)."""
    block = render_scan_mux_module(_mux())
    assert "ScanInterface host0 { Port fromArm0; Port toSI; Port toSEL0; }" in block
    assert "ScanInterface host1 { Port fromArm1; Port toSI; Port toSEL1; }" in block
    assert "ScanInterface host {" not in block


def test_mux_selectedby_clause_has_no_bypass_entry():
    """A real, deliberate limitation shared with the SIB case: real ICL's ScanMux grammar has
    no wildcard/else clause, so a value matching no declared arm is simply absent."""
    block = render_scan_mux_module(_mux())
    assert "ScanMux MUX SelectedBy SELREG[1:0] { 2'd1 : fromArm0; 2'd2 : fromArm1; }" in block


def test_arm_with_multiple_values_raises_named_error():
    mux = _mux(arms=(
        ScanArm(values=(1, 2), instrument=_instrument(name="a1")),
        ScanArm(values=(3,), instrument=_instrument(name="a2")),
    ))
    with pytest.raises(IclEmitError, match="values"):
        render_scan_mux_module(mux)


def test_mux_as_top_level_chain_entry_binds_si_to_tdi():
    graph = PhysicalGraph(chain=(_mux(),))
    lines = render_sib_instances(graph)
    joined = "\n".join(lines)
    assert "Instance warptap_scan_mux_mux_a Of warptap_scan_mux_mux_a { InputPort SI = tdi;" in joined
    assert "InputPort fromArm0 = warptap_instr_a1.SO;" in joined
    assert "InputPort fromArm1 = warptap_instr_a2.SO;" in joined


def test_mux_gated_by_hierarchy_sib_binds_fromso_to_mux_so():
    outer = SibNode(sib_name="gate_a", instrument=None, nested=(_mux(),))
    graph = PhysicalGraph(chain=(outer,))
    lines = render_sib_instances(graph)
    joined = "\n".join(lines)
    assert "fromSO = warptap_scan_mux_mux_a.SO;" in joined
    assert "InputPort SI = warptap_gate_a.toSI;" in joined  # mux's own SI, from the SIB's toSI


def test_mux_arm_gating_a_nested_hierarchy_sib():
    """The other nesting direction: one of a mux's own arms gates a nested sub-chain (a
    hierarchy SIB), rather than a leaf instrument directly."""
    inner = SibNode(sib_name="sib_inner", instrument=_instrument(name="deep"))
    mux = _mux(arms=(
        ScanArm(values=(1,), nested=(inner,)),
        ScanArm(values=(2,), instrument=_instrument(name="a2")),
    ))
    graph = PhysicalGraph(chain=(mux,))
    lines = render_sib_instances(graph)
    joined = "\n".join(lines)
    assert "InputPort fromArm0 = warptap_sib_inner.SO;" in joined
    assert "Instance warptap_sib_inner Of warptap_sib { InputPort SI = warptap_scan_mux_mux_a.toSI;" in joined


def test_arm_with_neither_instrument_nor_nested_raises_named_error():
    mux = _mux(arms=(
        ScanArm(values=(1,)),
        ScanArm(values=(2,), instrument=_instrument(name="a2")),
    ))
    graph = PhysicalGraph(chain=(mux,))
    with pytest.raises(IclEmitError, match="neither"):
        render_sib_instances(graph)


def test_arm_with_both_instrument_and_nested_raises_named_error():
    inner = SibNode(sib_name="sib_inner", instrument=_instrument(name="deep"))
    mux = _mux(arms=(
        ScanArm(values=(1,), instrument=_instrument(name="a1"), nested=(inner,)),
        ScanArm(values=(2,), instrument=_instrument(name="a2")),
    ))
    graph = PhysicalGraph(chain=(mux,))
    with pytest.raises(IclEmitError, match="both"):
        render_sib_instances(graph)


def test_zero_width_arm_instrument_raises_named_error():
    mux = _mux(arms=(
        ScanArm(values=(1,), instrument=_instrument(name="a1", width=0)),
        ScanArm(values=(2,), instrument=_instrument(name="a2")),
    ))
    graph = PhysicalGraph(chain=(mux,))
    with pytest.raises(IclEmitError, match="at least 1 bit"):
        render_sib_instances(graph)


def test_to_icl_renders_one_scan_mux_module_block():
    graph = PhysicalGraph(chain=(_mux(),))
    root = ModuleInstance(name="chip")
    text = to_icl(graph, root, include_access_link=False)
    assert text.count("Module warptap_scan_mux_mux_a {") == 1
    assert "Module warptap_instr_a1 {" in text
    assert "Module warptap_instr_a2 {" in text


def test_to_icl_collects_muxes_nested_inside_arms_too():
    """_collect_scan_muxes must walk into an arm's own nested sub-chain, not just the
    top-level chain -- a mux-behind-a-mux shape."""
    inner_mux = _mux(name="mux_inner", arms=(
        ScanArm(values=(5,), instrument=_instrument(name="deep_a")),
        ScanArm(values=(6,), instrument=_instrument(name="deep_b")),
    ))
    outer_mux = _mux(name="mux_outer", arms=(
        ScanArm(values=(1,), nested=(inner_mux,)),
        ScanArm(values=(2,), instrument=_instrument(name="a2")),
    ))
    graph = PhysicalGraph(chain=(outer_mux,))
    root = ModuleInstance(name="chip")
    text = to_icl(graph, root, include_access_link=False)
    assert "Module warptap_scan_mux_mux_outer {" in text
    assert "Module warptap_scan_mux_mux_inner {" in text


def test_to_icl_wdr_select_references_mux_when_it_is_the_first_slot():
    graph = PhysicalGraph(chain=(_mux(),))
    root = ModuleInstance(name="chip")
    text = to_icl(graph, root, include_access_link=True)
    wdr_line = next(line for line in text.splitlines() if "wdr_select" in line)
    assert "warptap_scan_mux_mux_a" in wdr_line
