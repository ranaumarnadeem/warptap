"""Integration test for the STIL emitter (implementation_plan.md §7 Stage 13): feed a REAL
``PDLInterpreter``-driven ops sequence -- built the same way a real caller would, mirroring
tests/test_tap_ir_emitters_integration.py's own end-to-end style -- through ``to_stil()``,
including a real :class:`~warptap.tap_ir.PulsePin` op appended directly onto
``pdl.program`` the way :mod:`warptap.faultflow_retarget` (Stage 14) will.
"""

from __future__ import annotations

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import PulsePin
from warptap.tap_ir_stil import to_stil

_SPECS = [
    InstrumentSpec(
        "ctrl_write",
        width=1,
        capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("bist_start"),),
    )
]


def _load_pulse_unload_ops():
    """Mirrors Stage 14's own real shape: load stimulus, pulse a functional clock for
    capture, unload/compare the response -- the exact sequence that motivated PulsePin."""
    graph, root = build_sib_plan(_SPECS, top_name="chip")
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    pdl.iApply()
    pdl.program.append(PulsePin("sysclk", 3, hold_pins=(("pi_a", 1),)))
    pdl.iTarget("ctrl_write")
    pdl.iRead(1)
    pdl.iApply()
    return pdl.program


def test_realistic_load_pulse_unload_sequence_renders_all_three_phases():
    ops = _load_pulse_unload_ops()
    text = to_stil(ops, jtag_period="100ns", pulse_periods={"sysclk": "20ns"})
    w_lines = [l.strip() for l in text.splitlines() if l.strip().startswith("W ")]
    assert w_lines == ["W jtag_wft;", "W pulse_sysclk_wft;", "W jtag_wft;"]


def test_realistic_sequence_shows_the_read_expected_value():
    ops = _load_pulse_unload_ops()
    text = to_stil(ops, jtag_period="100ns", pulse_periods={"sysclk": "20ns"})
    assert "tdo=H;" in text or "tdo=L;" in text


def test_realistic_sequence_holds_pi_during_pulse_and_p_during_jtag():
    ops = _load_pulse_unload_ops()
    text = to_stil(ops, jtag_period="100ns", pulse_periods={"sysclk": "20ns"})
    lines = text.splitlines()
    pulse_vectors = [l for l in lines if "sysclk=1;" in l]
    assert pulse_vectors and all("pi_a=1;" in l for l in pulse_vectors)
    jtag_vectors = [l for l in lines if l.strip().startswith("V {") and "sysclk=1;" not in l]
    assert any("pi_a=P;" in l for l in jtag_vectors)
