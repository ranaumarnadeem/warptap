"""Functional-equivalence proof (implementation_plan.md §7 Stage 3, testing strategy item 4):
a specific, provable claim, not a hand-wave. Holding `trst_n=0` for a whole simulation forces
`current_instruction` to its fixed reset default (rtl/tap_core.v), which forces
`extest_mode=0` at every cycle regardless of tck/tms/tdi activity, which forces
`pin_out=func_in` unconditionally on every inserted cell (rtl/bc1_full.v) — i.e. with `trst_n`
held asserted, the inserted design must be cycle-for-cycle identical to the pre-insertion
design for the same clk/rst_n/a/b stimulus. This test drives the same stimulus through both
and asserts `y` matches every cycle.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from warptap.bsr_insert import insert_bsr
from warptap.bsr_plan import build_bsr_plan
from warptap.netlist import Netlist
from warptap.sim_io import run_verilog_testbench
from warptap.yosys_io import ingest, write_verilog_from_json

_RST_N, _A, _B = 0, 1, 2  # tuple indices into the functional stimulus


def _functional_stimulus(seed: int, num_cycles: int) -> list[tuple[int, int, int]]:
    rng = random.Random(seed)
    stim = [(0, 0, 0)]  # start with rst_n asserted for one cycle
    for _ in range(num_cycles - 1):
        stim.append((1, rng.randint(0, 1), rng.randint(0, 1)))
    return stim


def _run_original(fixtures_dir, iverilog_command, vvp_command, stim) -> list[int]:
    lines = "\n".join(f"{rst_n} {a} {b}" for rst_n, a, b in stim) + "\n"
    stdout = run_verilog_testbench(
        [fixtures_dir / "trivial.v", fixtures_dir / "tb_trivial_functional.v"],
        extra_inputs={"stimulus.txt": lines},
        iverilog_command=iverilog_command,
        vvp_command=vvp_command,
    )
    return [int(line.split(",")[1]) for line in stdout.splitlines() if line.startswith("TRACE,")]


def _run_inserted(
    fixtures_dir, yosys_command, iverilog_command, vvp_command, stim
) -> list[int]:
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    plan = build_bsr_plan(
        netlist.module("trivial"),
        clock_ports=frozenset({"clk"}),
        excluded_ports=frozenset({"rst_n"}),
    )
    insert_bsr(netlist, "trivial", plan, yosys_command=yosys_command)
    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)

    lines = []
    for rst_n, a, b in stim:
        # trst_n=0 (asserted) for the whole run; tms/tdi alternate garbage -- the whole
        # point is proving they don't matter while trst_n is asserted.
        tms, tdi = a, b  # arbitrary, deliberately correlated with other signals, not zero
        lines.append(f"{tms} {tdi} 0 {rst_n} {a} {b}")
    stimulus_text = "\n".join(lines) + "\n"

    with tempfile.TemporaryDirectory(prefix="warptap-inserted-verilog-") as tmpdir:
        inserted_path = Path(tmpdir) / "trivial_inserted.v"
        inserted_path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [inserted_path, fixtures_dir / "tb_bsr_trivial.v"],
            extra_inputs={"stimulus.txt": stimulus_text},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
    # TRACE,tap_state,current_instruction,capture_dr,shift_dr,update_dr,tdo,y -- y is index 7.
    return [int(line.split(",")[7]) for line in stdout.splitlines() if line.startswith("TRACE,")]


def test_inserted_design_matches_original_with_trst_n_held_low(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    stim = _functional_stimulus(seed=1, num_cycles=40)

    original_y = _run_original(fixtures_dir, iverilog_command, vvp_command, stim)
    inserted_y = _run_inserted(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, stim
    )

    assert len(original_y) == len(stim)
    assert inserted_y == original_y
