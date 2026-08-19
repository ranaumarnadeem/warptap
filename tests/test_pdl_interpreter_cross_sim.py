"""RTL/Python cross-simulation for the PDL interpreter (implementation_plan.md §7 Stage 5).
Extends tests/test_sib_insert_cross_sim.py's exact convention: drives
PDLInterpreter.iApply()'s emitted IR through tap_ir_play.play() against TapModel +
SibNetworkRegister, and separately lowers the same IR to raw (tms, tdi) tuples (via
tap_ir_play.to_cycles()) for the real RTL path, asserting the two engines' per-op observed-TDO
streams match -- proving the interpreter's own stimulus generation (not just SibNetworkRegister,
already proven correct in Stage 4) is bit- and cycle-accurate against real hardware.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter, _target_layout
from warptap.sib_insert import insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_from_int, bits_to_int
from warptap.tap_ir_play import play, shift_op_ranges, to_cycles
from warptap.tap_model import Instruction, TapModel
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4
OPCODE_EXTEST = 0

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]

# Reset the TAP, then settle from TEST_LOGIC_RESET into RUN_TEST_IDLE -- the precondition
# both tap_ir_play.play() and tap_ir_play.to_cycles() require of their caller.
_RESET_LEAD_IN = [(0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0), (0, 0, 1, 1, 0, 0)]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "trivial", graph, yosys_command=yosys_command)
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


def run_on_rtl(
    fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
) -> list[int]:
    cycles = to_cycles(ir_ops)
    ranges = shift_op_ranges(ir_ops)
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1, 0, 0) for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-pdl-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "trivial_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_sib_trivial.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, _state, _instr, _cap, _shift, _upd, tdo, _y = line.split(",")
        tdo_by_cycle.append(int(tdo))
    # `cycles` (and therefore `ranges`) is indexed from 0 at the first real cycle, right
    # after the reset lead-in -- strip exactly that many trace rows before correlating.
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN):]

    return [bits_to_int(tdo_by_cycle[start : start + bits]) for start, bits in ranges]


def test_iapply_shift_through_real_network_matches_python_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b101)
    ir_ops = _select_extest_ops() + pdl.iApply()

    python_observed, _reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    assert rtl_observed == python_observed
    assert len(rtl_observed) == 3  # IR-select shift + iApply's phase-1 + phase-2 shifts


def test_sequential_iapply_to_different_targets_matches_on_real_rtl(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """implementation_plan.md's own text doesn't anticipate this: a second iApply to a
    DIFFERENT instrument must size its own phase 1 against whatever the first iApply left
    open -- proven here on real hardware, not just the Python model."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b101)
    first_ops = pdl.iApply()
    pdl.iTarget("sensor_b")
    pdl.iWrite(0b11)
    second_ops = pdl.iApply()
    ir_ops = _select_extest_ops() + first_ops + second_ops

    python_observed, reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    assert rtl_observed == python_observed
    # Not vacuous: confirm sensor_a's SIB really did close and sensor_b's really did open,
    # per the Python model's own state -- which the assertion above just proved matches RTL.
    sensor_a_slot = next(s for s in reg._slots if s.sib_name == "sib_sensor_a")
    sensor_b_slot = next(s for s in reg._slots if s.sib_name == "sib_sensor_b")
    assert sensor_a_slot.sib_po == 0
    assert sensor_b_slot.sib_po == 1


def test_write_then_reapply_never_persists_on_real_rtl(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The documented, deliberate limitation (implementation_plan.md §7 Stage 5's own
    Context, per the user's explicit decision not to fix it this stage): rtl/bc1_shift_only.v
    has no update_dr port, so a value shifted in via iWrite+iApply cannot survive a later
    Capture-DR. Write something other than sensor_a's capture_value, re-apply with an
    iRead, and confirm the SECOND apply's observed content is still the ORIGINAL
    capture_value -- a proven, intentional property, on real hardware, not a silent gap."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b010)  # deliberately not equal to sensor_a's capture_value (0b101)
    first_ops = pdl.iApply()

    pdl.iTarget("sensor_a")
    pdl.iRead(0b010)  # asserts (wrongly) that the write should still be there
    second_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + first_ops + second_ops
    python_observed, _reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )
    assert rtl_observed == python_observed

    second_phase2_observed = rtl_observed[-1]
    offset, width = _target_layout(graph, frozenset({"sib_sensor_a"}), "sib_sensor_a")
    total_bits = 3 + 1 + 1  # sensor_a open (3 content + 1 select) + sensor_b closed (1)
    position_bits = list(reversed(bits_from_int(second_phase2_observed, total_bits)))
    decoded_content = bits_to_int(position_bits[offset : offset + width])

    assert decoded_content == 0b101  # the original capture_value, never the written 0b010
