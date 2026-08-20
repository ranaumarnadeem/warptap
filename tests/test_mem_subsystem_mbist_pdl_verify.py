"""Tier 2 (implementation_plan.md §7 Stage 9 §5.2): PDL functional verification against the
REAL openMBIST target, `mem_subsystem_mbist` -- three real OpenRAM sky130 SRAM macros behind
one bus, wrapped with autonomous on-chip self-repair MBIST.

Deliberately does NOT use SibNetworkRegister/tap_ir_play.play()/TapModel at all (unlike every
prior stage's cross-sim tests): those model scan-mechanics against a Python stub with zero
knowledge of march_c_fsm.sv/onchip_selfrepair_ctrl.sv's real behavior -- pretending they could
predict `self_repair_busy` would defeat the entire point of this exercise (implementation_
plan.md §7 Stage 9 §4's own "two deliberately separate oracles" design). Instead: real RTL
only, PDL-declared expectations (`iRead`) checked against a real Icarus-observed trace via
`pdl_verify.check_reads()`.

Short, directed scenario (not a full self-repair pass -- already exhaustively covered by
openMBIST's own test suite, not this project's job to re-prove): write self_repair_start=1,
let onchip_selfrepair_ctrl.sv's S_IDLE -> S_ANALYZE_KICK transition fire (a registered
transition, one clock after self_repair_start reads high -- confirmed by direct reading of
that file's own FSM, not assumed), read self_repair_busy expecting 1
(`self_repair_busy = (ctrl_state != S_IDLE)`, combinational), then write self_repair_start=0.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pdl_verify import check_reads, correlate_observed
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_play import to_cycles
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4

# Mirrors openMBIST/tests/hardware/run_mem_subsystem_mbist_tb.py's own generation config
# exactly (implementation_plan.md §7 Stage 9 §5.2's own documented maintenance-drift risk if
# that reference config ever changes) -- same module names, widths, redundancy block, and
# critically read_latency: 0 (the real OpenRAM macro's decaying-dout timing requires it).
_BASE_PORTS = {
    "clk": "clk0", "addr": "addr0", "din": "din0",
    "dout": "dout0", "we": "web0", "csb": "csb0",
}
_WRAPPERS = [
    ("sram_wrap_a", "selfrepair_a", 8, 32, "gen_a"),
    ("sram_wrap_b", "selfrepair_b", 9, 32, "gen_b"),
    ("sram_wrap_c", "selfrepair_c", 10, 8, "gen_c"),
]

# One SIB per signal (implementation_plan.md §7 Stage 9's own v1 scope decision), matching
# mem_subsystem_mbist.sv's literal 8-signal MBIST/self-repair port enumeration 1:1.
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

# (tms, tdi, trst_n, rst_n, csb, we, mem_sel, addr, wdata) -- functional bus held idle
# (csb=1, disabled) throughout; this scenario never touches it.
_RESET_LEAD_IN = [
    (0, 0, 0, 0, 1, 0, 0, 0, 0),
    (0, 0, 0, 0, 1, 0, 0, 0, 0),
    (0, 0, 1, 1, 1, 0, 0, 0, 0),
]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
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
    # Only gen_a's shared march_c/onchip files: march_c_algo/fsm/top and
    # onchip_row_repair_analyzer/onchip_selfrepair_ctrl are parameterized Verilog modules
    # (one definition, instantiated per-memory with different width parameters) -- confirmed
    # by this project's own spike (§5.0): Yosys's `hierarchy` auto-derives a distinct
    # `$paramod` for each instantiation's parameter set from this single copy.
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


def _render_stimulus(rows) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def test_self_repair_start_write_and_busy_read_on_real_mem_subsystem_mbist(
    openmbist_dir, autombist_generator, yosys_command, iverilog_command, vvp_command,
    fixtures_dir, tmp_path,
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
    pdl.iRunLoop(10)  # >= 1 cycle for S_IDLE -> S_ANALYZE_KICK, generous margin
    pdl.iTarget("self_repair_busy")
    pdl.iRead(1)
    pdl.iApply()
    pdl.iTarget("self_repair_start")
    pdl.iWrite(0)
    pdl.iApply()
    ir_ops = _select_extest_ops() + pdl.program

    cycles = to_cycles(ir_ops)
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1, 1, 0, 0, 0, 0) for tms, tdi in cycles]

    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-mem-subsystem-mbist-") as tmpdir:
        path = Path(tmpdir) / "mem_subsystem_mbist_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_mem_subsystem_mbist.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows)},
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

    observed_by_shift_op = correlate_observed(ir_ops, tdo_by_cycle)
    results = check_reads(ir_ops, observed_by_shift_op)

    assert len(results) == 1  # exactly the one iRead(self_repair_busy) queued above
    assert results[0].passed, (
        f"self_repair_busy expected 1 after writing self_repair_start=1 and settling -- "
        f"got {results[0]}"
    )
