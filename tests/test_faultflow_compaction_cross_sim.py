"""RTL cross-simulation for faultflow compaction-aware pattern retargeting (Stage 24) -- the
gold-standard validation tier this project reserves for exactly this kind of correctness-
critical translation (mirrors test_faultflow_compression_cross_sim.py's own discipline, which
in turn mirrors test_faultflow_retarget_cross_sim.py's). Drives a REAL
faultflow.scan.compaction.insert_compaction()-produced, warptap-SIB-inserted netlist through
real Icarus Verilog, proving retarget_compacted_faultflow_patterns's op sequence (per-chain
writes via chain_to_instrument, a capture edge, then per-cycle tdo-channel sampling
interleaved with shift-clock pulses) actually works on real synthesized hardware -- including a
real, synthesized XOR-tree compactor genuinely folding two chains' worth of content into one
output bit, not just a single chain's value passing straight through.

Needs a real faultflow checkout (WARPTAP_FAULTFLOW_DIR / faultflow_dir fixture) -- see
test_faultflow_compression_cross_sim.py's own module docstring for why this is the one place
this project's "port understanding, not code" boundary doesn't apply.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

import pytest

from warptap.faultflow_compaction import retarget_compacted_faultflow_patterns
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import (
    GotoState,
    PulsePin,
    Runtest,
    ShiftDR,
    ShiftIR,
    bits_from_int,
    bits_to_int,
)
from warptap.tap_ir_play import navigation_tms, shift_tms
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4
_NUM_OUTPUTS = 2


def _dfxtp_cell(clk: int, d: int, q: int) -> dict:
    return {
        "hide_name": 0,
        "type": "sky130_fd_sc_hd__dfxtp_1",
        "parameters": {},
        "attributes": {},
        "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
        "connections": {"CLK": [clk], "D": [d], "Q": [q]},
    }


def _three_chain_core_json() -> dict:
    """Three independent 1-FF chains -- plain sky130 dfxtp cells. Unlike compression's own
    fixture, compaction's LOAD side is untouched (goes through chain_to_instrument's real
    JTAG-write mechanism exactly like faultflow_retarget.py's original precedent), so no
    scan_en-gated hold mux is needed here at all -- scan_in_N is a plain, directly
    instrument-writable port, captured on the retargeting function's own functional capture
    edge, exactly as test_faultflow_retarget_cross_sim.py's own functional_clock.v fixture
    already proved for the identical mechanism."""
    return {
        "modules": {
            "core_top": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "scan_en": {"direction": "input", "bits": [3]},
                    "scan_in_0": {"direction": "input", "bits": [4]},
                    "scan_in_1": {"direction": "input", "bits": [5]},
                    "scan_in_2": {"direction": "input", "bits": [6]},
                    "scan_out_0": {"direction": "output", "bits": [7]},
                    "scan_out_1": {"direction": "output", "bits": [8]},
                    "scan_out_2": {"direction": "output", "bits": [9]},
                },
                "cells": {
                    "u0": _dfxtp_cell(2, 4, 7),
                    "u1": _dfxtp_cell(2, 5, 8),
                    "u2": _dfxtp_cell(2, 6, 9),
                },
                "netnames": {
                    name: {"hide_name": 0, "bits": [bit], "attributes": {}}
                    for name, bit in [
                        ("clk", 2),
                        ("scan_en", 3),
                        ("scan_in_0", 4),
                        ("scan_in_1", 5),
                        ("scan_in_2", 6),
                        ("scan_out_0", 7),
                        ("scan_out_1", 8),
                        ("scan_out_2", 9),
                    ]
                },
            }
        }
    }


def _blackbox_stub_verilog(netlist_json: dict, defined_modules: set) -> str:
    """Identical to test_faultflow_compression_cross_sim.py's own helper of the same name --
    duplicated (not imported/shared, matching this project's "two occurrences" convention; see
    that module's own docstring for the full rationale)."""
    seen: dict = {}
    for mod in netlist_json.get("modules", {}).values():
        for cell in mod.get("cells", {}).values():
            cell_type = cell["type"]
            if cell_type in defined_modules or cell_type in seen:
                continue
            seen[cell_type] = dict(cell.get("port_directions", {}))
    lines: list = []
    for cell_type, ports in seen.items():
        lines.append(f"(* blackbox *) module {cell_type}({', '.join(ports)});")
        for name, direction in ports.items():
            lines.append(f"  {direction} {name};")
        lines.append("endmodule")
        lines.append("")
    return "\n".join(lines)


@pytest.fixture
def compaction_fixture(faultflow_dir: Path, yosys_command: str):
    """Real faultflow.scan.compaction.insert_compaction() output: a 3-chain, 2-output
    compaction-composed netlist, converted back to Verilog text, ready for warptap's own
    insert_test_access(). Skips (not fails) if faultflow isn't importable or yosys can't
    synthesize it."""
    src_dir = str(faultflow_dir)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from faultflow.scan.compaction import insert_compaction
    except ImportError as exc:
        pytest.skip(f"faultflow.scan.compaction not importable from {src_dir}: {exc}")

    with tempfile.TemporaryDirectory(prefix="warptap-compaction-fixture-") as tmpdir:
        tmp = Path(tmpdir)
        core_json_path = tmp / "core.json"
        core_json_path.write_text(
            json.dumps(_three_chain_core_json()), encoding="utf-8"
        )
        liberty = (
            faultflow_dir / "cells" / "sky130" / "sky130_fd_sc_hd__tt_025C_1v80.lib"
        )
        output_json = tmp / "compacted.json"
        output_json_path, compaction_map = insert_compaction(
            core_json_path,
            "core_top",
            ["scan_out_0", "scan_out_1", "scan_out_2"],
            _NUM_OUTPUTS,
            liberty=liberty,
            output_json=output_json,
            workdir=tmp / "work",
        )
        composed_json = json.loads(output_json_path.read_text(encoding="utf-8"))
        composed_verilog = write_verilog_from_json(
            composed_json, yosys_command=yosys_command
        )
        composed_v_path = tmp / "core_top_compacted.v"
        composed_v_path.write_text(composed_verilog, encoding="utf-8")
        blackbox_stub_verilog = _blackbox_stub_verilog(
            composed_json, set(composed_json.get("modules", {}))
        )

        persistent = Path(tempfile.mkdtemp(prefix="warptap-compaction-fixture-out-"))
        composed_v_out = persistent / "core_top_compacted.v"
        composed_v_out.write_text(composed_verilog, encoding="utf-8")
        stub_v_out = persistent / "sky130_blackbox_stub.v"
        stub_v_out.write_text(blackbox_stub_verilog, encoding="utf-8")
        # See test_faultflow_compression_cross_sim.py's own compression_fixture for why
        # `FUNCTIONAL` is required (real sky130 specify/timing-check blocks otherwise force X
        # on this crude, no-margin bit-banged testbench's very first clk edge).
        real_sky130_v_out = persistent / "sky130_fd_sc_hd.v"
        real_sky130_v_out.write_text(
            "`define FUNCTIONAL\n"
            + (faultflow_dir / "cells" / "sky130" / "sky130_fd_sc_hd.v").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        yield composed_v_out, stub_v_out, real_sky130_v_out, compaction_map


_SPECS_CHAIN_INSTRUMENTS = {0: "scan_in_0", 1: "scan_in_1", 2: "scan_in_2"}


def _instrument_specs() -> list:
    return [
        InstrumentSpec(
            "scan_in_0",
            width=1,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("scan_in_0", 0),),
        ),
        InstrumentSpec(
            "scan_in_1",
            width=1,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("scan_in_1", 0),),
        ),
        InstrumentSpec(
            "scan_in_2",
            width=1,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("scan_in_2", 0),),
        ),
        InstrumentSpec(
            "tdo_channel",
            width=_NUM_OUTPUTS,
            capture_value=0,
            signal_bits=tuple(
                SignalBinding("ff_tdo_channel", k) for k in range(_NUM_OUTPUTS)
            ),
        ),
    ]


_RESET_LEAD_IN: List[Tuple[int, int, int, int, int]] = [
    (0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0),
    (0, 0, 1, 0, 0),
]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # Instruction.EXTEST == 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _stimulus_rows(ir_ops) -> List[Tuple[int, int, int, int, int]]:
    """(tms, tdi, trst_n, scan_en, pulse_flag) per row -- identical shape to
    test_faultflow_compression_cross_sim.py's own helper (duplicated, not imported)."""
    state = TapState.RUN_TEST_IDLE
    rows: List[Tuple[int, int, int, int, int]] = []
    scan_en_value = 0

    def jtag_row(tms: int, tdi: int) -> None:
        nonlocal state
        rows.append((tms, tdi, 1, scan_en_value, 0))
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
            held = dict(op.hold_pins)
            scan_en_value = held.get("scan_en", scan_en_value)
            for _ in range(op.count):
                rows.append((0, 0, 1, scan_en_value, 1))
        else:
            raise ValueError(f"unsupported op {op!r}")
    return rows


def _correlate_skipping_pulses(ir_ops, tdo_by_cycle: List[int]) -> List[int]:
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


def test_compaction_retargeting_reads_the_real_xor_folded_value_on_real_rtl(
    compaction_fixture, yosys_command, iverilog_command, vvp_command
):
    composed_v_path, stub_v_path, real_sky130_v_path, compaction_map = (
        compaction_fixture
    )

    raw = ingest(
        [composed_v_path, stub_v_path],
        "core_top_compacted",
        yosys_command=yosys_command,
    )
    for name, mod_data in list(raw.get("modules", {}).items()):
        if mod_data.get("attributes", {}).get("blackbox"):
            del raw["modules"][name]
    netlist = Netlist.from_json(raw)
    top_mod = netlist.module("core_top_compacted")
    top_mod.rename_port("tdo", "ff_tdo_channel")
    graph, root = build_sib_plan(_instrument_specs(), top_name="core_top_compacted")
    insert_sib_network(
        netlist, "core_top_compacted", graph, yosys_command=yosys_command
    )

    # chain0=True, chain1=True, chain2=False -- with compaction_map.fanout == [[0,2],[1,2]]
    # (build_compactor_fanout(2,3)'s real, deterministic output), output0=chain0^chain2=True,
    # output1=chain1^chain2=True: BOTH compactor output bits genuinely differ from any single
    # chain's own raw value, so a real XOR-tree fold (not just a passthrough) is what this
    # test actually proves.
    load_seqs = {"0": [True], "1": [True], "2": [False]}
    expected_unload = {"0": [True, True], "1": [True, True], "2": [False, False]}
    patterns = [
        {
            "load_seqs": load_seqs,
            "expected_unload": expected_unload,
            "capture_pi_values": {},
        }
    ]

    retargeted = retarget_compacted_faultflow_patterns(
        patterns,
        _SPECS_CHAIN_INSTRUMENTS,
        "tdo_channel",
        graph,
        root,
        fanout=compaction_map.fanout,
        clock_port="clk",
        scan_enable_port="scan_en",
    )
    ir_ops = _select_extest_ops() + retargeted

    rows_stim = _RESET_LEAD_IN + _stimulus_rows(ir_ops)
    inserted_verilog = write_verilog_from_json(
        netlist.to_json(), yosys_command=yosys_command
    )
    with tempfile.TemporaryDirectory(prefix="warptap-compaction-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "core_top_compacted_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        fixtures_dir = Path(__file__).parent / "fixtures"
        stdout = run_verilog_testbench(
            [path, real_sky130_v_path, fixtures_dir / "tb_compaction_three_chain.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows_stim)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        fields = line.split(",")
        tdo = fields[6]
        # See test_faultflow_compression_cross_sim.py's own decode loop for why X-coercion
        # during pre-stimulus cycles is safe here too.
        tdo_by_cycle.append(0 if tdo == "x" else int(tdo))
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN) :]

    observed = _correlate_skipping_pulses(ir_ops, tdo_by_cycle)
    results = check_reads(ir_ops, observed)

    assert len(results) == 2  # two unload cycles, one tdo_channel iRead each
    for result in results:
        assert result.passed, (
            f"real RTL's compacted tdo did not match the XOR-folded expected value: "
            f"expected={result.expected}, observed={result.observed}, mask={result.mask}"
        )
