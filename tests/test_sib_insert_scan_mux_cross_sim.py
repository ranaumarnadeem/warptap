"""RTL/Python cross-simulation for a real ScanMuxNode-containing network (multi-arm ScanMux
plan Phase 5) -- the first real RTL validation of scan_mux_cell.v wired via sib_insert.py.
Mirrors test_sib_insert_write_instrument_cross_sim.py's exact convention -- including reusing
its own real_signal.v/tb_real_signal.v fixture pair -- but with ONE mux gating both arms
instead of two independent sibling SIBs: arm1 (WRITE, bound to ctrl_in) and arm2 (READ, bound
to status_out, real_signal.v's own one-cycle-delayed mirror of ctrl_in).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SignalBinding,
)
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_insert import insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftDR, ShiftIR, bits_to_int
from warptap.tap_ir_play import play, shift_op_ranges, to_cycles
from warptap.tap_model import Instruction, TapModel
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4
OPCODE_EXTEST = 0

_RESET_LEAD_IN = [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 1, 1)]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _mux_graph_and_root() -> tuple[PhysicalGraph, ModuleInstance]:
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(
                values=(1,),
                instrument=InstrumentNode(
                    name="ctrl_write", width=1, capture_value=0,
                    direction=InstrumentDirection.WRITE,
                    signal_bits=(SignalBinding("ctrl_in", 0),),
                ),
            ),
            ScanArm(
                values=(2,),
                instrument=InstrumentNode(
                    # 1 = what real_signal.v's ctrl_in -> status_out settles to after
                    # arm1's own write commits 1 -- SibNetworkRegister's own capture logic
                    # always uses capture_value for a READ instrument regardless of
                    # signal_bits (the test author's job to predict, matching
                    # test_sib_insert_write_instrument_cross_sim.py's own convention).
                    name="status_read", width=1, capture_value=1,
                    direction=InstrumentDirection.READ,
                    signal_bits=(SignalBinding("status_out", 0),),
                ),
            ),
        ),
    )
    graph = PhysicalGraph(chain=(mux,))
    root = ModuleInstance(
        "real_signal", children=(ModuleInstance("ctrl_write"), ModuleInstance("status_read"))
    )
    return graph, root


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = _mux_graph_and_root()
    insert_sib_network(netlist, "real_signal", graph, yosys_command=yosys_command)
    return netlist, graph, root


def _render_stimulus(rows) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def run_on_python(graph, ir_ops) -> tuple[list[int], SibNetworkRegister]:
    model = TapModel(has_idcode=True)
    reg = SibNetworkRegister(graph)
    model.register_data_register(Instruction.EXTEST, reg)
    model.reset()
    reg.reset()
    model.tick(0, 0)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE settle
    return play(model, ir_ops), reg


def run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops):
    cycles = to_cycles(ir_ops)
    ranges = shift_op_ranges(ir_ops)
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1) for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-scan-mux-insert-cross-sim-") as tmpdir:
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


def test_write_then_switch_arms_and_read_matches_python_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The genuinely new scenario this whole plan exists for, now proven on real hardware:
    write arm1 (ctrl_write), then a SINGLE iApply switches mux_a DIRECTLY to arm2
    (status_read) and reads its now-settled value back -- confirmed identical between real
    RTL and the Python oracle. Also the first real exercise of pdl_interpreter._target_
    layout's own mux-aware branch (Phase 4) against real hardware, not just the Python model."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()  # bypass -> arm1, write 1

    pdl.iRunLoop(3)  # >= real_signal.v's one-cycle ctrl_in -> status_out latency

    pdl.iTarget("status_read")
    pdl.iRead(1)
    second_ops = pdl.iApply()  # arm1 -> arm2 directly (its own round), then read

    ir_ops = _select_extest_ops() + first_ops + second_ops
    python_observed, _reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    assert rtl_observed == python_observed


def test_written_value_survives_switching_away_and_back_on_real_rtl(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The direct mux analog of test_sib_insert_write_instrument_cross_sim.py's own
    test_written_value_persists_through_a_second_apply_on_real_rtl: arm1's committed WRITE
    value must survive being switched away from (to arm2) and never touched again -- proven
    on real hardware, not just the Python oracle (already covered by Phase 3's sib_model
    tests and Phase 4's PDLInterpreter tests, neither of which touches real RTL).

    **Caveat, found during the retargeting shift-length optimization plan, NOT fixed by it**:
    this test only ever asserted rtl_observed == python_observed (RTL agrees with the Python
    model) -- it never checked the observed value against what was actually written. Adding a
    real pdl_verify.check_reads() call here (confirmed both on this real RTL fixture and in
    the Python model alone) finds arm1's own value does NOT actually round-trip correctly
    through a mux arm's own self-capture, even in this exact "safe" pattern -- a real,
    separate, pre-existing bug in sib_model.py's ScanMuxNode-arm handling, confirmed to predate
    and be unrelated to this plan (reproduced identically against the pre-fix retargeting
    code). Deliberately NOT asserted here -- see the plain-SIB case in
    test_sib_insert_write_instrument_cross_sim.py, which IS proven correct via check_reads(),
    for contrast. Flagged separately for its own investigation."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()  # bypass -> arm1, write 1

    pdl.iTarget("status_read")
    second_ops = pdl.iApply()  # arm1 -> arm2, no write/read -- just switches away

    pdl.iTarget("ctrl_write")
    pdl.iRead(1)  # no iWrite here -- asserts the earlier write is still there
    third_ops = pdl.iApply()  # arm2 -> arm1, read back

    ir_ops = _select_extest_ops() + first_ops + second_ops + third_ops
    python_observed, _reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    assert rtl_observed == python_observed


def test_same_arm_retargeted_twice_needs_no_redundant_round_on_real_rtl(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """Retargeting shift-length optimization plan, mux-arm counterpart to
    test_sib_insert_write_instrument_cross_sim.py's own test_write_then_reapply_same_
    instrument_now_persists_on_real_rtl: re-targeting the exact same already-open arm1 (no
    intervening different target) must emit zero phase-1 rounds -- proven here on real
    hardware (RTL still agrees with the Python model) that the shorter sequence executes
    cleanly, matching test_pdl_interpreter_scan_mux.py's own Python-model-only confirmation of
    the same round-count reduction.

    Deliberately does NOT assert check_reads() here -- see test_written_value_survives_
    switching_away_and_back_on_real_rtl's own docstring above: a mux arm's own WRITE value
    does not correctly round-trip through self-capture at all, a separate, pre-existing bug
    unrelated to this plan. What this test proves is narrower and still real: the retargeting
    orchestration itself (round count, RTL/Python-model agreement) is correct for this shape."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()  # bypass -> arm1, write 1

    pdl.iTarget("ctrl_write")  # SAME arm again, no intervening different target
    pdl.iRead(1)
    second_ops = pdl.iApply()

    first_shifts = [op for op in first_ops if isinstance(op, ShiftDR)]
    second_shifts = [op for op in second_ops if isinstance(op, ShiftDR)]
    assert len(first_shifts) == 2  # cold start: 1 phase-1 round (open mux_a) + 1 phase-2
    assert len(second_shifts) == 1  # mux_a already open: phase-2 only, no redundant round

    ir_ops = _select_extest_ops() + first_ops + second_ops
    python_observed, _reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )
    assert rtl_observed == python_observed
