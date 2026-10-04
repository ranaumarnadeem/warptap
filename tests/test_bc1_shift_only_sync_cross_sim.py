"""RTL/Python cross-simulation for rtl/bc1_shift_only_sync.v, the READ cell with a
two-TCK-flop synchronizer before capture -- standalone, no TAP and no network, in
tests/test_sib_cell_cross_sim.py's shape. A capture shows the pin as it was two TCK edges
earlier; the plain bc1_shift_only shows it as it is."""

from __future__ import annotations

import random
from pathlib import Path

import warptap
from warptap.sim_io import run_verilog_testbench

_RTL_DIR = Path(warptap.__file__).resolve().parent / "rtl"
_COLUMNS = ("trst_n", "pi", "si", "capture_dr", "shift_dr")


class _SyncCellRef:
    """rtl/bc1_shift_only_sync.v's two always-blocks: the synchronizer, then the
    capture/shift flop reading the synchronizer's old (pre-edge) output."""

    def __init__(self) -> None:
        self.sync_first = 0
        self.sync_second = 0
        self.shift_ff = 0

    def tick(self, *, trst_n: int, pi: int, si: int, capture_dr: int, shift_dr: int) -> int:
        if not trst_n:  # async reset, before this cycle's output is sampled
            self.sync_first = self.sync_second = self.shift_ff = 0
        so = self.shift_ff
        if trst_n:
            shift_ff = self.shift_ff
            if capture_dr:
                shift_ff = self.sync_second
            elif shift_dr:
                shift_ff = si
            self.sync_first, self.sync_second, self.shift_ff = pi, self.sync_first, shift_ff
        return so


def _render(rows: list[tuple[int, ...]]) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def _run_python(rows: list[tuple[int, ...]]) -> list[int]:
    ref = _SyncCellRef()
    return [ref.tick(**dict(zip(_COLUMNS, row))) for row in rows]


def _run_rtl(fixtures_dir: Path, iverilog_command, vvp_command, rows) -> list[int]:
    stdout = run_verilog_testbench(
        [_RTL_DIR / "bc1_shift_only_sync.v", fixtures_dir / "tb_bc1_shift_only_sync.v"],
        extra_inputs={"stimulus.txt": _render(rows)},
        iverilog_command=iverilog_command,
        vvp_command=vvp_command,
    )
    return [int(line.split(",")[1]) for line in stdout.splitlines() if line.startswith("TRACE,")]


def _row(trst_n=1, pi=0, si=0, capture_dr=0, shift_dr=0) -> tuple[int, ...]:
    return (trst_n, pi, si, capture_dr, shift_dr)


def _captured_after(edges: int) -> list[tuple[int, ...]]:
    """pi rises, `edges` TCK edges pass, then a capture; the captured bit then shows on so."""
    rows = [_row(trst_n=0), _row(trst_n=0), _row(), _row(), _row()]
    rows += [_row(pi=1)] * edges
    rows += [_row(pi=1, capture_dr=1), _row(pi=1)]
    return rows


def test_a_capture_shows_the_pin_two_edges_late(fixtures_dir, iverilog_command, vvp_command):
    for edges, expected in ((0, 0), (1, 0), (2, 1), (3, 1)):
        rows = _captured_after(edges)
        rtl = _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows)
        assert rtl == _run_python(rows), edges
        assert rtl[-1] == expected, f"{edges} edges between the pin and the capture"


def test_random_stimulus_matches_the_reference(fixtures_dir, iverilog_command, vvp_command):
    rng = random.Random(20261001)
    rows = [_row(trst_n=0), _row(trst_n=0)]
    for _ in range(400):
        capture = rng.random() < 0.2
        rows.append(
            _row(
                trst_n=0 if rng.random() < 0.02 else 1,
                pi=rng.randint(0, 1),
                si=rng.randint(0, 1),
                capture_dr=int(capture),
                shift_dr=int(not capture and rng.random() < 0.5),
            )
        )
    assert _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows) == _run_python(rows)
