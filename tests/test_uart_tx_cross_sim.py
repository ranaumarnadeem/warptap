"""RTL cross-simulation against a second real external design (implementation_plan.md §7's
"industry-grade gaps" -- breadth of real-world proof, not a known defect): alexforencich/
verilog-uart's `uart_tx.v` (MIT licensed), read live from a sibling checkout exactly like
openMBIST's `mem_subsystem_mbist` already is (`verilog_uart_dir` fixture in conftest.py).

Unlike every `mem_subsystem_mbist` test (a single functional clock, `clk`, driven the SAME
physical clock as this project's own `tck` throughout), `uart_tx.v` genuinely needs its own
independently-pulsed functional clock -- a full transmission takes 81 clk cycles at
`prescale=1` (confirmed empirically against the real RTL directly: 8 cycles/bit-time, not the
naive `prescale*8` for every bit -- the last, stop-bit bit-time is deliberately 9 cycles, one
longer, per `uart_tx.v`'s own `bit_cnt==1` branch setting `prescale_reg <= (prescale<<3)`
with no `-1`), with no useful relationship to how many TCK cycles any given JTAG shift
happens to take. So this test reuses Stage 14's `PulsePin` mechanism instead (the same
independent-clock-domain primitive `test_faultflow_retarget_cross_sim.py`/
`test_mem_subsystem_mbist_faultflow_cross_sim.py` already prove against real RTL), here via
`PDLInterpreter.iRunLoop(count, sck_port="clk")` -- real PDL's own `-sck` selector -- rather
than the lower-level `retarget_faultflow_patterns` API those tests use, since this scenario is
an ordinary direct `PDLInterpreter` program (mirrors `test_sib_insert_write_instrument_cross_
sim.py`'s own style), not a faultflow-pattern-loading one.

Validation scope (as approved): one WRITE instrument drives `s_axis_tdata`/`s_axis_tvalid`
together (`axis_write`, 9 bits: bits[7:0]=tdata, bit[8]=tvalid -- one instrument, not two, so
a single `iWrite`/`iApply` commits both atomically); a second, one-time WRITE instrument sets
`prescale=1`; a READ instrument (`busy_read`) confirms `busy` toggles 0 -> 1 -> 0 across the
transmission; and -- the strong end-to-end proof -- the testbench (`tb_uart_tx.v`) traces
`txd` directly every functional clock pulse, never through JTAG, so this test decodes the
actual transmitted byte in Python and confirms it matches what was written.

`uart_tx.v`'s own `rst` is active-HIGH and, unlike every prior fixture's DUT reset, is
SYNCHRONOUS ONLY (`always @(posedge clk) if (rst) ...`, no separate async sensitivity) -- so
`_RESET_LEAD_IN` below includes one PulsePin-style pulse row with `rst` held high, not just
the level, to actually commit the reset (see tb_uart_tx.v's own docstring)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Tuple

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR, bits_from_int, bits_to_int
from warptap.tap_ir_play import navigation_tms, shift_tms
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4
TDATA_BYTE = 0xA5  # 0b10100101 -- distinguishable, not a palindrome, not all-0/all-1

_SPECS = [
    InstrumentSpec(
        "axis_write", width=9, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=tuple(SignalBinding("s_axis_tdata", i) for i in range(8))
        + (SignalBinding("s_axis_tvalid"),),
    ),
    InstrumentSpec(
        "prescale_write", width=16, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=tuple(SignalBinding("prescale", i) for i in range(16)),
    ),
    InstrumentSpec(
        "busy_read", width=1, capture_value=0,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("busy"),),
    ),
]

# (tms, tdi, trst_n, rst, pulse) -- the middle row is a real PulsePin cycle (not just a held
# level) so uart_tx's own synchronous-only reset actually commits.
_RESET_LEAD_IN = [
    (0, 0, 0, 1, 0),
    (0, 0, 0, 1, 1),
    (0, 0, 1, 0, 0),
]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _pdl_program(graph, root) -> list:
    """prescale=1 once; confirm busy=0; drive tdata=TDATA_BYTE+tvalid=1; pulse clk once to
    trigger acceptance (bit_cnt/prescale_reg already 0 from reset, so this single pulse lands
    exactly on uart_tx's own accept-and-start edge); clear tvalid (prevents auto-restart once
    the frame completes, since tready pulses for exactly one cycle at frame-end and uart_tx
    has no separate same-cycle tready check gating a fresh start); confirm busy=1 mid-frame;
    pulse clk 90 more times (81 needed for the full frame + 10 idle margin); confirm busy=0
    again -- also proving the tvalid clear really did prevent a second frame from auto-
    starting, since a stuck-on tvalid would leave busy at 1 here instead."""
    pdl = PDLInterpreter(graph, root)

    pdl.iTarget("prescale_write")
    pdl.iWrite(1)
    pdl.iApply()

    pdl.iTarget("busy_read")
    pdl.iRead(0)
    pdl.iApply()

    pdl.iTarget("axis_write")
    pdl.iWrite(TDATA_BYTE | (1 << 8))
    pdl.iApply()

    pdl.iRunLoop(1, sck_port="clk")

    pdl.iTarget("axis_write")
    pdl.iWrite(0)
    pdl.iApply()

    pdl.iTarget("busy_read")
    pdl.iRead(1)
    pdl.iApply()

    pdl.iRunLoop(90, sck_port="clk")

    pdl.iTarget("busy_read")
    pdl.iRead(0)
    pdl.iApply()

    return pdl.program


def _stimulus_rows(ir_ops) -> List[Tuple[int, int, int, int, int]]:
    """(tms, tdi, trst_n, rst, pulse_flag) per row -- a PulsePin-aware extension of
    tap_ir_play.to_cycles()'s own per-cycle walk (that function has no concept of PulsePin at
    all and would raise), mirroring test_faultflow_retarget_cross_sim.py's own
    _stimulus_rows() exactly, with `rst` (uart_tx's own reset column, held low/inactive
    throughout ordinary operation) in place of that file's `rst_n`."""
    state = TapState.RUN_TEST_IDLE
    rows: List[Tuple[int, int, int, int, int]] = []

    def jtag_row(tms: int, tdi: int) -> None:
        nonlocal state
        rows.append((tms, tdi, 1, 0, 0))
        state = next_state(state, tms)

    for op in ir_ops:
        if isinstance(op, GotoState):
            for tms in navigation_tms(state, op.state):
                jtag_row(tms, 0)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            fed_bits = bits_from_int(op.tdi, op.bits)
            for tms, tdi in zip(shift_tms(op.bits), fed_bits):
                jtag_row(tms, tdi)
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                jtag_row(0, 0)
        elif isinstance(op, PulsePin):
            for _ in range(op.count):
                rows.append((0, 0, 1, 0, 1))
        else:
            raise ValueError(f"unsupported op {op!r}")
    return rows


def _correlate_skipping_pulses(ir_ops, tdo_by_cycle: List[int]) -> List[int]:
    """tap_ir_play.shift_op_ranges()-equivalent, extended for PulsePin -- identical accounting
    to test_faultflow_retarget_cross_sim.py's own _correlate_skipping_pulses()."""
    state = TapState.RUN_TEST_IDLE
    index = 0
    observed: List[int] = []
    for op in ir_ops:
        if isinstance(op, GotoState):
            tms_seq = navigation_tms(state, op.state)
            for tms in tms_seq:
                state = next_state(state, tms)
            index += len(tms_seq)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            observed.append(bits_to_int(tdo_by_cycle[index : index + op.bits]))
            for tms in shift_tms(op.bits):
                state = next_state(state, tms)
            index += op.bits
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                state = next_state(state, 0)
            index += op.count
        elif isinstance(op, PulsePin):
            index += op.count
        else:
            raise ValueError(f"unsupported op {op!r}")
    return observed


def _render_stimulus(rows) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def _parse_bit(raw: str) -> int:
    """uart_tx.v's own txd_reg/busy_reg sit at X until the reset pulse in _RESET_LEAD_IN
    commits (confirmed: Icarus prints 'x' for exactly the two rows before that pulse resolves
    them, matching this pipeline's own netlist round-trip not preserving `reg = <literal>`
    initial values the way simulating uart_tx.v directly would). Both rows fall within
    _RESET_LEAD_IN, always sliced off before use, so the placeholder value is never load-
    bearing -- this just needs to not crash on it."""
    return int(raw) if raw not in ("x", "z", "X", "Z") else 0


def _decode_transmitted_byte(txd_by_pulse: List[int]) -> int:
    """txd_by_pulse is the txd trace sampled on every PulsePin (clk) row, in order -- the raw,
    never-through-JTAG waveform. tb_uart_tx.v's own trace line prints BEFORE that row's clk
    toggle (matching tb_functional_clock.v's established convention), so txd_by_pulse[0] is
    the PRE-pulse baseline (idle, txd=1 -- captured before pulse #1 has happened at all), and
    each pulse row's OWN effect only becomes visible in the NEXT sample -- confirmed directly
    against the real RTL trace (not assumed): txd_by_pulse[1:9] is the start bit (8 samples of
    0), and txd_by_pulse[9 + 8*k] is data bit k's first (and, since each bit is held 8
    identical samples, representative) sample. LSB-first, one start bit then 8 data bits then
    a 9-cycle stop bit -- confirmed against a standalone RTL spike of uart_tx.v before this
    test was written."""
    return sum(txd_by_pulse[9 + 8 * k] << k for k in range(8))


def test_uart_tx_transmits_the_written_byte_on_real_rtl(
    verilog_uart_dir, yosys_command, iverilog_command, vvp_command, tmp_path
):
    source = verilog_uart_dir / "rtl" / "uart_tx.v"
    assert source.is_file(), f"expected real verilog-uart source missing: {source}"

    raw = ingest([source], "uart_tx", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS, top_name="uart_tx")
    insert_sib_network(netlist, "uart_tx", graph, yosys_command=yosys_command)

    ir_ops = _select_extest_ops() + _pdl_program(graph, root)
    rows = _RESET_LEAD_IN + _stimulus_rows(ir_ops)

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-uart-tx-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "uart_tx_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, Path(__file__).parent / "fixtures" / "tb_uart_tx.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle: List[int] = []
    txd_by_cycle: List[int] = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, _state, _instr, _cap, _shift, _upd, tdo, txd, _busy = line.split(",")
        tdo_by_cycle.append(_parse_bit(tdo))
        txd_by_cycle.append(_parse_bit(txd))
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN):]
    txd_by_cycle = txd_by_cycle[len(_RESET_LEAD_IN):]

    pulse_flags = [row[4] for row in rows[len(_RESET_LEAD_IN):]]
    txd_by_pulse = [txd for txd, is_pulse in zip(txd_by_cycle, pulse_flags) if is_pulse]
    assert len(txd_by_pulse) == 91  # 1 (accept) + 90 (this program's own two iRunLoop counts)

    decoded_byte = _decode_transmitted_byte(txd_by_pulse)
    assert decoded_byte == TDATA_BYTE, (
        f"real RTL's own txd waveform decoded to 0x{decoded_byte:02x}, expected "
        f"0x{TDATA_BYTE:02x} -- the byte written via JTAG did not reach the wire"
    )

    # Also confirmed through JTAG itself (the three busy_read touches): 0 at rest, 1 mid-frame,
    # 0 again once the frame (and this test's own tvalid clear) finish -- not vacuous, since
    # check_reads() runs against the real RTL-observed stream, not the Python model's own state.
    observed = _correlate_skipping_pulses(ir_ops, tdo_by_cycle)
    results = check_reads(ir_ops, observed)
    assert len(results) == 3
    assert [r.passed for r in results] == [True, True, True], (
        f"one or more busy_read checks failed against real RTL: {results}"
    )
