"""Integrity programs played on real inserted RTL (tap_integrity's own oracle is the Python
model; this is where it meets tap_core.v and the SIB/mux/instrument cells).

Positive: the program for a network passes on that network's RTL -- flat with a live READ
instrument (real_signal.v's status_out, masked), nested, and a ScanMux. Negative: a program
built for a different network (wrong width, swapped order, a missing SIB) or a different TAP
(IDCODE value, IR width) fails, naming the first test that sees the difference.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SignalBinding,
)
from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import HierarchySpec, InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_integrity import (
    IntegrityProgram,
    TapConfig,
    build_integrity_program,
    check_integrity,
)
from warptap.tap_model import IDCODE_VALUE
from warptap.yosys_io import ingest, write_verilog_from_json

_CTRL = InstrumentSpec(
    "ctrl_write", width=1, capture_value=0,
    direction=InstrumentDirection.WRITE,
    signal_bits=(SignalBinding("ctrl_in", 0),),
)
_STATUS = InstrumentSpec(
    "status_read", width=1, capture_value=0,
    direction=InstrumentDirection.READ,
    signal_bits=(SignalBinding("status_out", 0),),
)


def _mux_graph() -> tuple[PhysicalGraph, ModuleInstance]:
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(values=(1,), instrument=InstrumentNode(
                "ctrl_write", 1, 0, InstrumentDirection.WRITE, (SignalBinding("ctrl_in", 0),),
            )),
            ScanArm(values=(2,), instrument=InstrumentNode(
                "status_read", 1, 0, InstrumentDirection.READ, (SignalBinding("status_out", 0),),
            )),
        ),
    )
    root = ModuleInstance(
        "real_signal", children=(ModuleInstance("ctrl_write"), ModuleInstance("status_read"))
    )
    return PhysicalGraph(chain=(mux,)), root


def _inserted(fixtures_dir, yosys_command, design: str, graph: PhysicalGraph) -> str:
    raw = ingest([fixtures_dir / f"{design}.v"], design, yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    insert_sib_network(netlist, design, graph, yosys_command=yosys_command)
    return write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)


def _observed_tdo(
    fixtures_dir, iverilog_command, vvp_command, design: str, verilog: str,
    program: IntegrityProgram,
) -> list[int]:
    """One TDO per program cycle, sampled before the rising edge. The design's own reset
    follows TRST; trivial's a/b inputs stay 0."""
    if design == "real_signal":
        testbench, extra = "tb_real_signal.v", ""
    else:
        testbench, extra = "tb_sib_trivial.v", " 0 0"
    rows = [f"{c.tms} {c.tdi} {c.trst_n} {c.trst_n}{extra}" for c in program.cycles]
    with tempfile.TemporaryDirectory(prefix="warptap-integrity-cross-sim-") as tmpdir:
        path = Path(tmpdir) / f"{design}_inserted.v"
        path.write_text(verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / testbench],
            extra_inputs={"stimulus.txt": "\n".join(rows) + "\n"},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
    tdo = [line.split(",")[6] for line in stdout.splitlines() if line.startswith("TRACE,")]
    assert len(tdo) == len(program.cycles)
    return [int(v) if v in ("0", "1") else -1 for v in tdo]


@pytest.mark.parametrize("network", ["flat", "nested", "mux"])
def test_the_program_passes_on_its_own_network(
    network, fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    if network == "flat":
        graph, root = build_sib_plan([_CTRL, _STATUS], top_name="real_signal")
    elif network == "nested":
        graph, root = build_sib_plan(
            [HierarchySpec("bank", children=[_CTRL]), _STATUS], top_name="real_signal"
        )
    else:
        graph, root = _mux_graph()
    program = build_integrity_program(graph, root)
    verilog = _inserted(fixtures_dir, yosys_command, "real_signal", graph)

    observed = _observed_tdo(
        fixtures_dir, iverilog_command, vvp_command, "real_signal", verilog, program
    )
    result = check_integrity(program, observed)

    assert result.passed, result.failures
    assert result.compared > 200
    # Not vacuous: status_read is live, so some of its bits were don't-care.
    assert any(c.shift and not c.care for c in program.cycles)


_A = InstrumentSpec("a", width=1, capture_value=1)
_B = InstrumentSpec("b", width=3, capture_value=0b101)
_C = InstrumentSpec("c", width=2, capture_value=0b10)
_B2 = InstrumentSpec("b", width=2, capture_value=0b01)


@pytest.mark.parametrize(
    ("built", "inserted"),
    [([_A, _B2], [_A, _B]), ([_B, _A], [_A, _B]), ([_A, _B], [_A, _B, _C])],
    ids=["wrong_width", "swapped_order", "missing_sib"],
)
def test_a_program_for_another_network_fails_in_the_network_tests(
    built, inserted, fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    graph, root = build_sib_plan(built, top_name="trivial")
    program = build_integrity_program(graph, root)
    actual, _ = build_sib_plan(inserted, top_name="trivial")
    verilog = _inserted(fixtures_dir, yosys_command, "trivial", actual)

    result = check_integrity(
        program,
        _observed_tdo(fixtures_dir, iverilog_command, vvp_command, "trivial", verilog, program),
    )

    assert not result.passed
    network_tests = ("network_closed", "open_", "write_readback_")
    assert all(f.test.startswith(network_tests) for f in result.failures)


@pytest.mark.parametrize(
    ("tap", "first_failure"),
    [
        (TapConfig(idcode_value=IDCODE_VALUE ^ 0x100), "reset_instruction"),
        (TapConfig(ir_width=5), "instruction_register"),
    ],
    ids=["idcode_value", "ir_width"],
)
def test_a_program_for_another_tap_fails_in_the_tap_tests(
    tap, first_failure, fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    graph, root = build_sib_plan([_A, _B], top_name="trivial")
    program = build_integrity_program(graph, root, tap=tap)
    verilog = _inserted(fixtures_dir, yosys_command, "trivial", graph)

    result = check_integrity(
        program,
        _observed_tdo(fixtures_dir, iverilog_command, vvp_command, "trivial", verilog, program),
    )

    assert result.failures[0].test == first_failure
