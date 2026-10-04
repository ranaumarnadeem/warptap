"""``to_stil``'s timing, proven on RTL. Each emitted file is replayed literally
(``tests/stil_replay.py``) against ``rtl/tap_core.v`` alone and against an inserted design, in
Icarus:

- Semi-ATE-STIL's dump compiler resolves the vectors;
- every event is applied at its time in the period;
- every TDO compare is checked at its strobe.

Positive: an IDCODE read-back, the Capture-IR pattern and a BYPASS scan on ``tap_core``, and an
EXTEST write, a ``PulsePin`` and a read-back through an inserted network all compare clean.

Controls, each on the same pattern:

- a wrong expected bit fails at that bit, and only there;
- the 0.0.3 timing (TCK rising at 1ns, TDO compared at 2ns) fails, each compare seeing the next
  bit;
- leaving the ``PulsePin`` out fails the read, so the replay really clocks the functional logic.

The 0.0.3 TCK edge with only the strobe moved before it passes: that observation found the bug.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import warptap
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_fsm import TapState
from warptap.tap_integrity import select_instruction
from warptap.tap_ir import GotoState, Runtest, ShiftDR, ShiftIR, bits_from_int
from warptap.tap_ir_stil import to_stil
from warptap.tap_model import CAPTURE_IR_PATTERN, IDCODE_VALUE, OPCODE_EXTEST
from warptap.yosys_io import ingest, write_verilog_from_json

_TAP_CORE = Path(warptap.__file__).parent / "rtl" / "tap_core.v"
_TAP_CORE_PINS = {"tie": {"external_dr_tdo": 0}, "reset_pulse": {"trst_n": 0}}
#: The replay's TRST pulse leaves the TAP in Test-Logic-Reset; one TMS=0 cycle reaches
#: Run-Test/Idle, where to_stil starts walking.
_SETTLE = [Runtest(1)]
_BYPASS = 0b1111
_BYPASS_DATA = 0b0_1011_0011

_CTRL = InstrumentSpec(
    "ctrl_write", width=1, capture_value=0,
    direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("ctrl_in", 0),),
)
_STATUS = InstrumentSpec(
    "status_read", width=1, capture_value=0,
    direction=InstrumentDirection.READ, signal_bits=(SignalBinding("status_out", 0),),
)


def _idcode_ops(expected: int = IDCODE_VALUE) -> list:
    return _SETTLE + [
        GotoState(TapState.SHIFT_DR),
        ShiftDR(32, tdi=0, tdo=expected, mask=0xFFFF_FFFF),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _with_0_0_3_timing(text: str, strobe: str = "2ns") -> str:
    """``text`` with jtag_wft's TCK and TDO waveforms as 0.0.3 wrote them: TCK falling at 0
    and rising at 1ns, TDO compared at ``strobe``."""
    text, tck = re.subn(r"tck \{ 01 \{[^}]*\}\}", "tck { 01 { '0ns' D; '1ns' U; }}", text)
    text, tdo = re.subn(
        r"tdo \{ HLX \{[^}]*\}\}", f"tdo {{ HLX {{ '0ns' X; '{strobe}' H/L/X; }}}}", text
    )
    assert tck == tdo == 1
    return text


def test_idcode_reads_back(stil_replay):
    result = stil_replay(to_stil(_idcode_ops(), jtag_period="50ns"), [_TAP_CORE], "tap_core",
                         **_TAP_CORE_PINS)
    assert result.compares == 32
    assert result.mismatches == []


def test_capture_ir_and_bypass_compare_clean(stil_replay):
    ops = _SETTLE + [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(4, tdi=_BYPASS, tdo=CAPTURE_IR_PATTERN, mask=0b1111),
        GotoState(TapState.RUN_TEST_IDLE),
        GotoState(TapState.SHIFT_DR),
        # BYPASS: the captured 0 first, then each TDI bit one cycle late.
        ShiftDR(9, tdi=_BYPASS_DATA, tdo=_BYPASS_DATA << 1, mask=0x1FF),
        GotoState(TapState.RUN_TEST_IDLE),
    ]
    result = stil_replay(to_stil(ops, jtag_period="100ns"), [_TAP_CORE], "tap_core",
                         **_TAP_CORE_PINS)
    assert result.compares == 4 + 9
    assert result.mismatches == []


def test_a_wrong_expected_bit_fails_at_that_bit_only(stil_replay):
    text = to_stil(_idcode_ops(IDCODE_VALUE ^ 1 << 7), jtag_period="50ns")
    result = stil_replay(text, [_TAP_CORE], "tap_core", **_TAP_CORE_PINS)
    assert len(result.mismatches) == 1
    assert "expected 1, got 0" in result.mismatches[0] or "expected 0, got 1" in result.mismatches[0]


def test_0_0_3_timing_compares_each_bit_against_the_next_one(stil_replay):
    text = _with_0_0_3_timing(to_stil(_idcode_ops(), jtag_period="50ns"))
    result = stil_replay(text, [_TAP_CORE], "tap_core", **_TAP_CORE_PINS)
    bits = bits_from_int(IDCODE_VALUE, 32)
    seen = bits[1:] + [0]  # after the rising edge: the next bit, then Exit1-DR's 0
    assert len(result.mismatches) == sum(b != s for b, s in zip(bits, seen)) > 0


def test_the_0_0_3_rise_with_only_the_strobe_moved_before_it_passes(stil_replay):
    text = _with_0_0_3_timing(to_stil(_idcode_ops(), jtag_period="50ns"), strobe="500ps")
    assert stil_replay(text, [_TAP_CORE], "tap_core", **_TAP_CORE_PINS).mismatches == []


@pytest.fixture(scope="module")
def real_signal_network(fixtures_dir, yosys_command, tmp_path_factory):
    graph, root = build_sib_plan([_CTRL, _STATUS], top_name="real_signal")
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    insert_sib_network(netlist, "real_signal", graph, yosys_command=yosys_command)
    path = tmp_path_factory.mktemp("stil-replay") / "real_signal_inserted.v"
    path.write_text(
        write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command), encoding="utf-8"
    )
    return graph, root, path


def _write_pulse_read(graph, root, *, expected: int = 1, pulses: int = 2) -> list:
    """Write ctrl_in=1, clock it into ctrl_latched with ``pulses`` clk pulses, read status_out."""
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    pdl.iApply()
    if pulses:
        pdl.iRunLoop(pulses, sck_port="clk")
    pdl.iTarget("status_read")
    pdl.iRead(expected)
    pdl.iApply()
    return _SETTLE + select_instruction(OPCODE_EXTEST) + pdl.program


def _replay_real_signal(stil_replay, path, text, **pins):
    return stil_replay(text, [path], "real_signal", reset_pulse={"trst_n": 0, "rst_n": 0}, **pins)


def test_write_pulse_read_through_an_inserted_network(stil_replay, real_signal_network):
    graph, root, path = real_signal_network
    text = to_stil(_write_pulse_read(graph, root), jtag_period="50ns", pulse_periods={"clk": "10ns"})
    result = _replay_real_signal(stil_replay, path, text)
    assert result.compares > 0
    assert result.mismatches == []


@pytest.mark.parametrize("control", ["wrong_expected", "0.0.3_timing", "no_pulse"])
def test_inserted_network_controls_fail(control, stil_replay, real_signal_network):
    graph, root, path = real_signal_network
    pins = {}
    if control == "wrong_expected":
        ops = _write_pulse_read(graph, root, expected=0)
    elif control == "no_pulse":
        ops = _write_pulse_read(graph, root, pulses=0)
        pins = {"tie": {"clk": 0}}
    else:
        ops = _write_pulse_read(graph, root)
    text = to_stil(ops, jtag_period="50ns", pulse_periods={"clk": "10ns"})
    if control == "0.0.3_timing":
        text = _with_0_0_3_timing(text)
    assert _replay_real_signal(stil_replay, path, text, **pins).mismatches
