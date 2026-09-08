"""RTL/Python cross-simulation for a NESTED (SIB-gates-SIB) network, against real synthesized
RTL -- the test the nested-SIB implementation plan's Phase 4 calls for, closing the loop from
Phase 0's standalone RTL spike (tests/test_sib_cell_nested_cross_sim.py) against real
insert_sib_network() output.

Deliberately does NOT compare against SibNetworkRegister (unlike test_pdl_interpreter_cross_
sim.py/test_sib_insert_write_instrument_cross_sim.py's own convention) -- that Python
behavioral oracle doesn't model nested networks yet (a separate, later phase of the same
plan). Instead, this uses pdl_verify.check_reads() directly against the real RTL-observed
stream: it only ever reads a ShiftDR op's own tdo/mask fields, already correctly computed by
PDLInterpreter.iApply() via compose_bits/_read_mask_bits (both already proven correct for
nested networks by unit tests), so it needs no Python-side register model at all. A real WRITE
committed through a nested SIB, later confirmed correct via a real READ elsewhere in the
network, is strong end-to-end evidence independent of SibNetworkRegister's own catch-up.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import HierarchySpec, InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_play import shift_op_ranges, to_cycles
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4

_SPECS = [
    HierarchySpec(
        "bank_a",
        children=[
            InstrumentSpec(
                "ctrl_write", width=1, capture_value=0,
                direction=InstrumentDirection.WRITE,
                signal_bits=(SignalBinding("ctrl_in", 0),),
            ),
        ],
    ),
    InstrumentSpec(
        "status_read", width=1, capture_value=1,  # what ctrl_in=1 settles status_out to
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("status_out", 0),),
    ),
]

_RESET_LEAD_IN = [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 1, 1)]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "real_signal", graph, yosys_command=yosys_command)
    return netlist, graph, root


def _render_stimulus(rows) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops):
    cycles = to_cycles(ir_ops)
    ranges = shift_op_ranges(ir_ops)
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1) for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-nested-insert-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "real_signal_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_real_signal.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, _state, _instr, _cap, _shift, _upd, tdo, _status = line.split(",")
        tdo_by_cycle.append(int(tdo))
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN):]

    return [bits_to_int(tdo_by_cycle[start : start + bits]) for start, bits in ranges]


def test_write_through_a_nested_sib_reaches_a_real_host_signal(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """ctrl_write sits behind sib_bank_a (a hierarchy SIB with no instrument of its own) --
    reaching it needs iApply's own multi-round opening (bank_a first, then ctrl_write, per
    stage_open_sequence), exercised here for the first time against a real inserted network,
    not just the pure-Python unit tests already covering the arithmetic. status_read is a
    flat, ordinary top-level instrument -- confirms a nested write and a flat read coexist
    correctly in the same network."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()  # 2 phase-1 rounds (open bank_a, then ctrl_write) + phase 2

    pdl.iRunLoop(3)  # >= real_signal.v's one-cycle ctrl_in -> status_out latency
    pdl.iTarget("status_read")
    pdl.iRead(1)  # asserts ctrl_in really did reach status_out via the nested write
    second_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + first_ops + second_ops
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    results = check_reads(ir_ops, rtl_observed)
    assert len(results) == 1
    assert results[0].passed


def test_nested_write_survives_being_closed_and_reopened_on_real_rtl(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """Mirrors test_sib_insert_write_instrument_cross_sim.py's own written-value-persists
    case, but through a nested SIB: instrument_write.v has a real update latch, so the
    committed value survives sib_bank_a (and ctrl_write) being closed via a different
    intermediate target (status_read) and later reopened -- proven on real hardware."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()

    pdl.iTarget("status_read")  # unrelated flat target -- closes sib_bank_a/ctrl_write
    second_ops = pdl.iApply()

    pdl.iTarget("ctrl_write")
    pdl.iRead(1)  # asserts the write is still committed -- no iWrite() here, so this only
                   # observes whatever instrument_write.v's own po already holds
    third_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + first_ops + second_ops + third_ops
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    results = check_reads(ir_ops, rtl_observed)
    assert len(results) == 1
    assert results[0].passed
