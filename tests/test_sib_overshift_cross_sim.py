"""RTL/Python cross-simulation for the ICL connectivity check (implementation_plan.md §7
Stage 6). Extends tests/test_pdl_interpreter_cross_sim.py's exact convention: builds probe ops
via sib_overshift.build_overshift_ops, gets the Python-side prediction via
sib_overshift.expected_probe_tdo, and separately lowers the same ops to raw (tms, tdi) tuples
(via tap_ir_play.to_cycles() + shift_op_ranges()) for the real RTL path, correlating the raw
per-cycle RTL trace back to per-op granularity for direct comparison.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_model import InstrumentNode, PhysicalGraph, SibNode
from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_overshift import build_overshift_ops, diagnose_overshift, expected_probe_tdo
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sib_layout import layout_bit_length
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_play import shift_op_ranges, to_cycles
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=1, capture_value=0),
]


def _select_extest_ops():
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph_real, _root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "trivial", graph_real, yosys_command=yosys_command)
    return netlist, graph_real


def _graph_with_sensor_a_width(width: int, capture_value: int = 0b101) -> PhysicalGraph:
    """Hand-constructed graph matching build_sib_plan's own naming convention
    (f"sib_{instrument_name}"), but with sensor_a's declared width deliberately different
    from what _SPECS above actually caused to be inserted -- simulating the real scenario
    Stage 6 exists for: an ICL description that has drifted from what was actually built."""
    return PhysicalGraph(chain=(
        SibNode("sib_sensor_a", InstrumentNode("sensor_a", width, capture_value)),
        SibNode("sib_sensor_b", InstrumentNode("sensor_b", 1, 0)),
    ))


def run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ops) -> int:
    cycles = to_cycles(ops)
    ranges = shift_op_ranges(ops)
    reset_lead_in = [(0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0), (0, 0, 1, 1, 0, 0)]
    rows = reset_lead_in + [(tms, tdi, 1, 1, 0, 0) for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-overshift-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "trivial_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_sib_trivial.v"],
            extra_inputs={
                "stimulus.txt": "\n".join(" ".join(str(v) for v in r) for r in rows) + "\n"
            },
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, _state, _instr, _cap, _shift, _upd, tdo, _y = line.split(",")
        tdo_by_cycle.append(int(tdo))
    tdo_by_cycle = tdo_by_cycle[len(reset_lead_in):]

    # The probe is the LAST ShiftIR/ShiftDR op in `ops` by construction (build_overshift_ops
    # always ends with the probe) -- take the last range regardless of preamble length.
    start, bits = ranges[-1]
    return bits_to_int(tdo_by_cycle[start : start + bits])


def test_happy_path_matches_on_real_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command):
    netlist, graph_real = _build_and_insert(fixtures_dir, yosys_command)
    target_open = frozenset({"sib_sensor_a"})
    ops = _select_extest_ops() + build_overshift_ops(graph_real, target_open, margin=4)

    expected = expected_probe_tdo(graph_real, ops)
    observed = run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ops)
    assert observed == expected

    probe = ops[-2]
    result = diagnose_overshift(
        graph_real, target_open, probe.bits, layout_bit_length(graph_real, target_open),
        expected, observed,
    )
    assert result.passed


def test_declared_width_wider_than_real_rtl_is_detected(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The flagship test: the check must catch a real ICL-description/RTL mismatch, not
    just a Python-model-vs-Python-model one. RTL is inserted from _SPECS (sensor_a width=3);
    the check is run against a hand-constructed graph declaring sensor_a as width=4 --
    simulating a downstream-drifted ICL description. Detected reliably (matches the
    pure-Python finding that this direction -- declared wider than real -- is caught even
    without extra margin, since exact-stream comparison only needs one mismatched bit)."""
    netlist, graph_real = _build_and_insert(fixtures_dir, yosys_command)
    graph_declared = _graph_with_sensor_a_width(4)
    target_open = frozenset({"sib_sensor_a"})

    for margin in (0, 3):
        ops = _select_extest_ops() + build_overshift_ops(graph_declared, target_open, margin=margin)
        expected = expected_probe_tdo(graph_declared, ops)
        observed = run_on_rtl(
            fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ops
        )
        probe = ops[-2]
        result = diagnose_overshift(
            graph_declared, target_open, probe.bits,
            layout_bit_length(graph_declared, target_open), expected, observed,
        )
        assert not result.passed


def test_inadequate_margin_can_miss_a_real_rtl_mismatch(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The margin-rationale companion negative control, execution-verified against real
    hardware rather than only the Python model (tests/test_sib_overshift.py's own
    test_margin_zero_can_let_a_real_length_defect_slip_through proves the mechanism in pure
    Python; this proves the SAME property survives real RTL simulation). RTL is inserted
    from _SPECS (sensor_a width=3); the check is run against a hand-constructed graph
    declaring sensor_a as width=1 -- empirically found (not assumed) to slip through
    undetected at low margin and get caught once margin is large enough."""
    netlist, graph_real = _build_and_insert(fixtures_dir, yosys_command)
    graph_declared = _graph_with_sensor_a_width(1)
    target_open = frozenset({"sib_sensor_a"})

    def run(margin):
        ops = _select_extest_ops() + build_overshift_ops(graph_declared, target_open, margin=margin)
        expected = expected_probe_tdo(graph_declared, ops)
        observed = run_on_rtl(
            fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ops
        )
        probe = ops[-2]
        return diagnose_overshift(
            graph_declared, target_open, probe.bits,
            layout_bit_length(graph_declared, target_open), expected, observed,
        )

    assert run(margin=0).passed is True  # real mismatch slips through
    assert run(margin=2).passed is False  # adequate margin catches it
