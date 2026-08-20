"""Integration tests for the SVF/STAPL emitters (implementation_plan.md §7 Stage 7): feed a
REALISTIC ops sequence -- built the same way a real caller would, via
warptap.pdl_interpreter.PDLInterpreter, not a hand-crafted toy list -- through both backends,
proving the "stateless pretty-printers over one IR" property §3.4 promises end to end, not
just against isolated unit-test ops.
"""

from __future__ import annotations

from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.stapl_crc16 import stapl_file_crc
from warptap.tap_ir_stapl import to_stapl
from warptap.tap_ir_svf import to_svf

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]


def _realistic_write_only_ops():
    """A real iApply() sequence with only iWrite (no iRead) -- runnable through both
    backends, since STAPL's v1 emitter doesn't yet support the tdo/mask COMPARE clause
    (see tap_ir_stapl.py's own documented scope limit)."""
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b101)
    return pdl.iApply()


def _realistic_write_and_read_ops():
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b101)
    pdl.iRead(0b101)
    return pdl.iApply()


def test_svf_renders_a_real_iapply_sequence_with_read():
    ops = _realistic_write_and_read_ops()
    svf = to_svf(ops)
    # Phase 1 (retarget) + phase 2 (payload, with TDO/MASK since iRead was used).
    assert svf.count("SDR") == 2
    assert "TDO (" in svf
    assert "MASK (" in svf
    assert svf.startswith("ENDIR IDLE;\nENDDR IDLE;\n")


def test_stapl_renders_a_real_write_only_iapply_sequence():
    ops = _realistic_write_only_ops()
    stapl = to_stapl(ops, device="warptap-test-device", date="2026-08-20")
    assert stapl.count("DRSCAN") == 2
    assert stapl.rstrip().endswith(";")
    assert stapl.splitlines()[-1].startswith("CRC ")


def test_stapl_output_is_internally_consistent_end_to_end():
    """Not vacuous: recompute the CRC over the emitted STAPL body independently and
    confirm it matches what to_stapl() wrote, for a REAL (not toy) ops sequence -- the
    same property test_tap_ir_stapl.py proves in isolation, reconfirmed here against
    realistic, larger input."""
    ops = _realistic_write_only_ops()
    stapl = to_stapl(ops, device="warptap-test-device", date="2026-08-20")
    body, _sep, crc_line = stapl.rpartition("\nCRC ")
    assert crc_line
    assert int(crc_line[:-2], 16) == stapl_file_crc(body)


def test_svf_and_stapl_agree_on_which_ops_are_present():
    """Both backends walk the SAME ops list -- confirm they don't silently diverge on
    how many scan operations they each think are present, for identical input."""
    ops = _realistic_write_only_ops()
    svf = to_svf(ops)
    stapl = to_stapl(ops, device="warptap-test-device", date="2026-08-20")
    svf_scan_count = svf.count("SDR") + svf.count("SIR")
    stapl_scan_count = stapl.count("DRSCAN") + stapl.count("IRSCAN")
    assert svf_scan_count == stapl_scan_count == 2
