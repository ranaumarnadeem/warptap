"""Broader validation for the nested-SIB plan's final phase: a THREE-level-deep network
(bank_a gates bank_b gates a leaf instrument), exercised through the whole pipeline in one
scenario -- planning, real RTL insertion, PDL retargeting (a genuine 3-round iApply, cross-
simmed against both real RTL and the Python model), and ICL emit/import round-trip. Every
earlier phase's own tests only ever went 2 levels deep; this confirms the recursion genuinely
generalizes, not just happens to work for the one depth actually tested so far.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_emit import to_icl
from warptap.icl_import import import_icl
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sib_plan import HierarchySpec, InstrumentSpec, build_sib_plan
from warptap.sib_retarget import open_path_to, stage_open_sequence
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
            HierarchySpec(
                "bank_b",
                children=[InstrumentSpec("deep_sensor", width=2, capture_value=0b10)],
            ),
        ],
    ),
]

_RESET_LEAD_IN = [(0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0), (0, 0, 1, 1, 0, 0)]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "trivial", graph, yosys_command=yosys_command)
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
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1, 0, 0) for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-deep-nested-") as tmpdir:
        path = Path(tmpdir) / "trivial_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_sib_trivial.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, _state, _instr, _cap, _shift, _upd, tdo, _y = line.split(",")
        tdo_by_cycle.append(int(tdo))
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN):]

    return [bits_to_int(tdo_by_cycle[start : start + bits]) for start, bits in ranges]


def test_three_level_nested_read_matches_real_rtl_and_python_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    netlist, graph, root = _build_and_insert(fixtures_dir, yosys_command)

    # The retargeting path itself is genuinely 3 deep -- not exercised by any earlier phase's
    # own tests, which only ever went 2 levels.
    target_path = open_path_to(graph, "deep_sensor")
    assert target_path == ("sib_bank_a", "sib_bank_b", "sib_deep_sensor")
    assert len(stage_open_sequence(graph, frozenset(target_path))) == 3

    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("deep_sensor")
    pdl.iRead(0b10)
    ir_ops = _select_extest_ops() + pdl.iApply()

    python_observed, reg = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )
    assert rtl_observed == python_observed

    # Not vacuous: confirm all three levels really did commit open, per the Python model's own
    # state -- which the assertion above just proved matches real RTL.
    bank_a = next(s for s in reg._slots if s.sib_name == "sib_bank_a")
    bank_b = next(s for s in bank_a.children if s.sib_name == "sib_bank_b")
    deep_sensor = next(s for s in bank_b.children if s.sib_name == "sib_deep_sensor")
    assert bank_a.sib_po == 1
    assert bank_b.sib_po == 1
    assert deep_sensor.sib_po == 1

    # The iRead(0b10) expectation itself was satisfied on the real RTL-observed stream --
    # check_reads() only ever reads iApply()'s own tdo/mask fields, needing no hand-derived
    # expected bit pattern here.
    results = check_reads(ir_ops, rtl_observed)
    assert len(results) == 1
    assert results[0].passed


def test_three_level_nested_network_round_trips_through_icl(icl_parser_module):
    graph, root = build_sib_plan(_SPECS, top_name="chip")
    icl_text = to_icl(graph, root, include_access_link=False)

    with tempfile.TemporaryDirectory(prefix="warptap-deep-nested-icl-") as tmpdir:
        path = Path(tmpdir) / "warptap.icl"
        path.write_text(icl_text, encoding="utf-8")
        imported_graph, _imported_root = import_icl(
            [path], "chip", icl_parser_module=icl_parser_module
        )

    bank_a = graph.chain[0]
    bank_b = bank_a.nested[0]
    deep_sensor = bank_b.nested[0]
    imp_bank_a = imported_graph.chain[0]
    imp_bank_b = imp_bank_a.nested[0]
    imp_deep_sensor = imp_bank_b.nested[0]

    assert imp_bank_a.sib_name == bank_a.sib_name == "sib_bank_a"
    assert imp_bank_b.sib_name == bank_b.sib_name == "sib_bank_b"
    assert imp_deep_sensor.sib_name == deep_sensor.sib_name == "sib_deep_sensor"
    assert imp_deep_sensor.instrument.name == deep_sensor.instrument.name == "deep_sensor"
    assert imp_deep_sensor.instrument.width == deep_sensor.instrument.width == 2
    assert imp_bank_a.instrument is None
    assert imp_bank_b.instrument is None
