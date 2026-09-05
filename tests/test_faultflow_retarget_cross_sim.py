"""RTL cross-simulation for faultflow pattern retargeting's real functional-clock-pulse
mechanism (implementation_plan.md §7 Stage 14) -- the gold-standard validation tier this
project reserves for exactly this kind of correctness-critical translation (mirrors Stage 9's
own ``pdl_verify.py`` discipline). Every prior cross-sim fixture in this project ties
``clk``=``tck`` (the JTAG TAP and any functional logic share one physical clock); this is the
first to genuinely separate them -- ``functional_clock.v``'s own ``captured`` register is
clocked by ``sysclk`` alone, never ``tck``, so this test proves
:class:`~warptap.tap_ir.PulsePin` actually pulses an independent clock domain against real
simulated hardware, not just that the Python/STIL plumbing is self-consistent with itself.

Scenario: a faultflow-shaped pattern loads ``stim_write``=1 (chain 0), pulses ``sysclk`` once
(the functional capture edge), then reads back ``captured_read`` (chain 1) expecting 1 --
proving the whole Stage 14 pipeline (bit-order/padding handling, ``PDLInterpreter``-driven
retargeting, ``PulsePin`` insertion) against real RTL, not a self-written model.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Tuple

from warptap.faultflow_retarget import retarget_faultflow_patterns
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR, bits_from_int, bits_to_int
from warptap.tap_ir_play import navigation_tms, shift_tms
from warptap.tap_model import Instruction
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4

_SPECS = [
    InstrumentSpec(
        "stim_write", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("stim_in", 0),),
    ),
    InstrumentSpec(
        "captured_read", width=1, capture_value=0,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("captured_out", 0),),
    ),
]

_RESET_LEAD_IN = [(0, 0, 0, 0, 0), (0, 0, 0, 0, 0), (0, 0, 1, 1, 0)]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # Instruction.EXTEST == 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _stimulus_rows(ir_ops) -> List[Tuple[int, int, int, int, int]]:
    """(tms, tdi, trst_n, rst_n, pulse_flag) per row -- a PulsePin-aware extension of
    tap_ir_play.to_cycles()'s own per-cycle walk (that function has no concept of PulsePin
    at all and would raise). Reuses the exact same public navigation_tms/shift_tms helpers
    tap_ir_stil.py's own walker does."""
    state = TapState.RUN_TEST_IDLE
    rows: List[Tuple[int, int, int, int, int]] = []

    def jtag_row(tms: int, tdi: int) -> None:
        nonlocal state
        rows.append((tms, tdi, 1, 1, 0))
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
                rows.append((0, 0, 1, 1, 1))
        else:
            raise ValueError(f"unsupported op {op!r}")
    return rows


def _correlate_skipping_pulses(ir_ops, tdo_by_cycle: List[int]) -> List[int]:
    """tap_ir_play.shift_op_ranges()-equivalent, extended for PulsePin: a PulsePin op
    consumes rows (advances the row index) but contributes no ShiftIR/ShiftDR-correlatable
    entry -- matches _stimulus_rows()'s own row accounting exactly."""
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


def test_pulsepin_drives_a_real_independent_functional_clock_edge(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    raw = ingest([fixtures_dir / "functional_clock.v"], "functional_clock", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS, top_name="functional_clock")
    insert_sib_network(netlist, "functional_clock", graph, yosys_command=yosys_command)

    patterns = [
        {
            "load_seqs": {"0": [True]},
            "expected_unload": {"1": [True]},
            "capture_pi_values": {},
        }
    ]
    retargeted = retarget_faultflow_patterns(
        patterns,
        {0: "stim_write", 1: "captured_read"},
        graph,
        root,
        clock_port="sysclk",
        clock_pulse_count=1,
    )
    ir_ops = _select_extest_ops() + retargeted

    rows = _RESET_LEAD_IN + _stimulus_rows(ir_ops)
    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-faultflow-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "functional_clock_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_functional_clock.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, _state, _instr, _cap, _shift, _upd, tdo, _captured = line.split(",")
        tdo_by_cycle.append(int(tdo))
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN):]

    observed = _correlate_skipping_pulses(ir_ops, tdo_by_cycle)
    results = check_reads(ir_ops, observed)

    assert len(results) == 1  # exactly one iRead was queued (captured_read)
    assert results[0].passed, (
        f"real RTL did not capture the loaded stimulus through the independent sysclk pulse: "
        f"expected={results[0].expected}, observed={results[0].observed}, mask={results[0].mask}"
    )
