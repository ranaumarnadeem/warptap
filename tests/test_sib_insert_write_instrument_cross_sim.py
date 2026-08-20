"""RTL/Python cross-simulation for real (non-stub) functional instruments
(implementation_plan.md §7 Stage 9). Mirrors test_pdl_interpreter_cross_sim.py's exact
convention -- lower PDLInterpreter's emitted IR two ways (tap_ir_play.play() against
TapModel+SibNetworkRegister, tap_ir_play.to_cycles() against real RTL) and assert the two
engines' per-op observed-TDO streams match -- but against a WRITE instrument bound to a real
host input port and a READ instrument bound to a real host output port, not Stage 4's
fixed-value-only stub.

Deliberately fully Python-predictable (§5.1's own scoping): real_signal.v's `status_out` is
just `ctrl_in` delayed one clock, so the test author can set status_read's `capture_value`
to whatever the write+settle sequence below is engineered to produce, and SibNetworkRegister
(which has no notion of ctrl_in/status_out at all) still agrees with real RTL exactly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_play import play, shift_op_ranges, to_cycles
from warptap.tap_model import Instruction, TapModel
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4
OPCODE_EXTEST = 0

_SPECS = [
    InstrumentSpec(
        "ctrl_write", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("ctrl_in", 0),),
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
    with tempfile.TemporaryDirectory(prefix="warptap-write-instrument-cross-sim-") as tmpdir:
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


def _pdl_program(graph, root):
    """write ctrl_write=1, let status_out settle, read status_read expecting 1 -- §5.1's
    exact scenario."""
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    pdl.iApply()
    pdl.iRunLoop(3)  # >= real_signal.v's one-cycle ctrl_in -> status_out latency
    pdl.iTarget("status_read")
    pdl.iRead(1)
    pdl.iApply()
    return pdl.program


def test_write_then_read_a_real_signal_matches_python_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    ir_ops = _select_extest_ops() + _pdl_program(graph, root)

    python_observed, _reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    assert rtl_observed == python_observed


def test_written_value_persists_through_a_second_apply_on_real_rtl(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The direct contrast to test_pdl_interpreter_cross_sim.py's
    test_write_then_reapply_never_persists_on_real_rtl: unlike a READ (bc1_shift_only) stub,
    a WRITE (instrument_write) instrument has a real update latch, so a written value DOES
    survive being closed (retargeted away to a different instrument) and reopened later --
    proven here on real hardware.

    Deliberately three-apply, not two: closing ctrl_write's SIB via a DIFFERENT intermediate
    target (status_read) before reopening it is what real PDL sequencing looks like (e.g.
    Stage 9's own mem_subsystem_mbist scenario: write self_repair_start, read a DIFFERENT
    instrument, only later touch self_repair_start again). Retargeting to the exact SAME
    still-open instrument twice in a row (no intermediate target) is a narrower, documented
    v1 limitation -- see pdl_interpreter.py's iApply docstring -- not exercised here."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()

    pdl.iTarget("status_read")  # a different instrument -- closes ctrl_write's SIB
    second_ops = pdl.iApply()

    pdl.iTarget("ctrl_write")
    pdl.iRead(1)  # asserts the write IS still there -- deliberately no iWrite() here: Capture-
                    # DR (self-capturing po into shift_ff) happens before this apply's own
                    # shift/update, so what's OBSERVED is last apply's committed value,
                    # independent of whatever this apply's own (absent -> defaults to 0)
                    # payload will commit afterward
    third_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + first_ops + second_ops + third_ops
    python_observed, _reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )
    assert rtl_observed == python_observed
    # Not vacuous: check_reads() against the REAL RTL-observed stream (not the Python model's
    # own state, which would only prove the Python model agrees with itself) confirms the
    # committed 1 really was captured back -- ir_ops includes the IR-select shift, which
    # check_reads() (walking ShiftIR/ShiftDR alike) correctly skips since it carries no tdo.
    results = check_reads(ir_ops, rtl_observed)
    assert len(results) == 1
    assert results[0].passed


def test_ctrl_in_old_port_bits_are_undriven_and_unread_after_detach(
    fixtures_dir, yosys_command
):
    """Structural assertion (mirrors test_sib_insert_structural.py's style) of
    implementation_plan.md §7 Stage 9's own documented, intentional consequence: after
    insert_sib_network() detaches ctrl_in to give the instrument_write cell somewhere to
    drive, the *new* top-level ctrl_in port bits are a legal but functionally vestigial
    input -- driven/read by nothing internally, since JTAG is now the only path in."""
    netlist, _graph, _root = _build_and_insert(fixtures_dir, yosys_command)
    mod = netlist.module("real_signal")
    new_ctrl_in_bits = set(mod.data["ports"]["ctrl_in"]["bits"])

    referenced_bits: set = set()
    for cell in mod.data.get("cells", {}).values():
        for bits in cell["connections"].values():
            referenced_bits.update(b for b in bits if isinstance(b, int))

    assert new_ctrl_in_bits.isdisjoint(referenced_bits)
