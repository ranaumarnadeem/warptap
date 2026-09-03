"""Integration test for the ICL emitter (implementation_plan.md §7 Stage 10): feed a REALISTIC
graph -- built the same way a real caller would, via warptap.sib_plan.build_sib_plan, not
hand-crafted PhysicalGraph/ModuleInstance objects -- through to_icl(), mirroring
tests/test_tap_ir_emitters_integration.py's own end-to-end style for the SVF/STAPL emitters.
"""

from __future__ import annotations

from warptap.icl_emit import to_icl
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec(
        "status_b",
        width=1,
        capture_value=0,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("some_status"),),
    ),
    InstrumentSpec(
        "ctrl_write",
        width=1,
        capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("bist_start"),),
    ),
]


def test_realistic_network_renders_one_module_per_distinct_type():
    graph, root = build_sib_plan(_SPECS, top_name="mem_subsystem_mbist")
    text = to_icl(graph, root)

    assert "Module mem_subsystem_mbist {" in text
    assert "Module warptap_sib {" in text
    assert "Module warptap_instr_sensor_a {" in text
    assert "Module warptap_instr_status_b {" in text
    assert "Module warptap_instr_ctrl_write {" in text
    # Exactly one SIB module TYPE definition regardless of instrument count (cloned per
    # instance via Instance ... Of, mirroring sib_insert.py's own RTL precedent).
    assert text.count("Module warptap_sib {") == 1


def test_realistic_network_chains_all_three_sibs_tdi_side_first():
    graph, root = build_sib_plan(_SPECS, top_name="chip")
    text = to_icl(graph, root)

    assert "InputPort SI = tdi;" in text
    assert "InputPort SI = warptap_sib_sensor_a.SO;" in text
    assert "InputPort SI = warptap_sib_status_b.SO;" in text
    # The final SIB's SO feeds the top module's own tdo.
    assert "Source warptap_sib_ctrl_write.SO;" in text


def test_realistic_network_has_no_endmodule_anywhere():
    graph, root = build_sib_plan(_SPECS, top_name="chip")
    text = to_icl(graph, root)
    assert "endmodule" not in text


def test_write_instrument_dataoutport_and_read_instrument_capturesource_both_present():
    graph, root = build_sib_plan(_SPECS, top_name="chip")
    text = to_icl(graph, root)
    assert "DataOutPort DO[0:0]" in text  # ctrl_write
    assert "bist_start[0]" in text  # ctrl_write's real host-net binding, in a comment
    assert "some_status[0]" in text  # status_b's real host-net binding
