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
from typing import Optional

import pytest

import warptap
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_fsm import TapState
from warptap.tap_integrity import select_instruction
from warptap.tap_ir import GotoState, PulsePin, Runtest, SetPins, ShiftDR, ShiftIR, bits_from_int
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


def _write_pulse_read(
    graph, root, *, expected: int = 1, pulses: int = 2, pulse_ops: Optional[list] = None,
    lead_in: list = _SETTLE,
) -> list:
    """After ``lead_in``, write ctrl_in=1, clock it into ctrl_latched with ``pulses`` clk
    pulses (or ``pulse_ops``), read status_out."""
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    pdl.iApply()
    if pulse_ops is not None:
        pdl.program.extend(pulse_ops)
    elif pulses:
        pdl.iRunLoop(pulses, sck_port="clk")
    pdl.iTarget("status_read")
    pdl.iRead(expected)
    pdl.iApply()
    return lead_in + select_instruction(OPCODE_EXTEST) + pdl.program


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


@pytest.mark.parametrize("rst_n, passes", [(1, True), (0, False)])
def test_declared_inputs_reach_the_dut_and_outputs_are_never_compared(
    rst_n, passes, stil_replay, real_signal_network
):
    """rst_n held by the file, not by the harness: held high the write survives the pulses;
    held low it clears ctrl_latched and the read fails. status_out is declared but adds no
    compare."""
    graph, root, path = real_signal_network
    text = to_stil(
        _write_pulse_read(graph, root), jtag_period="50ns", pulse_periods={"clk": "10ns"},
        inputs={"rst_n": rst_n, "clk": 0, "ctrl_in": 0}, outputs=["status_out"],
    )
    result = stil_replay(text, [path], "real_signal", reset_pulse={"trst_n": 0})
    assert result.compares == 1
    assert (result.mismatches == []) is passes


@pytest.mark.parametrize("unlisted, passes", [("held", True), ("forced_0_as_in_0.0.3", False)])
def test_a_pulse_leaves_a_pin_it_does_not_list_where_it_was(
    unlisted, passes, stil_replay, real_signal_network
):
    """rst_n is undeclared and driven only by the first pulse's hold_pins. The second pulse
    doesn't list it, so it stays 1 and the read passes. Driven 0 there instead, as 0.0.3 did,
    the pulse resets ctrl_latched and the read fails."""
    graph, root, path = real_signal_network
    pulse_ops = [PulsePin("clk", 1, hold_pins=(("rst_n", 1),)), PulsePin("clk", 1)]
    text = to_stil(_write_pulse_read(graph, root, pulse_ops=pulse_ops), jtag_period="50ns",
                   pulse_periods={"clk": "10ns"})
    if unlisted != "held":
        lines = text.splitlines()
        pulse_lines = [i for i, l in enumerate(lines) if "clk=1;" in l]
        assert "rst_n=1;" in lines[pulse_lines[0]] and "rst_n=P;" in lines[pulse_lines[1]]
        lines[pulse_lines[1]] = lines[pulse_lines[1]].replace("rst_n=P;", "rst_n=0;")
        text = "\n".join(lines) + "\n"
    result = stil_replay(text, [path], "real_signal", reset_pulse={"trst_n": 0})
    assert result.compares == 1
    assert (result.mismatches == []) is passes


#: Every real_signal pin declared, so the harness ties and resets nothing.
_REAL_SIGNAL_PINS = {
    "inputs": {"trst_n": 1, "rst_n": 1, "clk": 0, "ctrl_in": 0}, "outputs": ["status_out"],
}
_TRST_LEAD_IN = [
    SetPins((("trst_n", 0), ("rst_n", 0))), GotoState(TapState.TEST_LOGIC_RESET),
    SetPins((("trst_n", 1), ("rst_n", 1))), GotoState(TapState.RUN_TEST_IDLE),
]
_TMS_ONLY_LEAD_IN = [GotoState(TapState.TEST_LOGIC_RESET), GotoState(TapState.RUN_TEST_IDLE)]


@pytest.mark.parametrize(
    "lead_in, passes", [(_TRST_LEAD_IN, True), (_TMS_ONLY_LEAD_IN, False)], ids=["trst", "tms_only"]
)
def test_a_reset_lead_in_in_the_file_resets_the_network(
    lead_in, passes, stil_replay, real_signal_network
):
    """trst_n and rst_n are 0 from time zero, so they never fall: only TCK edges while TRST is
    low reset the TAP and the SIB network, and the lead-in gives five. A TMS-only reset
    leaves the network's cells unknown, so the read fails."""
    graph, root, path = real_signal_network
    ops = _write_pulse_read(graph, root, lead_in=lead_in)
    text = to_stil(ops, jtag_period="50ns", pulse_periods={"clk": "10ns"}, **_REAL_SIGNAL_PINS)
    initial = {"trst_n": 0, "rst_n": 0} if passes else {"trst_n": 1, "rst_n": 1}
    result = stil_replay(text, [path], "real_signal", initial=initial)
    assert result.compares == 1
    assert (result.mismatches == []) is passes


def test_idcode_reads_back_after_the_lead_in_with_trst_low_from_time_zero(stil_replay):
    lead_in = [
        SetPins((("trst_n", 0),)), GotoState(TapState.TEST_LOGIC_RESET),
        SetPins((("trst_n", 1),)), GotoState(TapState.RUN_TEST_IDLE),
    ]
    text = to_stil(lead_in + _idcode_ops()[len(_SETTLE):], jtag_period="50ns",
                   inputs={"trst_n": 1, "external_dr_tdo": 0})
    result = stil_replay(text, [_TAP_CORE], "tap_core", initial={"trst_n": 0})
    assert result.compares == 32
    assert result.mismatches == []


_COUNT = InstrumentSpec(
    "count_read", width=8, capture_value=0, direction=InstrumentDirection.READ,
    signal_bits=tuple(SignalBinding("count_out", i) for i in range(8)),
)


@pytest.fixture(scope="module")
def counter_network(fixtures_dir, yosys_command, tmp_path_factory):
    """stil_counter.v with an 8-bit READ instrument on count_out: every clk pulse adds the
    4-bit input step to the count."""
    graph, root = build_sib_plan([_COUNT], top_name="stil_counter")
    raw = ingest([fixtures_dir / "stil_counter.v"], "stil_counter", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    insert_sib_network(netlist, "stil_counter", graph, yosys_command=yosys_command)
    path = tmp_path_factory.mktemp("stil-replay") / "stil_counter_inserted.v"
    path.write_text(
        write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command), encoding="utf-8"
    )
    return graph, root, path


def _bits(name: str, width: int, value: int) -> dict:
    return {f"{name}[{i}]": (value >> i) & 1 for i in range(width)}


def _count_text(graph, root, *, step: int, pulses: int, expected: int, run_test: int = 0) -> str:
    """Every stil_counter pin declared, step held per bit; the TRST lead-in also clears the
    count through rst_n."""
    pdl = PDLInterpreter(graph, root)
    if run_test:
        pdl.iRunLoop(run_test)
    pdl.iRunLoop(pulses, sck_port="clk")
    pdl.iTarget("count_read")
    pdl.iRead(expected)
    pdl.iApply()
    ops = _TRST_LEAD_IN + select_instruction(OPCODE_EXTEST) + pdl.program
    return to_stil(
        ops, jtag_period="50ns", pulse_periods={"clk": "10ns"},
        inputs={"trst_n": 1, "rst_n": 1, "clk": 0, **_bits("step", 4, step)},
        outputs=[f"count_out[{i}]" for i in range(8)],
    )


def _replay_counter(stil_replay, path, text):
    return stil_replay(text, [path], "stil_counter", initial={"trst_n": 0, "rst_n": 0})


@pytest.mark.parametrize("swap", [False, True], ids=["as_emitted", "step_0_and_3_swapped"])
def test_per_bit_inputs_reach_their_own_bits(swap, stil_replay, counter_network):
    """step = 0101 held per bit, three pulses: the count reads back 15. With the STIL names of
    step[0] and step[3] swapped the DUT sees 1100 and counts 36, so the read fails."""
    graph, root, path = counter_network
    text = _count_text(graph, root, step=0b0101, pulses=3, expected=15)
    assert '"step[0]"=1; "step[1]"=0; "step[2]"=1; "step[3]"=0;' in text
    if swap:
        text = text.replace('"step[0]"', "SWAP").replace('"step[3]"', '"step[0]"')
        text = text.replace("SWAP", '"step[3]"')
    result = _replay_counter(stil_replay, path, text)
    assert result.compares == 8
    assert (result.mismatches == []) is not swap
