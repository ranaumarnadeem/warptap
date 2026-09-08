"""RTL/Python cross-simulation spike for the N-arm ScanMux primitive -- Phase 0 of the
multi-arm ScanMux implementation plan. Proves, BEFORE any package source changes, that
rtl/scan_mux_cell.v's select-field shift register, decode, and relay logic behave correctly
for a real N>2-arm, multi-bit-select case -- and specifically that switching DIRECTLY from one
already-matched arm to a DIFFERENT arm commits correctly in a single round, a case with no
nested-SIB precedent (nested SIBs only ever have binary open/closed transitions). This gates
every later phase of the plan -- if this file didn't pass, the RTL wiring hypothesis (fresh
select-field bits reach `shift_ff` by relaying through whichever arm is currently matched, the
same 1-cycle-delayed mechanism sib_cell.v's own `nested_so` relies on) would need rethinking
before any package source changes were worth making.

Fixed at ARMS=3, SEL_WIDTH=2 (arm0=1, arm1=2, arm2=3; value 0 is bypass), matching
tb_scan_mux_cell.v exactly.

Mirrors tests/test_sib_cell_nested_cross_sim.py's exact standalone, hand-wired pattern -- no
sib_insert.py/sib_plan.py involvement, testing the RTL primitive's own composability directly.
Since tb_scan_mux_cell.v leaves every `arm_so[k]` as a free stimulus input (no real nested RTL
in this spike, mirroring tb_sib_cell.v's own `nested_so`), exercising the relay mechanism at
all requires `arm_so` to actually carry a one-cycle-delayed copy of `si` -- exactly what a
trivial always-shifting leaf register would produce -- so ``_TrivialLeafRef`` supplies that,
computed once and shared as the stimulus for every arm (mirrors tb_sib_cell_nested.v's own use
of a second real sib_cell instance for the same reason, just built as a Python stand-in here
rather than a second RTL module, since this spike's whole point is testing scan_mux_cell.v in
isolation, not composed with anything else yet).
"""

from __future__ import annotations

import random
from pathlib import Path

import warptap
from warptap.sim_io import run_verilog_testbench

_RTL_DIR = Path(warptap.__file__).resolve().parent / "rtl"
_ARMS = 3
_SEL_WIDTH = 2
_ARM_VALUES = (1, 2, 3)  # arm0, arm1, arm2 -- matches tb_scan_mux_cell.v's ARM_VALUES exactly
_COLUMNS = ("trst_n", "select", "si", "arm_so", "capture_dr", "shift_dr", "update_dr")


class _TrivialLeafRef:
    """A minimal one-bit register that unconditionally shifts from `si` on shift_dr -- stands
    in for 'whatever occupies an arm', giving arm_so a real one-cycle-delayed relay of si to
    exercise scan_mux_cell's own relay mechanism (see this module's own docstring)."""

    def __init__(self) -> None:
        self.ff = 0

    def tick(self, *, trst_n: int, si: int, shift_dr: int) -> int:
        so = self.ff
        if not trst_n:
            self.ff = 0
        elif shift_dr:
            self.ff = si
        return so


class _ScanMuxCellRef:
    """Python reference model for rtl/scan_mux_cell.v, mirroring its combinational decode
    block and clocked shift/capture/update block statement-for-statement."""

    def __init__(self) -> None:
        self.shift_ff = 0
        self.po = 0

    def tick(
        self, *, trst_n: int, select: int, si: int, arm_so: tuple[int, ...],
        capture_dr: int, shift_dr: int, update_dr: int,
    ) -> tuple[int, int, int]:
        if not trst_n:
            self.shift_ff = 0
            self.po = 0

        matched = next((k for k in range(_ARMS) if self.po == _ARM_VALUES[k]), None)
        relay_so = arm_so[matched] if matched is not None else 0

        so = relay_so if matched is not None else (self.shift_ff & 1)
        arm_active = 0
        arm_select = 0
        if matched is not None and select:
            arm_active |= 1 << matched
            if self.shift_ff == _ARM_VALUES[matched]:
                arm_select |= 1 << matched

        if trst_n and select:
            old_shift_ff, old_po = self.shift_ff, self.po
            new_shift_ff = old_shift_ff
            if capture_dr:
                new_shift_ff = old_po
            elif shift_dr:
                fresh_bit = relay_so if matched is not None else si
                new_shift_ff = (old_shift_ff >> 1) | (fresh_bit << (_SEL_WIDTH - 1))
            new_po = old_shift_ff if update_dr else old_po
            self.shift_ff, self.po = new_shift_ff, new_po

        return so, arm_active, arm_select


def _with_arm_so(rows6: list[tuple[int, ...]]) -> list[tuple[int, ...]]:
    """Expands (trst_n, select, si, capture_dr, shift_dr, update_dr) 6-tuples into the full
    7-column stimulus by running ``_TrivialLeafRef`` alongside and packing its `so` into all
    three ``arm_so`` bits (they're identical since every arm shares the same si fan-out)."""
    leaf = _TrivialLeafRef()
    rows7 = []
    for trst_n, select, si, capture_dr, shift_dr, update_dr in rows6:
        leaf_so = leaf.tick(trst_n=trst_n, si=si, shift_dr=shift_dr)
        arm_so_packed = leaf_so * 0b111
        rows7.append((trst_n, select, si, arm_so_packed, capture_dr, shift_dr, update_dr))
    return rows7


def _render_stimulus(rows: list[tuple[int, ...]]) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def _run_python(rows: list[tuple[int, ...]]) -> list[tuple[int, int, int]]:
    ref = _ScanMuxCellRef()
    trace = []
    for row in rows:
        kwargs = dict(zip(_COLUMNS, row))
        kwargs["arm_so"] = tuple((kwargs["arm_so"] >> k) & 1 for k in range(_ARMS))
        trace.append(ref.tick(**kwargs))
    return trace


def _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows) -> list[tuple[int, int, int]]:
    stdout = run_verilog_testbench(
        [_RTL_DIR / "scan_mux_cell.v", fixtures_dir / "tb_scan_mux_cell.v"],
        extra_inputs={"stimulus.txt": _render_stimulus(rows)},
        iverilog_command=iverilog_command,
        vvp_command=vvp_command,
    )
    trace = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, so, arm_active, arm_select = line.split(",")
        trace.append((int(so), int(arm_active), int(arm_select)))
    return trace


def test_random_stimulus_matches_between_rtl_and_python(fixtures_dir, iverilog_command, vvp_command):
    """The primary proof: the Python reference model's decode/relay/shift hypothesis matches
    real RTL simulation bit-for-bit across many random cycles."""
    rng = random.Random(1687)
    rows6: list[tuple[int, ...]] = [(0, 1, 0, 0, 0, 0)] * 2  # reset lead-in
    for _ in range(150):
        rows6.append(tuple(1 if i == 0 else rng.randint(0, 1) for i in range(6)))  # trst_n held 1

    rows = _with_arm_so(rows6)
    python_trace = _run_python(rows)
    rtl_trace = _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows)
    assert rtl_trace == python_trace


def test_switching_arms_directly_commits_new_arm_in_one_round(fixtures_dir, iverilog_command, vvp_command):
    """The specific finding this spike exists to confirm: once arm0 is already matched (open),
    a single Capture/Shift/Update round can retarget `po` DIRECTLY to a different arm (arm2)
    -- no intermediate bypass round needed -- because the fresh select-field bits relay through
    arm0's own (currently-matched) content on their way to `shift_ff`, exactly generalizing
    sib_cell.v's own nested_so relay. Values are fed LSB-first (bit 0 shifted in first), which
    is what a right-shift-in-at-the-MSB register requires to land the target value correctly
    by round's end -- the same convention rtl/tap_core.v's ir_shift already uses for real IR
    opcodes.

    Round 2 needs THREE shift cycles, not two: the two fresh select-field bits PLUS one dummy
    cycle for arm0's own (width-1, stood in for by the shared trivial leaf) content -- an
    arm-switch round still has to carry the currently-matched arm's own content width, even
    though that content is being abandoned, exactly mirroring how closing an already-open SIB
    always includes that SIB's own soon-to-be-abandoned content width in compose_bits' length
    arithmetic today. An earlier version of this test used only two cycles and got this wrong
    (a caught-by-this-spike-not-guessed finding, not an assumption): with only two cycles, the
    relay hadn't yet flushed arm0's stale pre-round-2 content out from in front of the fresh
    select bits, and `po` landed on 2, not the intended 3."""
    rows6: list[tuple[int, ...]] = [(0, 1, 0, 0, 0, 0)] * 2  # reset lead-in

    def add(trst_n=1, select=1, si=0, capture_dr=0, shift_dr=0, update_dr=0):
        rows6.append((trst_n, select, si, capture_dr, shift_dr, update_dr))

    # Round 1: bypass -> arm0 (value 1 = 0b01). LSB first: si=1, then si=0. Bypass carries no
    # content (mirrors a closed SIB contributing just its own select bit), so 2 cycles only.
    add(capture_dr=1)
    add(shift_dr=1, si=1)
    add(shift_dr=1, si=0)
    add(update_dr=1)
    add()  # settle: arm_active should now reflect arm0

    # Round 2: arm0 -> arm2 (value 3 = 0b11), DIRECTLY, arm0 still matched throughout. LSB
    # first: si=1, si=1, then 1 dummy content cycle for arm0's own abandoned width-1 content.
    add(capture_dr=1)
    add(shift_dr=1, si=1)
    add(shift_dr=1, si=1)
    add(shift_dr=1, si=0)  # dummy: arm0's own content, being abandoned this round
    add(update_dr=1)
    add()  # settle: arm_active should now reflect arm2

    rows = _with_arm_so(rows6)
    python_trace = _run_python(rows)
    rtl_trace = _run_rtl(fixtures_dir, iverilog_command, vvp_command, rows)
    assert rtl_trace == python_trace  # cross-checked bit-for-bit throughout

    ref = _ScanMuxCellRef()
    for row in rows[:7]:  # reset (2) + round 1 (4) + settle (1)
        kwargs = dict(zip(_COLUMNS, row))
        kwargs["arm_so"] = tuple((kwargs["arm_so"] >> k) & 1 for k in range(_ARMS))
        ref.tick(**kwargs)
    assert ref.po == 1  # round 1 committed arm0

    for row in rows[7:]:  # round 2 (5) + settle (1)
        kwargs = dict(zip(_COLUMNS, row))
        kwargs["arm_so"] = tuple((kwargs["arm_so"] >> k) & 1 for k in range(_ARMS))
        ref.tick(**kwargs)
    assert ref.po == 3  # round 2 switched DIRECTLY to arm2, no intermediate bypass round

    assert rtl_trace[6][1] == 0b001  # after round 1's settle row: arm_active = arm0 only
    assert rtl_trace[12][1] == 0b100  # after round 2's settle row: arm_active = arm2 only
