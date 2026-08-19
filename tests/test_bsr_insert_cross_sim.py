"""RTL/Python cross-simulation for the inserted boundary-scan chain (implementation_plan.md §7
Stage 3, testing strategy item 3): drives identical stimulus through the real inserted RTL
(via Icarus) and through TapModel + BoundaryScanRegister (registered as EXTEST's data
register — precisely the extension point tap_model.TapModelError already names), asserting
identical per-cycle traces. Mirrors tests/test_tap_fsm_cross_sim.py's shape.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.bsr_insert import insert_bsr
from warptap.bsr_model import BoundaryScanRegister
from warptap.bsr_plan import build_bsr_plan
from warptap.netlist import Netlist
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_model import Instruction, TapModel
from warptap.yosys_io import ingest, write_verilog_from_json

OPCODE_EXTEST = 0
IR_WIDTH = 4


class _TrivialFunctionalModel:
    """Minimal Python mirror of the fixture's own functional logic
    (``always @(posedge clk or negedge rst_n) q <= !rst_n ? 0 : a & b; assign y = q;``)
    — needed so the OUTPUT3 cell's ``func_in`` (plan §3 step 6) can be fed a live
    value each cycle, matching what the real RTL's ``y_pre_bsr`` net carries."""

    def __init__(self):
        self.q = 0

    def current_y(self) -> int:
        return self.q

    def clock(self, rst_n: int, a: int, b: int) -> None:
        self.q = 0 if not rst_n else (a & b)


def _directed_stimulus() -> list[tuple[int, int, int, int, int, int]]:
    """(tms, tdi, trst_n, rst_n, a, b) per cycle: reset, select EXTEST via a real
    IR shift (last bit rides the Shift-IR exit edge — the exact timing lesson from
    Stage 2's cross-sim work: every cycle resident in Shift-IR/Shift-DR shifts,
    including the cycle that exits), shift a deliberately-chosen pattern through
    the 4-cell chain (cell0=a, cell1=b, cell2=y_oe/control, cell3=y/output3) so
    that after Update-DR the control cell's po=1 (driver enabled) and the output3
    cell's po=1. Note `y` genuinely floats to "z" for the cycles between EXTEST
    becoming selected and this Update-DR committing po=1 — the control cell's
    reset value (safe_value=0) keeps the driver disabled by default, so there's a
    real window where nothing drives the pin yet. That's expected, not a bug (see
    _parse_bit/pin_drive_value); `y` settles to a concrete 1 only afterward."""
    s: list[tuple[int, int, int, int, int, int]] = []

    def tick(tms, tdi=0, trst_n=1, rst_n=1, a=0, b=0):
        s.append((tms, tdi, trst_n, rst_n, a, b))

    # Reset both the TAP and the functional side, then settle in Run-Test-Idle.
    tick(0, trst_n=0, rst_n=0)
    tick(0, trst_n=0, rst_n=0)
    tick(0, trst_n=1, rst_n=1)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE
    tick(0, trst_n=1, rst_n=1)  # settle

    # Navigate to Shift-IR and select EXTEST (opcode 0000 -- all bits 0, but the
    # navigation/timing is exercised regardless of the opcode's literal value).
    tick(1)  # RUN_TEST_IDLE -> SELECT_DR_SCAN
    tick(1)  # -> SELECT_IR_SCAN
    tick(0)  # -> CAPTURE_IR
    tick(0)  # -> SHIFT_IR (this tick's old_state=CAPTURE_IR loads the pattern)
    for i in range(IR_WIDTH - 1):
        tick(0, tdi=(OPCODE_EXTEST >> i) & 1)  # remain in SHIFT_IR
    tick(1, tdi=(OPCODE_EXTEST >> (IR_WIDTH - 1)) & 1)  # last bit + exit -> EXIT1_IR
    tick(1)  # EXIT1_IR -> UPDATE_IR (arrival, no decode yet)
    tick(0)  # UPDATE_IR -> RUN_TEST_IDLE (decode fires while departing)

    # Navigate to Shift-DR and shift a 4-bit pattern through the chain:
    # fed order tdi_1..tdi_4 ends up, after 4 shifts, as cell0=tdi_4, cell1=tdi_3,
    # cell2=tdi_2, cell3=tdi_1 (same "last fed lands at position 0" pattern as the
    # IR case). Feed 1,1,0,0 -> cell2(control)=1, cell3(output3)=1.
    tick(1)  # RUN_TEST_IDLE -> SELECT_DR_SCAN
    tick(0)  # -> CAPTURE_DR
    tick(0)  # -> SHIFT_DR (this tick's old_state=CAPTURE_DR captures live values)
    pattern = [1, 1, 0]  # tdi_1, tdi_2, tdi_3 (tms=0, stay in SHIFT_DR)
    for bit in pattern:
        tick(0, tdi=bit)
    tick(1, tdi=0)  # tdi_4 + exit -> EXIT1_DR
    tick(1)  # EXIT1_DR -> UPDATE_DR (arrival)
    tick(0)  # UPDATE_DR -> RUN_TEST_IDLE (update fires while departing)

    # A few settled idle cycles to observe the committed `y` value.
    for _ in range(3):
        tick(0)

    return s


def run_on_python(plan, stimulus) -> list[tuple]:
    model = TapModel(has_idcode=True)
    bsr = BoundaryScanRegister(plan)
    model.register_data_register(Instruction.EXTEST, bsr)
    functional = _TrivialFunctionalModel()

    trace = []
    for tms, tdi, trst_n, rst_n, a, b in stimulus:
        bsr.set_functional_inputs({"a": a, "b": b, "y": functional.current_y()})
        if not trst_n:
            model.reset()
            bsr.reset()  # rtl/bc1_full.v's po/shift_ff reset via trst_n too
            pre_state, pre_instr = TapState.TEST_LOGIC_RESET, model.instruction_opcode()
            pre_instruction = model.instruction
            capture_dr = shift_dr = update_dr = 0
            tdo = 0
            extest_active = pre_instruction is Instruction.EXTEST
            y = bsr.pin_drive_value("y", extest_mode=extest_active)
        else:
            pre_state = model.state
            pre_instr = model.instruction_opcode()
            pre_instruction = model.instruction
            capture_dr = int(pre_state is TapState.CAPTURE_DR)
            shift_dr = int(pre_state is TapState.SHIFT_DR)
            update_dr = int(pre_state is TapState.UPDATE_DR)
            # Sampled BEFORE tick() -- combinational from *current* (pre-edge)
            # register values, same as the RTL side: tick() mutates the registered
            # bsr DataRegister as a side effect (capture/shift/update all fire
            # inside it), so reading pin_drive_value() after tick() would reflect
            # this cycle's own edge instead of the state that was live *during* it.
            extest_active = pre_instruction is Instruction.EXTEST
            y = bsr.pin_drive_value("y", extest_mode=extest_active)
            tdo = model.tick(tms, tdi)

        trace.append((pre_state.value, pre_instr, capture_dr, shift_dr, update_dr, tdo, y))
        functional.clock(rst_n, a, b)

    return trace


def _parse_bit(token: str) -> int | str:
    """`y`'s $display("%0d", y) prints the literal character 'z' (or 'x') instead
    of a decimal digit when the net is tri-stated/undefined — genuinely expected
    here: `y` floats to "z" whenever EXTEST is selected but its control cell's
    committed enable (`po`) hasn't been set yet (still disabled from reset, per
    safe_value=0's whole purpose), not a parsing edge case to work around."""
    return token if token in ("z", "x") else int(token)


def render_stimulus_file(stimulus) -> str:
    return "\n".join(f"{t} {d} {r} {s} {a} {b}" for t, d, r, s, a, b in stimulus) + "\n"


def build_plan_and_insert(fixtures_dir, yosys_command):
    """Builds the BsrPlan once from the real ingested netlist and inserts it,
    returning (netlist, plan) — the SAME plan object is later handed to the Python
    model too, so there is exactly one plan-construction path, not two that could
    silently diverge."""
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    plan = build_bsr_plan(
        netlist.module("trivial"),
        clock_ports=frozenset({"clk"}),
        excluded_ports=frozenset({"rst_n"}),
    )
    insert_bsr(netlist, "trivial", plan, yosys_command=yosys_command)
    return netlist, plan


def run_on_rtl(netlist, yosys_command, iverilog_command, vvp_command, fixtures_dir, stimulus) -> list[tuple]:
    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)

    with tempfile.TemporaryDirectory(prefix="warptap-cross-sim-") as tmpdir:
        inserted_path = Path(tmpdir) / "trivial_inserted.v"
        inserted_path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [inserted_path, fixtures_dir / "tb_bsr_trivial.v"],
            extra_inputs={"stimulus.txt": render_stimulus_file(stimulus)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    trace = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, state, instr, cap, shift, upd, tdo, y = line.split(",")
        trace.append((int(state), int(instr), int(cap), int(shift), int(upd), int(tdo), _parse_bit(y)))
    return trace


def test_extest_shift_through_real_chain_matches_python_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    stimulus = _directed_stimulus()

    netlist, plan = build_plan_and_insert(fixtures_dir, yosys_command)
    python_trace = run_on_python(plan, stimulus)
    rtl_trace = run_on_rtl(
        netlist, yosys_command, iverilog_command, vvp_command, fixtures_dir, stimulus
    )

    assert rtl_trace == python_trace
    # Not vacuous: confirm the shifted-in pattern actually settled `y` to the
    # expected driven value (1, chosen so the control cell stays enabled -- see
    # _directed_stimulus's docstring), not e.g. two implementations agreeing on an
    # all-zero trace.
    assert rtl_trace[-1][6] == 1
