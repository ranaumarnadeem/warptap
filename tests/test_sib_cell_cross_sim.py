"""RTL/Python cross-simulation for the standalone SIB primitive (implementation_plan.md §7
Stage 4, testing layer 2). Mirrors tests/test_bsr_insert_cross_sim.py's shape, but against
rtl/sib_cell.v directly -- no network, no instrument -- proving the primitive's self-capture,
scan-splice mux (both directions), and select-gating behavior in isolation *before* network
insertion (sib_insert.py) is built on top of it, per this stage's own sequencing note.
"""

from __future__ import annotations

import random
from pathlib import Path

import warptap
from warptap.sim_io import run_verilog_testbench

_RTL_DIR = Path(warptap.__file__).resolve().parent / "rtl"
_COLUMNS = ("trst_n", "select", "si", "nested_so", "capture_dr", "shift_dr", "update_dr")


class _SibCellRef:
    """Minimal single-cell Python reference, independent of how sib_insert.py will later
    wire `select` -- this only models rtl/sib_cell.v's own always-block/assign shape."""

    def __init__(self) -> None:
        self.shift_ff = 0
        self.po = 0

    def tick(
        self, *, trst_n: int, select: int, si: int, nested_so: int,
        capture_dr: int, shift_dr: int, update_dr: int,
    ) -> tuple[int, int, int]:
        # Async reset takes effect immediately, before this cycle's combinational
        # outputs are sampled -- matches `always @(posedge tck or negedge trst_n)`:
        # trst_n dropping fires the block right away, well before the next tck edge.
        if not trst_n:
            self.shift_ff = 0
            self.po = 0

        so = self.shift_ff
        nested_si = si
        nested_select = self.po & select

        if trst_n and select:
            # rtl/sib_cell.v uses non-blocking (`<=`) assignment for both shift_ff
            # and po, in two SEPARATE (not else-if-chained) `if` statements -- a
            # real tap_core.v never asserts shift_dr and update_dr in the same
            # cycle (they're one-hot per FSM state), but this standalone primitive
            # must still be correct if a caller ever did, so both new values are
            # computed from OLD (pre-edge) state and applied together, exactly
            # mirroring Verilog's non-blocking simultaneity rather than a
            # blocking, line-by-line Python read-after-write.
            old_shift_ff, old_po = self.shift_ff, self.po
            new_shift_ff = old_shift_ff
            if capture_dr:
                new_shift_ff = old_po
            elif shift_dr:
                new_shift_ff = nested_so if old_po else si
            new_po = old_shift_ff if update_dr else old_po
            self.shift_ff, self.po = new_shift_ff, new_po

        return so, nested_si, nested_select


def _render_stimulus(rows: list[tuple[int, ...]]) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def _run_python(rows: list[tuple[int, ...]]) -> list[tuple[int, int, int]]:
    ref = _SibCellRef()
    trace = []
    for row in rows:
        kwargs = dict(zip(_COLUMNS, row))
        trace.append(ref.tick(**kwargs))
    return trace


def _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows) -> list[tuple[int, int, int]]:
    stdout = run_verilog_testbench(
        [_RTL_DIR / "sib_cell.v", fixtures_dir / "tb_sib_cell.v"],
        extra_inputs={"stimulus.txt": _render_stimulus(rows)},
        iverilog_command=iverilog_command,
        vvp_command=vvp_command,
    )
    trace = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, so, nested_si, nested_select = line.split(",")
        trace.append((int(so), int(nested_si), int(nested_select)))
    return trace


def _directed_rows() -> list[tuple[int, ...]]:
    rows: list[tuple[int, ...]] = []

    def row(trst_n=1, select=1, si=0, nested_so=0, capture_dr=0, shift_dr=0, update_dr=0):
        rows.append((trst_n, select, si, nested_so, capture_dr, shift_dr, update_dr))

    # Reset lead-in.
    row(trst_n=0)
    row(trst_n=0)

    # Closed (po=0): shift_dr with si=1 must bypass straight through (scan-splice,
    # closed direction) -- one shift lands si=1 in shift_ff.
    row(shift_dr=1, si=1)
    row()  # settle -- so now reflects the shifted-in 1

    # Open the SIB: capture (loads po=0 into shift_ff, self-capture of the CLOSED
    # state), then shift a 1 in, then update_dr commits po=1.
    row(capture_dr=1)
    row()  # so now reflects the captured po (0)
    row(shift_dr=1, si=1)
    row()  # so now reflects the shifted-in 1
    row(update_dr=1)
    row()  # po is now 1 (open)

    # Open (po=1): shift_dr must route through nested_so, NOT si (scan-splice, open
    # direction) -- drive si=0, nested_so=1, and shift_ff must pick up nested_so.
    row(shift_dr=1, si=0, nested_so=1)
    row()  # so now reflects nested_so (1), proving si was ignored while open

    # Self-capture: capture_dr while po=1 must load shift_ff with po itself (1).
    row(capture_dr=1)
    row()  # so now reflects the captured po (1)

    # select=0 freezes all state: try every strobe with select deasserted and prove
    # nothing changes (shift_ff stays at whatever it settled to above).
    row(select=0, capture_dr=1, shift_dr=1, update_dr=1, si=1, nested_so=0)
    row(select=0, capture_dr=1, shift_dr=1, update_dr=1, si=1, nested_so=0)

    return rows


def test_directed_scenario_matches_between_rtl_and_python(
    fixtures_dir, iverilog_command, vvp_command
):
    rows = _directed_rows()
    python_trace = _run_python(rows)
    rtl_trace = _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows)
    assert rtl_trace == python_trace

    # Not vacuous: pin down the specific settled values the directed sequence
    # above was built to demonstrate (indices line up with _directed_rows()).
    assert rtl_trace[3][0] == 1   # closed-bypass shift landed si=1 in shift_ff -> so=1
    assert rtl_trace[5][0] == 0   # self-capture while closed loaded po=0
    assert rtl_trace[7][0] == 1   # shifted a 1 in before opening
    assert rtl_trace[9][0] == 1   # update_dr committed po=1 (open)
    assert rtl_trace[11][0] == 1  # open-direction shift routed nested_so=1, not si=0
    assert rtl_trace[13][0] == 1  # self-capture while open loaded po=1
    # select=0 rows: shift_ff must be frozen at whatever it was (1 from the self-capture).
    assert rtl_trace[14][0] == 1
    assert rtl_trace[15][0] == 1


def test_select_zero_freezes_state_across_every_strobe_combination(
    fixtures_dir, iverilog_command, vvp_command
):
    """A dedicated, broader proof of select-gating: every one of the 8 capture/shift/
    update strobe combinations, held for two cycles each with select=0 throughout
    (after opening the SIB so po=1, the more interesting frozen state), must never
    change `so`."""
    rows: list[tuple[int, ...]] = [(0, 1, 0, 0, 0, 0, 0)] * 2  # reset lead-in
    # Open the SIB first (select=1) so there's real non-zero state to freeze.
    rows.append((1, 1, 1, 0, 0, 1, 0))  # shift_dr, si=1
    rows.append((1, 1, 0, 0, 0, 0, 1))  # update_dr commits po=1
    for capture_dr in (0, 1):
        for shift_dr in (0, 1):
            for update_dr in (0, 1):
                rows.append((1, 0, 1, 1, capture_dr, shift_dr, update_dr))
                rows.append((1, 0, 1, 1, capture_dr, shift_dr, update_dr))

    python_trace = _run_python(rows)
    rtl_trace = _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows)
    assert rtl_trace == python_trace

    so_values_while_frozen = [t[0] for t in rtl_trace[4:]]
    assert len(set(so_values_while_frozen)) == 1  # never changes across any combination


def test_random_stimulus_matches_between_rtl_and_python(fixtures_dir, iverilog_command, vvp_command):
    rng = random.Random(42)
    rows: list[tuple[int, ...]] = [(0, 1, 0, 0, 0, 0, 0)] * 2  # reset lead-in
    for _ in range(60):
        rows.append(tuple(1 if i == 0 else rng.randint(0, 1) for i in range(7)))  # trst_n held 1

    python_trace = _run_python(rows)
    rtl_trace = _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows)
    assert rtl_trace == python_trace
