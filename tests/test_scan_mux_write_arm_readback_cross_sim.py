"""RTL spike (mux-arm WRITE-readback bug fix plan, Phase 0): does a WRITE instrument's own
committed value correctly propagate to TDO when read back through a matched ScanMuxNode arm --
on real RTL, not just reasoned about. Mirrors test_sib_insert_scan_mux_cross_sim.py's own real
`sib_insert.py`-driven fixture pattern (not test_scan_mux_cell_cross_sim.py's standalone-
primitive style) specifically because `sib_insert.py`'s own wiring, `PDLInterpreter.iApply`'s
own real op emission, and `sib_layout.compose_bits`/`layout_bit_length` are ALL already
confirmed correct (see this plan's own Context) -- this spike's only open question is what the
*observed* per-cycle `so`/TDO sequence actually is for a matched-arm round, so reusing the
already-proven emission pipeline to build real stimulus is lower-risk than hand-deriving a
parallel one.

Uses `tests/fixtures/wide_signal.v` (a multi-bit sibling of `real_signal.v`, kept separate --
see its own module docstring for why) so a committed value's own bit pattern is actually
distinguishable from a single stuck bit, which the existing 1-bit `real_signal.v`-based tests
structurally cannot demonstrate (their `check_reads()` results agree on `passed=True`/`False`
for a *masked* comparison, but never establish *which* raw cycle each bit should land on).

Deliberately asserts against the **raw per-cycle TDO trace directly**, never through
`pdl_verify.check_reads()`/`PDLInterpreter._target_layout` -- both are exactly what this bug is
in, so validating this spike's own hypothesis through them would risk it silently confirming
itself against the same mistake it exists to catch.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_model import InstrumentDirection, ModuleInstance, ScanArm, ScanMuxNode, PhysicalGraph, SignalBinding
from warptap.icl_model import InstrumentNode
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_insert import insert_sib_network
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftDR, ShiftIR, bits_to_int
from warptap.tap_ir_play import shift_op_ranges, to_cycles
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4
_RESET_LEAD_IN = [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 1, 1)]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _mux_graph_and_root() -> tuple[PhysicalGraph, ModuleInstance]:
    """arm0 (values=(1,), WRITE width 3, bound to wide_signal's ctrl_in[2:0]) and arm1
    (values=(2,), WRITE width 2, bound to ctrl2_in[1:0]) -- the exact shape (select_width=2,
    width=3/2) that first reproduced the bug in the Python model."""
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(
                values=(1,),
                instrument=InstrumentNode(
                    name="arm0", width=3, capture_value=0,
                    direction=InstrumentDirection.WRITE,
                    signal_bits=(
                        SignalBinding("ctrl_in", 0),
                        SignalBinding("ctrl_in", 1),
                        SignalBinding("ctrl_in", 2),
                    ),
                ),
            ),
            ScanArm(
                values=(2,),
                instrument=InstrumentNode(
                    name="arm1", width=2, capture_value=0,
                    direction=InstrumentDirection.WRITE,
                    signal_bits=(
                        SignalBinding("ctrl2_in", 0),
                        SignalBinding("ctrl2_in", 1),
                    ),
                ),
            ),
        ),
    )
    graph = PhysicalGraph(chain=(mux,))
    root = ModuleInstance("wide_signal", children=(ModuleInstance("arm0"), ModuleInstance("arm1")))
    return graph, root


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "wide_signal.v"], "wide_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = _mux_graph_and_root()
    insert_sib_network(netlist, "wide_signal", graph, yosys_command=yosys_command)
    return netlist, graph, root


def _render_stimulus(rows) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def _run_rtl_raw(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops):
    """Like test_sib_insert_scan_mux_cross_sim.py's own run_on_rtl, but returns the RAW
    per-cycle tdo trace (one int per TCK cycle during a shift) rather than collapsing it into
    one int per ShiftDR/ShiftIR op -- this spike needs to inspect individual cycles, not just
    each op's own combined value."""
    cycles = to_cycles(ir_ops)
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1) for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-mux-write-readback-spike-") as tmpdir:
        path = Path(tmpdir) / "wide_signal_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_wide_signal.v"],
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
    return tdo_by_cycle[len(_RESET_LEAD_IN):]


def test_write_then_reread_same_arm_content_lands_in_the_first_width_cycles_msb_first(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The exact failing scenario, on real RTL, decoded by hand rather than through
    check_reads(): write 0b101 to arm0 (already open cold-start), then re-target arm0 with no
    intervening different target (the second iApply's own phase-1 is zero rounds -- a landed,
    unrelated optimization). Working hypothesis under test (see plan Context): the arm's own
    content becomes observable in the chronological cycles BEFORE the mux's own select-field
    bits (not after, as compose_bits' reused linear layout assumes), MSB-first."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("arm0")
    pdl.iWrite(0b101)
    first_ops = pdl.iApply()

    pdl.iTarget("arm0")  # SAME arm again, no intervening different target
    pdl.iRead(0b101)
    second_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + first_ops + second_ops
    tdo_by_cycle = _run_rtl_raw(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    ranges = shift_op_ranges(ir_ops)
    # ir_ops = [select_extest (1 shift), first_ops (phase1 1 round + phase2 1 round = 2
    # shifts), second_ops (phase1 0 rounds + phase2 1 round = 1 shift)] -- 4 shift ops total.
    assert len(ranges) == 4
    second_apply_start, second_apply_bits = ranges[-1]
    assert second_apply_bits == 3 + 2  # arm0 width + mux select_width, confirmed length
    raw = tdo_by_cycle[second_apply_start : second_apply_start + second_apply_bits]

    # Hypothesis: the first `width` (3) chronological cycles carry arm0's own content,
    # MSB-first (bit 2, then bit 1, then bit 0); the remaining select_width (2) cycles carry
    # whatever the mux's own internal register happens to hold (not asserted here -- that's a
    # separate, already-proven-correct property, see test_scan_mux_cell_cross_sim.py).
    content_cycles = raw[:3]
    assert content_cycles == [1, 0, 1], (
        f"expected arm0's own committed 0b101 MSB-first in the first 3 cycles, got {raw!r} "
        f"(full second-apply raw trace)"
    )


def test_write_then_switch_arms_then_switch_back_content_lands_in_first_width_cycles(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The other half of the hypothesis: does it hold when the round SWITCHES arms (not just
    re-targets the same one)? Write arm0=0b101, switch directly to arm1 (write 0b11), switch
    directly back to arm0 and read -- confirms the abandoned-arm's own width still counts
    toward the round length (already established, test_scan_mux_cell_cross_sim.py) AND that
    the newly-matched arm's content (arm1's 0b11, then arm0's 0b101 again) lands the same
    first-width-cycles-MSB-first way, not just for a same-arm re-read."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("arm0")
    pdl.iWrite(0b101)
    first_ops = pdl.iApply()

    pdl.iTarget("arm1")
    pdl.iWrite(0b11)
    second_ops = pdl.iApply()  # arm0 -> arm1 directly

    pdl.iTarget("arm0")
    pdl.iRead(0b101)
    third_ops = pdl.iApply()  # arm1 -> arm0 directly, read back arm0's still-committed value

    ir_ops = _select_extest_ops() + first_ops + second_ops + third_ops
    tdo_by_cycle = _run_rtl_raw(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    ranges = shift_op_ranges(ir_ops)
    # select_extest(1) + first_ops(phase1=1,phase2=1) + second_ops(phase1=1,phase2=1) +
    # third_ops(phase1=1,phase2=1) = 7 shift ops total (every apply here is a top-level mux
    # switch/re-target, so phase1 is always exactly 1 round -- confirmed by this count itself).
    assert len(ranges) == 7
    third_apply_phase2_start, third_apply_phase2_bits = ranges[-1]
    assert third_apply_phase2_bits == 3 + 2  # arm0's own width (matched arm) + select_width
    raw = tdo_by_cycle[third_apply_phase2_start : third_apply_phase2_start + third_apply_phase2_bits]

    content_cycles = raw[:3]
    assert content_cycles == [1, 0, 1], (
        f"expected arm0's own still-committed 0b101 MSB-first in the first 3 cycles after "
        f"switching away and back, got {raw!r}"
    )
