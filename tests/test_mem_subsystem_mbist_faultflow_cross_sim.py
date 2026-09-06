"""RTL cross-simulation for faultflow pattern retargeting against a real external design
(implementation_plan.md §7 Stage 14) -- scales the proof `test_faultflow_retarget_cross_sim.py`
already established on the small `functional_clock.v` fixture up to the same real openMBIST
target (`mem_subsystem_mbist`) Stage 9's own `pdl_verify.py` tier-2 test cross-simulates.

`mem_subsystem_mbist`'s own functional clock (`clk`) and the inserted TAP's `tck` are two
genuinely separate ports on the DUT -- Stage 9's own `tb_mem_subsystem_mbist.v` just ties them
together at the harness level for convenience. `tb_mem_subsystem_mbist_faultflow.v` (new)
drives them independently instead, the same technique already proven on `functional_clock.v`,
so `PulsePin` pulsing `clk` alone (never `tck`) is a real, separate-clock-domain proof on this
design too, not just the toy fixture.

Scenario: reuses Stage 9's own proven one exactly (write `self_repair_start=1`, pulse the real
functional clock enough times for `S_IDLE -> S_ANALYZE_KICK` to fire, read `self_repair_busy`
expecting 1) -- but driven entirely through `retarget_faultflow_patterns()` + real RTL, not
`PDLInterpreter` calls made directly against raw tap_ir ops the way Stage 9's own test does.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Tuple

import yaml

from warptap.faultflow_retarget import retarget_faultflow_patterns
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR, bits_from_int, bits_to_int
from warptap.tap_ir_play import navigation_tms, shift_tms
from warptap.tap_ir_stil import to_stil
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
        "self_repair_start", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("self_repair_start"),),
    ),
    InstrumentSpec(
        "self_repair_busy", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("self_repair_busy"),),
    ),
]

# (tms, tdi, trst_n, rst_n, csb, we, mem_sel, addr, wdata, pulse) -- functional bus held idle
# (csb=1, disabled) throughout, matching test_mem_subsystem_mbist_pdl_verify.py's own
# _RESET_LEAD_IN exactly, extended with the trailing pulse column this testbench adds.
_RESET_LEAD_IN = [
    (0, 0, 0, 0, 1, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 1, 0, 0, 0, 0, 0),
    (0, 0, 1, 1, 1, 0, 0, 0, 0, 0),
]

_IDLE_BUS = (1, 0, 0, 0, 0)  # csb=1 (disabled), we=0, mem_sel=0, addr=0, wdata=0


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


def _stimulus_rows(ir_ops) -> List[Tuple[int, ...]]:
    """(tms, tdi, trst_n, rst_n, *_IDLE_BUS, pulse_flag) per row -- a PulsePin-aware extension
    of tap_ir_play.to_cycles()'s own per-cycle walk, mirroring
    test_faultflow_retarget_cross_sim.py's own _stimulus_rows() exactly, plus this design's own
    functional-bus columns (held idle throughout -- this scenario never touches the bus)."""
    state = TapState.RUN_TEST_IDLE
    rows: List[Tuple[int, ...]] = []

    def jtag_row(tms: int, tdi: int) -> None:
        nonlocal state
        rows.append((tms, tdi, 1, 1) + _IDLE_BUS + (0,))
        state = next_state(state, tms)

    for op in ir_ops:
        if isinstance(op, GotoState):
            for tms in navigation_tms(state, op.state):
                jtag_row(tms, 0)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            fed_bits = bits_from_int(op.tdi, op.bits)
            for tms, tdi in zip(shift_tms(op.bits), fed_bits):
                jtag_row(tms, tdi)
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                jtag_row(0, 0)
        elif isinstance(op, PulsePin):
            for _ in range(op.count):
                rows.append((0, 0, 1, 1) + _IDLE_BUS + (1,))
        else:
            raise ValueError(f"unsupported op {op!r}")
    return rows


def _correlate_skipping_pulses(ir_ops, tdo_by_cycle: List[int]) -> List[int]:
    """tap_ir_play.shift_op_ranges()-equivalent, extended for PulsePin -- identical accounting
    to test_faultflow_retarget_cross_sim.py's own _correlate_skipping_pulses()."""
    state = TapState.RUN_TEST_IDLE
    index = 0
    observed: List[int] = []
    for op in ir_ops:
        if isinstance(op, GotoState):
            tms_seq = navigation_tms(state, op.state)
            for tms in tms_seq:
                state = next_state(state, tms)
            index += len(tms_seq)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            observed.append(bits_to_int(tdo_by_cycle[index : index + op.bits]))
            for tms in shift_tms(op.bits):
                state = next_state(state, tms)
            index += op.bits
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                state = next_state(state, 0)
            index += op.count
        elif isinstance(op, PulsePin):
            index += op.count
        else:
            raise ValueError(f"unsupported op {op!r}")
    return observed


def _render_stimulus(rows) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


def test_faultflow_pattern_drives_real_self_repair_start_and_reads_busy(
    openmbist_dir, autombist_generator, yosys_command, iverilog_command, vvp_command,
    fixtures_dir, tmp_path,
):
    wrapper_paths = _generate_wrappers(autombist_generator, tmp_path)
    sources = _source_list(openmbist_dir, wrapper_paths)
    for src in sources:
        assert src.is_file(), f"expected real/generated openMBIST source missing: {src}"

    raw = ingest(sources, "mem_subsystem_mbist", yosys_command=yosys_command, use_sv=True)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS, top_name="mem_subsystem_mbist")
    insert_sib_network(netlist, "mem_subsystem_mbist", graph, yosys_command=yosys_command)

    patterns = [
        {
            "load_seqs": {"0": [True]},        # chain 0 -> self_repair_start
            "expected_unload": {"1": [True]},  # chain 1 -> self_repair_busy
            "capture_pi_values": {},
        }
    ]
    retargeted = retarget_faultflow_patterns(
        patterns,
        {0: "self_repair_start", 1: "self_repair_busy"},
        graph,
        root,
        clock_port="clk",
        clock_pulse_count=10,  # matches Stage 9's own >=1-cycle margin exactly
    )
    ir_ops = _select_extest_ops() + retargeted

    # Secondary: the same realistic, PulsePin-bearing sequence must also be real, valid STIL.
    to_stil(ir_ops, pulse_periods={"clk": "20ns"})

    rows = _RESET_LEAD_IN + _stimulus_rows(ir_ops)
    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-mem-subsystem-mbist-faultflow-") as tmpdir:
        path = Path(tmpdir) / "mem_subsystem_mbist_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_mem_subsystem_mbist_faultflow.v"],
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

    observed = _correlate_skipping_pulses(ir_ops, tdo_by_cycle)
    results = check_reads(ir_ops, observed)

    assert len(results) == 1  # exactly the one iRead(self_repair_busy) queued above
    assert results[0].passed, (
        f"real RTL did not report self_repair_busy=1 after retargeting a faultflow pattern "
        f"through the real independent functional clock: {results[0]}"
    )
