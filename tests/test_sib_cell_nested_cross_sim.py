"""RTL/Python cross-simulation spike for nested (SIB-gates-SIB) scan connectivity -- Phase 0
of the nested-SIB implementation plan. Proves, BEFORE any package source changes, that two
rtl/sib_cell.v instances wired outer.nested_si -> inner.si, outer.nested_select ->
inner.select, inner.so -> outer.nested_so behave correctly for real hierarchical IEEE 1687
scan-splice semantics, and specifically that a single Shift-DR/Update-DR round cannot open
more than one previously-closed level of nesting at once: a closed SIB's nested content isn't
physically part of the live scan chain yet, so there's nothing to address a bit position
within. This gates every later phase -- if this file didn't pass, the whole plan's RTL wiring
hypothesis would need rethinking before any package source changes were worth making.

Mirrors tests/test_sib_cell_cross_sim.py's exact standalone, hand-wired pattern -- no
sib_insert.py/sib_plan.py involvement, testing the RTL primitive's own composability
directly. ``_SibCellRef`` is duplicated from that file rather than imported (no existing
precedent in this suite for cross-test-file imports; two occurrences is this project's own
"rule of three" case for not sharing yet).
"""

from __future__ import annotations

import random
from pathlib import Path

import warptap
from warptap.sim_io import run_verilog_testbench

_RTL_DIR = Path(warptap.__file__).resolve().parent / "rtl"
_COLUMNS = (
    "trst_n", "outer_select", "outer_si", "inner_nested_so",
    "capture_dr", "shift_dr", "update_dr",
)


class _SibCellRef:
    """Minimal single-cell Python reference, duplicated from test_sib_cell_cross_sim.py's own
    class of the same name (see that file for how a bug in this class's `nested_select`
    computation -- `po & select`, missing a real `shift_ff` term -- was found and fixed while
    building this one: wiring two real cells together here makes a wrong `nested_select`
    directly observable in a second cell's behavior, unlike that file's own tests, where it's
    only ever traced, never fed back into anything)."""

    def __init__(self) -> None:
        self.shift_ff = 0
        self.po = 0

    def tick(
        self, *, trst_n: int, select: int, si: int, nested_so: int,
        capture_dr: int, shift_dr: int, update_dr: int,
    ) -> tuple[int, int, int]:
        if not trst_n:
            self.shift_ff = 0
            self.po = 0

        so = self.shift_ff
        nested_si = si
        nested_select = self.po & self.shift_ff & select

        if trst_n and select:
            old_shift_ff, old_po = self.shift_ff, self.po
            new_shift_ff = old_shift_ff
            if capture_dr:
                new_shift_ff = old_po
            elif shift_dr:
                new_shift_ff = nested_so if old_po else si
            new_po = old_shift_ff if update_dr else old_po
            self.shift_ff, self.po = new_shift_ff, new_po

        return so, nested_si, nested_select


class _NestedSibRef:
    """Two chained _SibCellRef instances, wired exactly as tb_sib_cell_nested.v wires two
    real sib_cell instances: outer.nested_si -> inner.si, outer.nested_select ->
    inner.select, inner.so -> outer.nested_so. ``inner_nested_so`` is a free stimulus input,
    standing in for whatever leaf content (an instrument, or a further-nested SIB) would
    occupy that position in a real network -- this spike only tests the SIB-to-SIB splice
    itself, not any particular leaf.

    Call order (outer then inner) is not arbitrary but doesn't need to be: outer's returned
    ``(so, nested_si, nested_select)`` never depends on outer's OWN ``nested_so`` argument
    (only outer's *next* state does, computed from OLD state -- see _SibCellRef.tick's own
    non-blocking-assignment shape), so outer can be ticked first using inner's current
    (pre-edge, directly-readable) ``shift_ff`` as its nested_so input, then inner ticked using
    outer's freshly-computed nested_si/nested_select as its own same-cycle si/select --
    matching real same-edge combinational wiring between two flip-flops."""

    def __init__(self) -> None:
        self.outer = _SibCellRef()
        self.inner = _SibCellRef()

    def tick(
        self, *, trst_n: int, outer_select: int, outer_si: int, inner_nested_so: int,
        capture_dr: int, shift_dr: int, update_dr: int,
    ) -> tuple[int, int]:
        inner_so_now = 0 if not trst_n else self.inner.shift_ff
        outer_so, nested_si, nested_select = self.outer.tick(
            trst_n=trst_n, select=outer_select, si=outer_si, nested_so=inner_so_now,
            capture_dr=capture_dr, shift_dr=shift_dr, update_dr=update_dr,
        )
        inner_so, _inner_nested_si, _inner_nested_select = self.inner.tick(
            trst_n=trst_n, select=nested_select, si=nested_si, nested_so=inner_nested_so,
            capture_dr=capture_dr, shift_dr=shift_dr, update_dr=update_dr,
        )
        return outer_so, inner_so


def _render_stimulus(rows: list[tuple[int, ...]]) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def _run_python(rows: list[tuple[int, ...]]) -> list[tuple[int, int]]:
    ref = _NestedSibRef()
    return [ref.tick(**dict(zip(_COLUMNS, row))) for row in rows]


def _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows) -> list[tuple[int, int]]:
    stdout = run_verilog_testbench(
        [_RTL_DIR / "sib_cell.v", fixtures_dir / "tb_sib_cell_nested.v"],
        extra_inputs={"stimulus.txt": _render_stimulus(rows)},
        iverilog_command=iverilog_command,
        vvp_command=vvp_command,
    )
    trace = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, outer_so, inner_so = line.split(",")
        trace.append((int(outer_so), int(inner_so)))
    return trace


def test_random_stimulus_matches_between_rtl_and_python(fixtures_dir, iverilog_command, vvp_command):
    """The primary proof: the Python reference model's wiring hypothesis matches real RTL
    simulation bit-for-bit across many random cycles -- confirms the outer/inner wiring
    (nested_si/nested_select/nested_so chained between two real sib_cell instances) is
    correct in general, not just for one hand-picked sequence."""
    rng = random.Random(1687)
    rows: list[tuple[int, ...]] = [(0, 1, 0, 0, 0, 0, 0)] * 2  # reset lead-in
    for _ in range(120):
        rows.append(tuple(1 if i == 0 else rng.randint(0, 1) for i in range(7)))  # trst_n held 1

    python_trace = _run_python(rows)
    rtl_trace = _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows)
    assert rtl_trace == python_trace


def test_opening_both_levels_needs_two_separate_rounds(fixtures_dir, iverilog_command, vvp_command):
    """The specific finding this spike exists to confirm: a closed SIB's nested content isn't
    physically part of the live scan chain, so a single Capture/Shift/Update round can only
    open ONE previously-closed level. Round 1 below is a complete, ordinary capture/shift/
    update pass attempting to open the network -- it opens outer only. Round 2, a second
    complete pass, is what actually reaches inner, now that outer already commits open."""
    rows: list[tuple[int, ...]] = [(0, 1, 0, 0, 0, 0, 0)] * 2  # reset lead-in

    def row(trst_n=1, outer_select=1, outer_si=0, inner_nested_so=0,
            capture_dr=0, shift_dr=0, update_dr=0):
        rows.append(
            (trst_n, outer_select, outer_si, inner_nested_so, capture_dr, shift_dr, update_dr)
        )

    # Round 1: one full capture/shift/update pass, one shift_dr cycle -- with everything
    # closed, the live scan path is exactly 1 bit long (outer's own closed-bypass bit; inner
    # doesn't physically exist in the chain yet). Exactly the shape of one PDL iApply phase
    # (or one sib_overshift retargeting round) opening a single top-level SIB.
    row(capture_dr=1)
    row(shift_dr=1, outer_si=1)
    row(update_dr=1)

    # Round 2: now that outer is open, the live scan path is 2 bits long -- inner's own
    # (still closed) bit, then outer's own select bit, TDI-nearest-first -- so re-opening
    # outer AND opening inner both need to be fed in this one pass. Two shift_dr cycles.
    row(capture_dr=1)
    row(shift_dr=1, outer_si=1)
    row(shift_dr=1, outer_si=1)
    row(update_dr=1)

    python_trace = _run_python(rows)
    rtl_trace = _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows)
    assert rtl_trace == python_trace  # cross-checked bit-for-bit, though the real assertions
                                       # this test exists for are the reference model's own
                                       # internal state at the round boundary, below.

    ref = _NestedSibRef()
    for row_vals in rows[:5]:  # reset (2 rows) + round 1 (3 rows)
        ref.tick(**dict(zip(_COLUMNS, row_vals)))
    assert ref.outer.po == 1  # round 1 alone DID open outer
    assert ref.inner.po == 0  # ...but did NOT reach inner -- the core finding

    for row_vals in rows[5:]:  # round 2 (4 rows)
        ref.tick(**dict(zip(_COLUMNS, row_vals)))
    assert ref.outer.po == 1  # outer stays open
    assert ref.inner.po == 1  # round 2 (now that outer is open) DOES open inner
