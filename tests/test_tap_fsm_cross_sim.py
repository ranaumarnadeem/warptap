"""RTL/Python behavioral equivalence tests (implementation_plan.md §7 Stage 2): the actual
drift-prevention mechanism between rtl/tap_core.v and warptap.tap_model.TapModel. Both are
driven, cycle-for-cycle, with identical TMS/TDI stimulus, and their per-cycle
(state, instruction, capture_dr, shift_dr, update_dr, tdo) traces are asserted equal.

A structural table diff (tests/test_tap_fsm_table.py) can't catch e.g. a capture/shift/update
strobe wired to the wrong state in the RTL's `case` statement even when the state table
itself is transcribed correctly: this is the check that actually would.
"""

from __future__ import annotations

import random
from collections import deque
from pathlib import Path

import pytest

from warptap import sim_io
from warptap.tap_fsm import TapState, next_state
from warptap.tap_model import IDCODE_VALUE, OPCODE_EXTEST, OPCODE_IDCODE, TapModel
from warptap.yosys_io import ingest

_RTL_PATH = Path(__file__).resolve().parent.parent / "src" / "warptap" / "rtl" / "tap_core.v"


def _bfs_path(start: TapState, goal: TapState) -> list[int]:
    """Shortest TMS sequence from `start` to `goal`, via BFS over tap_fsm.next_state.
    Used only to construct test stimulus: the correctness claim under test is whether
    RTL's resulting trace matches Python's for this exact stimulus, which is independent
    of how the stimulus was generated, so using next_state here isn't circular."""
    if start is goal:
        return []
    visited = {start}
    queue = deque([(start, [])])
    while queue:
        state, path = queue.popleft()
        for tms in (0, 1):
            nxt = next_state(state, tms)
            if nxt is goal:
                return path + [tms]
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, path + [tms]))
    raise AssertionError(f"no path found from {start} to {goal}")  # pragma: no cover


def directed_all_edges_stimulus() -> list[tuple]:
    """Visits every one of the 32 (state, tms) edges at least once: for each of the 16
    states, reset, drive a shortest path to it, then probe both tms=0 and tms=1 from
    there. TDI is 0 throughout every path segment; this is safe (never lands on
    EXTEST/SAMPLE_PRELOAD as the active instruction with a DR access still to come in
    the same iteration) because a BFS shortest path to a DR-side state never routes
    through the IR side, and the only iterations that touch Update-IR at all are the
    two probing Update-IR's own edges: where the probe tick is the last tick of that
    iteration, so no subsequent DR access ever observes the changed instruction."""
    entries: list[tuple] = []
    for state in TapState:
        path = _bfs_path(TapState.TEST_LOGIC_RESET, state)
        for tms in (0, 1):
            entries.append(("reset",))
            for step_tms in path:
                entries.append(("tick", step_tms, 0))
            entries.append(("tick", tms, 0))
    return entries


def constrained_random_stimulus(seed: int, num_cycles: int, ir_width: int) -> list[tuple]:
    """A random TMS walk (one continuous run, no resets), except: whenever the walk
    freshly enters SHIFT_IR, TDI is forced to 1 for `ir_width` consecutive cycles, so
    the instruction register always ends up all-1s (BYPASS's opcode) by the time
    Update-IR is next reached, regardless of prior content. This test stresses FSM
    transition and capture/shift/update *timing*, not instruction-selection coverage
    (already covered by test_tap_model.py and the dedicated directed test above):
    pinning the outcome to BYPASS avoids ever exercising TapModel's deliberate
    TapModelError boundary (EXTEST/SAMPLE_PRELOAD have no registered DR) in the middle
    of an otherwise-unrelated FSM stress test."""
    rng = random.Random(seed)
    state = TapState.TEST_LOGIC_RESET
    entries: list[tuple] = [("reset",)]
    forced_remaining = 0
    while len(entries) - 1 < num_cycles:
        if state is TapState.SHIFT_IR and forced_remaining == 0:
            forced_remaining = ir_width
        if forced_remaining > 0:
            tms, tdi = 0, 1
            forced_remaining -= 1
        else:
            tms, tdi = rng.randint(0, 1), rng.randint(0, 1)
        entries.append(("tick", tms, tdi))
        state = next_state(state, tms)
    return entries


def run_on_model(stimulus: list[tuple], model: TapModel) -> list[tuple]:
    """Step `model` through `stimulus`, sampling state/strobes/instruction *before*
    each tick mutates them (matching a real synchronous testbench, which can only
    observe register contents between clock edges): mirrors tb_tap_core.v's own
    sample-then-clock ordering exactly."""
    trace: list[tuple] = []
    for entry in stimulus:
        if entry[0] == "reset":
            model.reset()
            continue
        _, tms, tdi = entry
        pre_state = model.state
        pre_instruction_opcode = model.instruction_opcode()
        tdo = model.tick(tms, tdi)
        trace.append(
            (
                pre_state.value,
                pre_instruction_opcode,
                int(pre_state is TapState.CAPTURE_DR),
                int(pre_state is TapState.SHIFT_DR),
                int(pre_state is TapState.UPDATE_DR),
                tdo,
            )
        )
    return trace


def render_stimulus_file(stimulus: list[tuple]) -> str:
    lines = []
    for entry in stimulus:
        if entry[0] == "reset":
            lines.append("1 0 0")
        else:
            _, tms, tdi = entry
            lines.append(f"0 {tms} {tdi}")
    return "\n".join(lines) + "\n"


def run_on_rtl(
    stimulus: list[tuple],
    fixtures_dir: Path,
    iverilog_command: str,
    vvp_command: str,
) -> list[tuple]:
    stdout = sim_io.run_verilog_testbench(
        [_RTL_PATH, fixtures_dir / "tb_tap_core.v"],
        extra_inputs={"stimulus.txt": render_stimulus_file(stimulus)},
        iverilog_command=iverilog_command,
        vvp_command=vvp_command,
    )
    trace: list[tuple] = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, state, instr, cap, shift, upd, tdo = line.split(",")
        trace.append((int(state), int(instr), int(cap), int(shift), int(upd), int(tdo)))
    return trace


def test_directed_all_edges_cross_sim_matches(fixtures_dir, iverilog_command, vvp_command):
    stimulus = directed_all_edges_stimulus()
    python_trace = run_on_model(stimulus, TapModel(has_idcode=True))
    rtl_trace = run_on_rtl(stimulus, fixtures_dir, iverilog_command, vvp_command)
    assert rtl_trace == python_trace


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_randomized_cross_sim_matches(seed, fixtures_dir, iverilog_command, vvp_command):
    stimulus = constrained_random_stimulus(seed, num_cycles=300, ir_width=4)
    python_trace = run_on_model(stimulus, TapModel(has_idcode=True))
    rtl_trace = run_on_rtl(stimulus, fixtures_dir, iverilog_command, vvp_command)
    assert rtl_trace == python_trace


def test_test_logic_reset_reloads_idcode_on_rtl_and_model(
    fixtures_dir, iverilog_command, vvp_command
):
    """IEEE 1149.1: Test-Logic-Reset reloads IDCODE, so five TMS=1 cycles deselect EXTEST
    without a TRST pulse -- and the following DR scan reads the IDCODE value."""
    extest_ir = [("tick", tms, 0) for tms in (1, 1, 0, 0)]  # RUN_TEST_IDLE -> SHIFT_IR
    extest_ir += [("tick", 0, 0)] * 3 + [("tick", 1, 0)]  # shift 0000, exit to EXIT1_IR
    extest_ir += [("tick", 1, 0), ("tick", 0, 0)]  # UPDATE_IR -> RUN_TEST_IDLE
    tms_reset = [("tick", 1, 0)] * 5 + [("tick", 0, 0)]  # -> TEST_LOGIC_RESET -> RUN_TEST_IDLE
    idcode_dr = [("tick", tms, 0) for tms in (1, 0, 0)]  # -> SHIFT_DR, capturing IDCODE
    idcode_dr += [("tick", 0, 0)] * 31 + [("tick", 1, 0), ("tick", 1, 0), ("tick", 0, 0)]
    stimulus = [("reset",), ("tick", 0, 0)] + extest_ir + tms_reset + idcode_dr

    python_trace = run_on_model(stimulus, TapModel(has_idcode=True))
    rtl_trace = run_on_rtl(stimulus, fixtures_dir, iverilog_command, vvp_command)
    assert rtl_trace == python_trace

    ir_done = 1 + len(extest_ir)
    assert rtl_trace[ir_done][1] == OPCODE_EXTEST  # not vacuous: EXTEST really was loaded
    assert rtl_trace[ir_done + len(tms_reset)][1] == OPCODE_IDCODE
    shifted = [t[5] for t in rtl_trace if t[3] == 1]
    assert sum(bit << i for i, bit in enumerate(shifted)) == IDCODE_VALUE


def test_every_tap_core_register_is_reset_by_trst_n(yosys_command):
    """Every tap_core flop is an async-reset flop on trst_n, including the IDCODE/BYPASS
    shift registers, which Capture-DR always loads before they reach tdo. Without it they
    start unknown in a gate-level simulation, and a fault grader can't prove tdo X-free."""
    mod = ingest([_RTL_PATH], "tap_core", yosys_command=yosys_command)["modules"]["tap_core"]
    trst_n = mod["ports"]["trst_n"]["bits"]
    flops = [cell for cell in mod["cells"].values() if "dff" in cell["type"]]
    assert len(flops) == 5  # state, ir_shift, current_instruction_r, idcode_shift, bypass_bit
    for cell in flops:
        assert cell["type"] == "$adff"
        assert cell["connections"]["ARST"] == trst_n
        assert int(cell["parameters"]["ARST_POLARITY"], 2) == 0


def test_tap_core_ingests_via_yosys(yosys_command):
    """Proves the hand-authored RTL is real synthesizable input to warptap's own
    pipeline (Stage 1's ingest()), not merely iverilog-simulatable text."""
    raw = ingest([_RTL_PATH], "tap_core", yosys_command=yosys_command)
    assert "tap_core" in raw["modules"]
    assert raw["modules"]["tap_core"]["cells"]
