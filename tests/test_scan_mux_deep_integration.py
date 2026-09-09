"""Multi-arm ScanMux plan Phase 8 (final phase): a mixed, 3-level-deep network -- a hierarchy
SIB (gate_outer) gates a ScanMuxNode (mux_inner), one of whose own arms directly gates a leaf
WRITE instrument while the OTHER arm gates a further hierarchy SIB (gate_status) gating a leaf
READ instrument. Mirrors test_nested_sib_deep_integration.py's own two-test shape (real RTL
cross-sim, then ICL round trip) combined with test_sib_insert_scan_mux_cross_sim.py's exact
plumbing (same real_signal.v/tb_real_signal.v fixture pair, same write-then-switch-arms-and-
read scenario) -- but exercises BOTH nesting directions in one graph (a SibNode nesting a
ScanMuxNode, and a ScanMuxNode arm nesting a SibNode), the one combination no earlier phase's
own tests build in a single network, and includes a genuine arm switch (mux_inner directly
from arm1 to arm2) inside a chain that also has its own separate SIB-opening rounds -- the
full generality every earlier phase's own tests only ever exercised in isolation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_emit import to_icl
from warptap.icl_import import import_icl
from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SibNode,
    SignalBinding,
    validate_physical_graph,
)
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sib_retarget import PathStep, open_path_to
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_play import play, shift_op_ranges, to_cycles
from warptap.tap_model import Instruction, TapModel
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4

_RESET_LEAD_IN = [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 1, 1)]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _mixed_graph_and_root():
    ctrl_write = InstrumentNode(
        name="ctrl_write", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("ctrl_in", 0),),
    )
    status_read = InstrumentNode(
        # Same real_signal.v settled value test_sib_insert_scan_mux_cross_sim.py's own
        # status_read predicts, for the same reason: the test author's job to predict what
        # ctrl_in -> status_out settles to, matching test_sib_insert_write_instrument_cross_
        # sim.py's own established convention.
        name="status_read", width=1, capture_value=1,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("status_out", 0),),
    )
    gate_status = SibNode(sib_name="gate_status", instrument=status_read)
    mux_inner = ScanMuxNode(
        "mux_inner", select_width=2,
        arms=(
            ScanArm(values=(1,), instrument=ctrl_write),
            ScanArm(values=(2,), nested=(gate_status,)),
        ),
    )
    gate_outer = SibNode(sib_name="gate_outer", instrument=None, nested=(mux_inner,))
    graph = PhysicalGraph(chain=(gate_outer,))
    validate_physical_graph(graph)
    root = ModuleInstance(
        "real_signal", children=(ModuleInstance("ctrl_write"), ModuleInstance("status_read"))
    )
    return graph, root


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = _mixed_graph_and_root()
    insert_sib_network(netlist, "real_signal", graph, yosys_command=yosys_command)
    return netlist, graph, root


def _render_stimulus(rows) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def run_on_python(graph, ir_ops) -> tuple[list[int], SibNetworkRegister]:
    model = TapModel(has_idcode=True)
    reg = SibNetworkRegister(graph)
    model.register_data_register(Instruction.EXTEST, reg)
    model.reset()
    reg.reset()
    model.tick(0, 0)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE settle
    return play(model, ir_ops), reg


def run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops):
    cycles = to_cycles(ir_ops)
    ranges = shift_op_ranges(ir_ops)
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1) for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-scan-mux-deep-") as tmpdir:
        path = Path(tmpdir) / "real_signal_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_real_signal.v"],
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
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN):]

    return [bits_to_int(tdo_by_cycle[start : start + bits]) for start, bits in ranges]


def test_write_then_switch_arms_and_read_matches_real_rtl_and_python_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """The core Phase 8 deliverable, proven on real hardware: gate_outer opens (its own
    round), mux_inner switches bypass -> arm1 (ctrl_write, its own round), the write commits
    (a third round) -- then, after settling, mux_inner switches DIRECTLY arm1 -> arm2 (the
    genuine arm-switch case this whole plan exists for) while gate_status (nested behind
    arm2) needs its own separate opening round on top of that, before status_read's now-
    settled value is finally read back. Confirms real RTL and the Python oracle agree bit for
    bit across this whole mixed-nesting, multi-round sequence -- not just each nesting
    direction in isolation, which is all any earlier phase's own tests ever built."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)

    assert open_path_to(graph, "ctrl_write") == (
        PathStep("gate_outer", 1), PathStep("mux_inner", 1),
    )
    assert open_path_to(graph, "status_read") == (
        PathStep("gate_outer", 1), PathStep("mux_inner", 2), PathStep("gate_status", 1),
    )

    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()  # open gate_outer, bypass -> arm1, write 1

    pdl.iRunLoop(3)  # >= real_signal.v's one-cycle ctrl_in -> status_out latency

    pdl.iTarget("status_read")
    pdl.iRead(1)
    second_ops = pdl.iApply()  # arm1 -> arm2 directly, open gate_status, then read

    ir_ops = _select_extest_ops() + first_ops + second_ops
    python_observed, _reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )
    assert rtl_observed == python_observed

    # Deliberately NOT asserting raw post-sequence sib_po/po literals here (e.g.
    # "mux_inner.po == [1, 0] for value 2"): a genuine, newly-confirmed generalization of the
    # already-known WRITE-instrument read-back aliasing gotcha (see sib_model.py's own
    # test_sib_insert_scan_mux_cross_sim.py-cited history) -- ANY further round that shifts
    # through an already-committed SIB/mux position relays new content through it right up to
    # that round's own update_dr, overwriting sib_po/po with whatever bit happened to be last
    # in transit, not the original commit value. Confirmed directly: after this test's own
    # second_ops (which shifts back through both gate_outer and mux_inner while reading
    # status_read), mux_inner.po comes back as [1, 1] -- decimal 3, not the arm2 value 2 that
    # was actually committed and used -- and gate_status.sib_po reads 0 even though it was
    # genuinely open throughout the read. Neither is a bug: the RTL/Python cross-sim match
    # above already proves the network behaved correctly; a raw register snapshot taken after
    # further shifting has occurred is simply not a reliable proxy for "is X still open/
    # matched" -- only immediately after the commit itself (before any further round), the
    # convention every earlier phase's own such assertions already restrict themselves to
    # (e.g. test_sib_overshift_scan_mux.py's own mux_slot.po check, taken right after phase 1
    # and before phase 2 ever runs).
    results = check_reads(ir_ops, rtl_observed)
    assert len(results) == 1
    assert results[0].passed


def test_mixed_sib_mux_network_round_trips_through_icl(icl_parser_module):
    """The other Phase 8 deliverable: the exact same mixed-nesting graph survives a full
    to_icl() -> import_icl() round trip, recovering both nesting directions -- gate_outer's
    own nested ScanMuxNode, and mux_inner's own arm2 nesting a further SibNode."""
    graph, root = _mixed_graph_and_root()
    icl_text = to_icl(graph, root, include_access_link=False)

    with tempfile.TemporaryDirectory(prefix="warptap-scan-mux-deep-icl-") as tmpdir:
        path = Path(tmpdir) / "warptap.icl"
        path.write_text(icl_text, encoding="utf-8")
        imported_graph, imported_root = import_icl(
            [path], "real_signal", icl_parser_module=icl_parser_module
        )

    assert {c.name for c in imported_root.children} == {"ctrl_write", "status_read"}

    gate_outer, imp_gate_outer = graph.chain[0], imported_graph.chain[0]
    assert imp_gate_outer.sib_name == gate_outer.sib_name == "gate_outer"
    assert imp_gate_outer.instrument is None
    assert len(imp_gate_outer.nested) == 1

    mux_inner, imp_mux_inner = gate_outer.nested[0], imp_gate_outer.nested[0]
    assert isinstance(imp_mux_inner, ScanMuxNode)
    assert imp_mux_inner.mux_name == mux_inner.mux_name == "mux_inner"
    assert imp_mux_inner.select_width == mux_inner.select_width == 2
    assert len(imp_mux_inner.arms) == 2

    imp_by_value = {arm.values[0]: arm for arm in imp_mux_inner.arms}
    assert set(imp_by_value) == {1, 2}

    write_arm = imp_by_value[1]
    assert write_arm.nested == ()
    assert write_arm.instrument.name == "ctrl_write"
    assert write_arm.instrument.direction == InstrumentDirection.WRITE

    nested_arm = imp_by_value[2]
    assert nested_arm.instrument is None
    assert len(nested_arm.nested) == 1
    imp_gate_status = nested_arm.nested[0]
    assert imp_gate_status.sib_name == "gate_status"
    assert imp_gate_status.instrument.name == "status_read"
    assert imp_gate_status.instrument.direction == InstrumentDirection.READ
