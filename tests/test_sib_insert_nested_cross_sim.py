"""RTL/Python cross-simulation for a NESTED (SIB-gates-SIB) network, against real synthesized
RTL -- the test the nested-SIB implementation plan's Phase 4 calls for, closing the loop from
Phase 0's standalone RTL spike (tests/test_sib_cell_nested_cross_sim.py) against real
insert_sib_network() output.

The first two tests below were written before SibNetworkRegister (sib_model.py) modeled
nested networks (a separate, later phase of the same plan) -- they deliberately used
pdl_verify.check_reads() directly against the real RTL-observed stream instead, since that
only ever reads a ShiftDR op's own tdo/mask fields (already correctly computed by
PDLInterpreter.iApply() via compose_bits/_read_mask_bits), needing no Python-side register
model at all. test_matches_python_model below closes that loop, now that SibNetworkRegister
is nested-aware too, mirroring test_pdl_interpreter_cross_sim.py's/test_sib_insert_write_
instrument_cross_sim.py's own full run_on_python-vs-run_on_rtl convention.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sib_plan import HierarchySpec, InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_play import play, shift_op_ranges, to_cycles
from warptap.tap_model import Instruction, TapModel
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4

_SPECS = [
    HierarchySpec(
        "bank_a",
        children=[
            InstrumentSpec(
                "ctrl_write", width=1, capture_value=0,
                direction=InstrumentDirection.WRITE,
                signal_bits=(SignalBinding("ctrl_in", 0),),
            ),
        ],
    ),
    InstrumentSpec(
        "status_read", width=1, capture_value=1,  # what ctrl_in=1 settles status_out to
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("status_out", 0),),
    ),
]

_RESET_LEAD_IN = [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 1, 1)]


def test_sib_network_register_models_a_nested_network_directly():
    """Pure-Python direct-state check, no RTL involved -- generalizes test_sib_insert_cross_
    sim.py's own test_sib_network_register_captures_only_the_open_slots_instrument_bits one
    level deeper: bank_a (hierarchy, no instrument) gates deep_a directly; sibling is an
    unrelated flat top-level instrument. Opening bank_a alone must NOT expose deep_a's own
    content (matching the RTL spike's own core finding: a closed nested level isn't part of
    the live chain) -- only opening BOTH does."""
    nested_graph, _root = build_sib_plan(
        [
            HierarchySpec(
                "bank_a", children=[InstrumentSpec("deep_a", width=3, capture_value=0b101)]
            ),
            InstrumentSpec("sibling", width=2, capture_value=0b11),
        ]
    )
    reg = SibNetworkRegister(nested_graph)
    bank_a_slot = reg._slots[0]
    deep_a_slot = bank_a_slot.children[0]
    sibling_slot = reg._slots[1]
    assert bank_a_slot.sib_name == "sib_bank_a"
    assert deep_a_slot.sib_name == "sib_deep_a"
    assert sibling_slot.sib_name == "sib_sibling"

    # bank_a open, deep_a still closed: capture must NOT expose deep_a's own CONTENT -- but
    # deep_a's own (closed) select bit still appears in the layout, exactly like a closed
    # top-level SIB always contributes its own 1 bit in the flat case.
    bank_a_slot.sib_po = 1
    reg.capture()
    assert bank_a_slot.sib_shift_ff == 1  # self-capture of its own po=1
    assert deep_a_slot.inst_shift_ff == [1, 0, 1]  # unconditional, matches the flat case exactly
    layout = reg._live_layout()
    assert [(ref.slot.sib_name, ref.bit) for ref in layout] == [
        ("sib_deep_a", None), ("sib_bank_a", None), ("sib_sibling", None),
    ]

    # Now also open deep_a: capture again, confirm its content IS now live.
    deep_a_slot.sib_po = 1
    reg.capture()
    layout = reg._live_layout()
    assert [(ref.slot.sib_name, ref.bit) for ref in layout] == [
        ("sib_deep_a", 0), ("sib_deep_a", 1), ("sib_deep_a", 2), ("sib_deep_a", None),
        ("sib_bank_a", None), ("sib_sibling", None),
    ]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS)
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
    with tempfile.TemporaryDirectory(prefix="warptap-nested-insert-cross-sim-") as tmpdir:
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


def test_write_through_a_nested_sib_reaches_a_real_host_signal(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """ctrl_write sits behind sib_bank_a (a hierarchy SIB with no instrument of its own) --
    reaching it needs iApply's own multi-round opening (bank_a first, then ctrl_write, per
    stage_open_sequence), exercised here for the first time against a real inserted network,
    not just the pure-Python unit tests already covering the arithmetic. status_read is a
    flat, ordinary top-level instrument -- confirms a nested write and a flat read coexist
    correctly in the same network."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()  # 2 phase-1 rounds (open bank_a, then ctrl_write) + phase 2

    pdl.iRunLoop(3)  # >= real_signal.v's one-cycle ctrl_in -> status_out latency
    pdl.iTarget("status_read")
    pdl.iRead(1)  # asserts ctrl_in really did reach status_out via the nested write
    second_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + first_ops + second_ops
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    results = check_reads(ir_ops, rtl_observed)
    assert len(results) == 1
    assert results[0].passed


def test_nested_write_survives_being_closed_and_reopened_on_real_rtl(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """Mirrors test_sib_insert_write_instrument_cross_sim.py's own written-value-persists
    case, but through a nested SIB: instrument_write.v has a real update latch, so the
    committed value survives sib_bank_a (and ctrl_write) being closed via a different
    intermediate target (status_read) and later reopened -- proven on real hardware."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()

    pdl.iTarget("status_read")  # unrelated flat target -- closes sib_bank_a/ctrl_write
    second_ops = pdl.iApply()

    pdl.iTarget("ctrl_write")
    pdl.iRead(1)  # asserts the write is still committed -- no iWrite() here, so this only
                   # observes whatever instrument_write.v's own po already holds
    third_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + first_ops + second_ops + third_ops
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    results = check_reads(ir_ops, rtl_observed)
    assert len(results) == 1
    assert results[0].passed


def test_nested_write_and_flat_read_matches_python_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """Closes the loop this file's own module docstring describes: now that
    SibNetworkRegister (sib_model.py) is nested-aware, the same scenario
    test_write_through_a_nested_sib_reaches_a_real_host_signal already proved against real
    RTL also matches the Python behavioral model bit-for-bit, exactly like every flat cross-
    sim test already does."""
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    first_ops = pdl.iApply()

    pdl.iRunLoop(3)
    pdl.iTarget("status_read")
    pdl.iRead(1)
    second_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + first_ops + second_ops
    python_observed, reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )

    assert rtl_observed == python_observed
    # Not vacuous: confirm the Python model's own state agrees that ctrl_write's nested SIB
    # (and its outer bank_a) really did close, and status_read really did open. ctrl_write's
    # own select bit is explicitly driven to 0 by this same phase-1 shift (compose_bits
    # closes every currently-open slot not in open_after, nested ones included, not merely
    # relying on its parent's gating to freeze it) -- and bank_a's own pre-edge po is still 1
    # during that same commit, so the close genuinely takes effect this edge, not later.
    bank_a_slot = next(s for s in reg._slots if s.sib_name == "sib_bank_a")
    ctrl_write_slot = next(s for s in bank_a_slot.children if s.sib_name == "sib_ctrl_write")
    status_read_slot = next(s for s in reg._slots if s.sib_name == "sib_status_read")
    assert bank_a_slot.sib_po == 0
    assert ctrl_write_slot.sib_po == 0
    assert status_read_slot.sib_po == 1
