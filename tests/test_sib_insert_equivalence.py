"""Non-interference proof for SIB network insertion (implementation_plan.md §7 Stage 4,
testing layer 4). Two claims:

1. `y` (trivial's own functional output) matches the pre-insertion design bit-for-bit under
   ANY tms/tdi/trst_n stimulus, unconditionally -- not just "while trst_n is held asserted"
   the way Stage 3's BSR-chain claim needed, since sib_insert.py never touches any of
   trivial's own ports at all (bsr_insert.py's cells drive/observe real pins; sib_insert.py's
   never do).
2. Under BYPASS or IDCODE -- the only instructions whose data register is tap_core's own
   built-in one -- the inserted RTL's full tap_state/current_instruction/capture_dr/shift_dr/
   update_dr/tdo trace matches a plain `TapModel(has_idcode=True)` with no SIB register
   registered at all. This proves the SIB network wired onto external_dr_tdo/tck/trst_n
   doesn't perturb tap_core's own internal registers, using the same technique
   test_tap_fsm_cross_sim.py established in Stage 2.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_model import TapModel
from warptap.yosys_io import ingest, write_verilog_from_json

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]
IR_WIDTH = 4
OPCODE_IDCODE = 0b0001
BYPASS_OPCODE = 0b1111


def _build_inserted(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "trivial", graph, yosys_command=yosys_command)
    return netlist


def _run_baseline(fixtures_dir, iverilog_command, vvp_command, stim) -> list[int]:
    lines = "\n".join(f"{rst_n} {a} {b}" for rst_n, a, b in stim) + "\n"
    stdout = run_verilog_testbench(
        [fixtures_dir / "trivial.v", fixtures_dir / "tb_trivial_functional.v"],
        extra_inputs={"stimulus.txt": lines},
        iverilog_command=iverilog_command,
        vvp_command=vvp_command,
    )
    return [int(line.split(",")[1]) for line in stdout.splitlines() if line.startswith("TRACE,")]


def _run_inserted(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, rows):
    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-sib-eq-sim-") as tmpdir:
        path = Path(tmpdir) / "trivial_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_sib_trivial.v"],
            extra_inputs={"stimulus.txt": "\n".join(" ".join(str(v) for v in r) for r in rows) + "\n"},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
    trace = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, state, instr, cap, shift, upd, tdo, y = line.split(",")
        trace.append((int(state), int(instr), int(cap), int(shift), int(upd), int(tdo), int(y)))
    return trace


def test_functional_output_unaffected_by_arbitrary_tap_activity(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """Claim 1: fully random tms/tdi/trst_n (deliberately unconstrained -- may select
    EXTEST, may shift garbage through the SIB network, doesn't matter) must never move
    `y` away from what the unmodified design alone would produce."""
    rng = random.Random(7)
    stim = [(0, 0, 0)]
    for _ in range(50):
        stim.append((1, rng.randint(0, 1), rng.randint(0, 1)))
    baseline_y = _run_baseline(fixtures_dir, iverilog_command, vvp_command, stim)

    netlist = _build_inserted(fixtures_dir, yosys_command)
    rows = []
    for i, (rst_n, a, b) in enumerate(stim):
        tms, tdi = rng.randint(0, 1), rng.randint(0, 1)
        # Force a real trst_n=0 pulse on the very first row -- Icarus's registers
        # start undefined ('x'), and a randomly-chosen trst_n here could skip the
        # reset entirely, propagating 'x' through tap_state/current_instruction/tdo
        # for the rest of the run (same lesson test_bsr_insert_bidir.py's reset
        # lead-in already established). Every later row is free to fluctuate.
        trst_n = 0 if i == 0 else (1 if rst_n else rng.randint(0, 1))
        rows.append((tms, tdi, trst_n, rst_n, a, b))
    inserted_trace = _run_inserted(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, rows
    )

    assert [t[6] for t in inserted_trace] == baseline_y


def _navigate_and_select(opcode: int) -> list[tuple[int, int]]:
    """(tms, tdi) pairs to go RUN_TEST_IDLE -> ... -> UPDATE_IR -> RUN_TEST_IDLE,
    shifting `opcode` in, same navigation shape as every prior stage's directed tests."""
    seq = [(1, 0), (1, 0), (0, 0), (0, 0)]  # -> SELECT_DR_SCAN -> SELECT_IR_SCAN -> CAPTURE_IR -> SHIFT_IR
    for i in range(IR_WIDTH - 1):
        seq.append((0, (opcode >> i) & 1))
    seq.append((1, (opcode >> (IR_WIDTH - 1)) & 1))  # last bit + exit -> EXIT1_IR
    seq.append((1, 0))  # -> UPDATE_IR
    seq.append((0, 0))  # -> RUN_TEST_IDLE
    return seq


def _shift_dr(num_bits: int, tdi_bits: list[int]) -> list[tuple[int, int]]:
    seq = [(1, 0), (0, 0), (0, 0)]  # -> SELECT_DR_SCAN -> CAPTURE_DR -> SHIFT_DR
    for i in range(num_bits - 1):
        seq.append((0, tdi_bits[i]))
    seq.append((1, tdi_bits[num_bits - 1]))  # last bit + exit -> EXIT1_DR
    seq.append((1, 0))  # -> UPDATE_DR
    seq.append((0, 0))  # -> RUN_TEST_IDLE
    return seq


def _idcode_bypass_only_stimulus() -> list[tuple[int, int, int, int, int, int]]:
    tms_tdi: list[tuple[int, int]] = [(0, 0), (0, 0), (0, 0)]  # reset settle, RUN_TEST_IDLE

    tms_tdi += _navigate_and_select(OPCODE_IDCODE)
    tms_tdi += _shift_dr(32, [0] * 32)  # shift out the full IDCODE value
    tms_tdi += [(0, 0)] * 3

    tms_tdi += _navigate_and_select(BYPASS_OPCODE)
    tms_tdi += _shift_dr(5, [1, 0, 1, 1, 0])
    tms_tdi += [(0, 0)] * 3

    stim = [(0, 0, 0, 0, 0, 0)]  # trst_n=0 pulse
    for tms, tdi in tms_tdi:
        stim.append((tms, tdi, 1, 1, 0, 0))
    return stim


def _run_on_tap_model(stim) -> list[tuple[int, int, int, int, int, int]]:
    model = TapModel(has_idcode=True)
    trace = []
    for tms, tdi, trst_n, _rst_n, _a, _b in stim:
        if not trst_n:
            model.reset()
            pre_state, pre_instr = TapState.TEST_LOGIC_RESET, model.instruction_opcode()
            capture_dr = shift_dr = update_dr = 0
            tdo = 0
        else:
            pre_state = model.state
            pre_instr = model.instruction_opcode()
            capture_dr = int(pre_state is TapState.CAPTURE_DR)
            shift_dr = int(pre_state is TapState.SHIFT_DR)
            update_dr = int(pre_state is TapState.UPDATE_DR)
            tdo = model.tick(tms, tdi)
        trace.append((pre_state.value, pre_instr, capture_dr, shift_dr, update_dr, tdo))
    return trace


def test_idcode_and_bypass_traces_match_tap_model_unaffected_by_sib_network(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """Claim 2: with the SIB network wired onto external_dr_tdo, IDCODE/BYPASS -- the two
    instructions that never touch it -- must behave identically to a plain TapModel with
    no SIB register registered, proving tap_core's own FSM/IR/data-register logic is
    untouched by this insertion."""
    stim = _idcode_bypass_only_stimulus()
    netlist = _build_inserted(fixtures_dir, yosys_command)
    inserted_trace = _run_inserted(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, stim
    )
    model_trace = _run_on_tap_model(stim)

    rtl_side = [t[:6] for t in inserted_trace]
    assert rtl_side == model_trace
    # Not vacuous: confirm the IDCODE value actually shifted out as the real configured
    # constant, not e.g. two implementations agreeing on an all-zero trace. Identify the
    # 32 IDCODE shift-DR ticks by their own trace flags (shift_dr=1, instr=OPCODE_IDCODE)
    # rather than a hand-counted index, so this doesn't silently break if the navigation
    # sequence above ever changes shape.
    from warptap.tap_model import IDCODE_VALUE

    idcode_shift_rows = [t for t in inserted_trace if t[3] == 1 and t[1] == OPCODE_IDCODE]
    assert len(idcode_shift_rows) == 32
    idcode_bits = [t[5] for t in idcode_shift_rows]
    shifted_out = sum(bit << i for i, bit in enumerate(idcode_bits))
    assert shifted_out == IDCODE_VALUE
