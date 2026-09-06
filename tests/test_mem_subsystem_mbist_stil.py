"""STIL emission on a real, non-trivial op sequence (implementation_plan.md §7 Stage 13):
proves the STIL emitter against a real 8-instrument network generated from the same external
design Stage 9's own `pdl_verify.py` tier-2 test already cross-simulates
(`openMBIST`'s `mem_subsystem_mbist`), not just small hand-built sequences.

Reuses `test_mem_subsystem_mbist_pdl_verify.py`'s own generation machinery and real scenario
verbatim (write `self_repair_start=1`, settle, read `self_repair_busy` expecting 1) -- this
file only adds a `to_stil()` render + `semiate_stil_parser` validation step on top of the
identical `pdl.program` that test already builds and separately cross-simulates against real
RTL. No `PulsePin` here (that's Part B's -- `test_mem_subsystem_mbist_faultflow_cross_sim.py` --
concern, which also needs a genuinely separate functional clock this design's *existing*
testbench doesn't wire up); this proves the emitter itself handles a real, larger sequence
cleanly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir_stil import to_stil
from warptap.yosys_io import ingest

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


def test_real_mem_subsystem_mbist_scenario_renders_valid_stil(
    openmbist_dir, autombist_generator, yosys_command, fixtures_dir, tmp_path,
    semiate_stil_parser,
):
    wrapper_paths = _generate_wrappers(autombist_generator, tmp_path)
    sources = _source_list(openmbist_dir, wrapper_paths)
    for src in sources:
        assert src.is_file(), f"expected real/generated openMBIST source missing: {src}"

    raw = ingest(sources, "mem_subsystem_mbist", yosys_command=yosys_command, use_sv=True)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "mem_subsystem_mbist", graph, yosys_command=yosys_command)

    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("self_repair_start")
    pdl.iWrite(1)
    pdl.iApply()
    pdl.iRunLoop(10)
    pdl.iTarget("self_repair_busy")
    pdl.iRead(1)
    pdl.iApply()
    pdl.iTarget("self_repair_start")
    pdl.iWrite(0)
    pdl.iApply()

    assert len(graph.chain) == 8  # all eight real instrument specs made it into the network

    stil_text = to_stil(pdl.program)

    with tempfile.TemporaryDirectory(prefix="warptap-mem-subsystem-mbist-stil-") as tmpdir:
        path = Path(tmpdir) / "mem_subsystem_mbist.stil"
        path.write_text(stil_text, encoding="utf-8")
        parser = semiate_stil_parser(str(path))
        syntax_ok = parser.parse_syntax()
        assert syntax_ok, f"syntax error: {parser.err_msg}"
        parser.parse_semantic()
        assert parser.is_parsing_done, f"semantic error: {parser.err_msg}"
        assert parser.err_msg == ""
