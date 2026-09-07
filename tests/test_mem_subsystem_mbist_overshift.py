"""RTL cross-simulation for the ICL connectivity check against a real external design
(implementation_plan.md §7 Stage 6): extends `test_sib_overshift_cross_sim.py`'s own proof
(against a small toy fixture) to the real openMBIST target (`mem_subsystem_mbist`) Stage 9's
own `pdl_verify.py` tier-2 test cross-simulates -- the connectivity check itself was never
re-validated against a real, non-trivial design until now.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_layout import layout_bit_length
from warptap.sib_overshift import build_overshift_ops, diagnose_overshift, expected_probe_tdo
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_play import shift_op_ranges, to_cycles
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4

_BASE_PORTS = {
    "clk": "clk0", "addr": "addr0", "din": "din0",
    "dout": "dout0", "we": "web0", "csb": "csb0",
}
_WRAPPERS = [
    ("sram_wrap_a", "selfrepair_a", 8, 32, "gen_a"),
    ("sram_wrap_b", "selfrepair_b", 9, 32, "gen_b"),
    ("sram_wrap_c", "selfrepair_c", 10, 8, "gen_c"),
]

_SPECS = [
    InstrumentSpec(
        "test_mode", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("test_mode"),),
    ),
    InstrumentSpec(
        "bist_start", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("bist_start"),),
    ),
    InstrumentSpec(
        "bist_done", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("bist_done"),),
    ),
    InstrumentSpec(
        "bist_fail", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("bist_fail"),),
    ),
    InstrumentSpec(
        "self_repair_start", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("self_repair_start"),),
    ),
    InstrumentSpec(
        "self_repair_done", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("self_repair_done"),),
    ),
    InstrumentSpec(
        "self_repair_fail", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("self_repair_fail"),),
    ),
    InstrumentSpec(
        "self_repair_busy", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("self_repair_busy"),),
    ),
]

_IDLE_BUS = (1, 0, 0, 0, 0)  # csb=1 (disabled), we=0, mem_sel=0, addr=0, wdata=0
_RESET_LEAD_IN = [
    (0, 0, 0, 0) + _IDLE_BUS,
    (0, 0, 0, 0) + _IDLE_BUS,
    (0, 0, 1, 1) + _IDLE_BUS,
]


def _generate_wrappers(autombist_generator, gen_dir: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for memory_name, wrapper_name, addr_width, data_width, subdir in _WRAPPERS:
        config = {
            "memory_name": memory_name,
            "wrapper_module_name": wrapper_name,
            "addr_width": addr_width,
            "data_width": data_width,
            "we_active_low": True,
            "ports": _BASE_PORTS,
            "redundancy": {"num_spare_rows": 1, "num_spare_cols": 0, "onchip_selfrepair": True},
            "read_latency": 0,
        }
        config_path = gen_dir / f"{subdir}.yml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        paths[subdir] = autombist_generator(config_path, gen_dir / subdir, algo="march-c")
    return paths


def _source_list(openmbist_dir: Path, wrapper_paths: dict[str, Path]) -> list[Path]:
    multimem_dir = openmbist_dir / "flow" / "multimem"
    mbist_dir = multimem_dir / "mbist"
    shared = wrapper_paths["gen_a"].parent
    return [
        shared / "march_c" / "march_c_algo.sv",
        shared / "march_c" / "march_c_fsm.sv",
        shared / "march_c" / "march_c_top.sv",
        shared / "onchip_row_repair_analyzer.sv",
        shared / "onchip_selfrepair_ctrl.sv",
        shared / "repair_remap_row.sv",
        wrapper_paths["gen_a"], wrapper_paths["gen_b"], wrapper_paths["gen_c"],
        mbist_dir / "sram_wrap_a.sv", mbist_dir / "sram_wrap_b.sv", mbist_dir / "sram_wrap_c.sv",
        multimem_dir / "sky130_sram_32b256w.v",
        multimem_dir / "sky130_sram_32b512w.v",
        multimem_dir / "sky130_sram_8b1024w.v",
        mbist_dir / "mem_subsystem_mbist.sv",
    ]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _build_and_insert(openmbist_dir, autombist_generator, yosys_command, tmp_path):
    wrapper_paths = _generate_wrappers(autombist_generator, tmp_path)
    sources = _source_list(openmbist_dir, wrapper_paths)
    for src in sources:
        assert src.is_file(), f"expected real/generated openMBIST source missing: {src}"

    raw = ingest(sources, "mem_subsystem_mbist", yosys_command=yosys_command, use_sv=True)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan(_SPECS, top_name="mem_subsystem_mbist")
    insert_sib_network(netlist, "mem_subsystem_mbist", graph, yosys_command=yosys_command)
    return netlist, graph


def _declared_graph_with_width_override(instrument_name: str, width: int):
    """Same real 8-instrument network, but with one instrument's declared width deliberately
    wrong -- simulating the real scenario Stage 6 exists for: an ICL description that has
    drifted from what was actually built. Mirrors test_sib_overshift_cross_sim.py's own
    `_graph_with_sensor_a_width`, generalized via NamedTuple._replace() instead of hand-listing
    all 8 slots (real transcription-error risk otherwise)."""
    modified_specs = [
        spec._replace(width=width) if spec.name == instrument_name else spec
        for spec in _SPECS
    ]
    graph, _root = build_sib_plan(modified_specs, top_name="mem_subsystem_mbist")
    return graph


def _run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ops) -> int:
    cycles = to_cycles(ops)
    ranges = shift_op_ranges(ops)
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1) + _IDLE_BUS for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-mem-subsystem-mbist-overshift-") as tmpdir:
        path = Path(tmpdir) / "mem_subsystem_mbist_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_mem_subsystem_mbist.v"],
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
        _, _state, _instr, _cap, _shift, _upd, tdo, _rdata = line.split(",")
        tdo_by_cycle.append(int(tdo))
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN):]

    # The probe is the LAST ShiftIR/ShiftDR op in `ops` by construction (build_overshift_ops
    # always ends with the probe) -- take the last range regardless of preamble length.
    start, bits = ranges[-1]
    return bits_to_int(tdo_by_cycle[start : start + bits])


def test_real_mem_subsystem_mbist_network_passes_overshift_check(
    openmbist_dir, autombist_generator, yosys_command, iverilog_command, vvp_command,
    fixtures_dir, tmp_path,
):
    netlist, graph = _build_and_insert(openmbist_dir, autombist_generator, yosys_command, tmp_path)
    target_open = frozenset({"sib_self_repair_start"})
    ops = _select_extest_ops() + build_overshift_ops(graph, target_open, margin=8)

    expected = expected_probe_tdo(graph, ops)
    observed = _run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ops)
    assert observed == expected

    probe = ops[-2]
    result = diagnose_overshift(
        graph, target_open, probe.bits, layout_bit_length(graph, target_open), expected, observed,
    )
    assert result.passed


def test_real_mem_subsystem_mbist_network_detects_a_drifted_width_declaration(
    openmbist_dir, autombist_generator, yosys_command, iverilog_command, vvp_command,
    fixtures_dir, tmp_path,
):
    """The flagship test, scaled to the real design: RTL is inserted with self_repair_start
    at its real width=1; the check runs against a hand-drifted graph declaring it width=2,
    simulating a real ICL-description/RTL mismatch on an actual external target -- not just
    the toy fixture test_sib_overshift_cross_sim.py already proves this against.

    margin=4 here is empirically confirmed (not assumed from the toy fixture's own finding --
    see the companion test below for why that assumption would have been wrong) to reliably
    catch this specific real defect; a margin sweep against the real RTL found the reliable
    threshold at margin=3 for this particular network, so margin=4 has real headroom above it,
    not just barely clearing the line."""
    netlist, _graph_real = _build_and_insert(
        openmbist_dir, autombist_generator, yosys_command, tmp_path
    )
    graph_declared = _declared_graph_with_width_override("self_repair_start", 2)
    target_open = frozenset({"sib_self_repair_start"})

    ops = _select_extest_ops() + build_overshift_ops(graph_declared, target_open, margin=4)
    expected = expected_probe_tdo(graph_declared, ops)
    observed = _run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ops)
    probe = ops[-2]
    result = diagnose_overshift(
        graph_declared, target_open, probe.bits,
        layout_bit_length(graph_declared, target_open), expected, observed,
    )
    assert not result.passed


def test_real_mem_subsystem_mbist_network_low_margin_can_miss_the_same_defect(
    openmbist_dir, autombist_generator, yosys_command, iverilog_command, vvp_command,
    fixtures_dir, tmp_path,
):
    """A real, empirically-found correction to an initial assumption, kept as its own test
    rather than silently fixed: the toy fixture's own test_declared_width_wider_than_real_rtl_is_detected
    found a "wider declared" defect is reliably caught even at margin=0 -- true for that small
    2-instrument network, but NOT universal. A margin sweep against this real, larger
    8-instrument network found the SAME direction of defect (self_repair_start declared
    width=2, really width=1) coincidentally slips through at margin=0, 1, AND 2, only becoming
    reliably caught starting at margin=3 -- a real, useful data point that the margin needed
    for a reliable guarantee is network-dependent, not a fixed constant, exactly matching this
    module's own documented "no simple formula" caveat, now demonstrated on real RTL at a
    larger scale than the toy fixture ever exercised."""
    netlist, _graph_real = _build_and_insert(
        openmbist_dir, autombist_generator, yosys_command, tmp_path
    )
    graph_declared = _declared_graph_with_width_override("self_repair_start", 2)
    target_open = frozenset({"sib_self_repair_start"})

    def run(margin):
        ops = _select_extest_ops() + build_overshift_ops(graph_declared, target_open, margin=margin)
        expected = expected_probe_tdo(graph_declared, ops)
        observed = _run_on_rtl(
            fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ops
        )
        probe = ops[-2]
        return diagnose_overshift(
            graph_declared, target_open, probe.bits,
            layout_bit_length(graph_declared, target_open), expected, observed,
        )

    assert run(margin=0).passed is True  # real mismatch coincidentally slips through
    assert run(margin=3).passed is False  # the real, empirically-found reliable threshold
