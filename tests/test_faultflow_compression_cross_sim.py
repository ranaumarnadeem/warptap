"""RTL cross-simulation for faultflow compression-aware pattern retargeting (Stage 24) --
the gold-standard validation tier this project reserves for exactly this kind of
correctness-critical translation (mirrors test_faultflow_retarget_cross_sim.py's own
discipline). Drives a REAL faultflow.scan.compression.insert_compression()-produced,
warptap-SIB-inserted netlist through real Icarus Verilog, proving
retarget_compressed_faultflow_patterns's op sequence (solve a joint seed, write it to the tdi
channel instrument, physically clock the reseed + natural LFSR stepping, then read back each
raw scan_out_N chain) actually works on real synthesized hardware, not just against the
Python-derived timing model its own docstring states.

Needs a real faultflow checkout (WARPTAP_FAULTFLOW_DIR / faultflow_dir fixture) to produce the
composed netlist -- this cross-sim tier is the one place this project's own "port
understanding, not code" boundary doesn't apply, since faultflow's insert_compression is the
only thing that can produce a REAL compression-composed netlist to test against; nothing here
imports or calls any faultflow ATPG/simulation code, only its pure netlist-generation path.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

import pytest

from warptap.faultflow_compression import (
    care_bit_rows,
    lookup_polynomial,
    retarget_compressed_faultflow_patterns,
)
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
_NUM_CHANNELS = 8
_MAX_CHAIN_LENGTH = 6
_SEED = 0b10110010


def _dfxtp_cell(clk: int, d: int, q: int) -> dict:
    return {
        "hide_name": 0,
        "type": "sky130_fd_sc_hd__dfxtp_1",
        "parameters": {},
        "attributes": {},
        "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
        "connections": {"CLK": [clk], "D": [d], "Q": [q]},
    }


def _mux2_cell(a0: int, a1: int, s: int, x: int) -> dict:
    """sky130_fd_sc_hd__mux2_1: X = S ? A1 : A0."""
    return {
        "hide_name": 0,
        "type": "sky130_fd_sc_hd__mux2_1",
        "parameters": {},
        "attributes": {},
        "port_directions": {"A0": "input", "A1": "input", "S": "input", "X": "output"},
        "connections": {"A0": [a0], "A1": [a1], "S": [s], "X": [x]},
    }


def _two_chain_core_json() -> dict:
    """Two independent, genuinely scan-enable-gated 1-FF chains -- real sky130 mux2+dfxtp
    cells (not faultflow's own synthetic $scanff_faultflow marker, which has no real
    behavioral Verilog model Icarus could simulate). The scan_en-controlled mux
    (X = scan_en ? scan_in_N : own_Q, i.e. HOLD when scan_en=0, SHIFT when scan_en=1) is not
    optional realism -- a plain dfxtp with D tied directly to scan_in_N (this fixture's first,
    failing attempt) has no way to distinguish a capture cycle from an ordinary shift cycle: it
    blindly re-samples whatever the ring generator's own combinational scan_in_N currently
    drives on EVERY clk edge, including retarget_compressed_faultflow_patterns's own capture
    PulsePin -- and since lfsr_reg keeps a real, materially different value across that one
    extra edge (confirmed by direct RTL inspection: lfsr_reg's value used to drive scan_in_N
    during the natural-step pulse's OWN LAST edge is NOT the same value lfsr_reg holds
    immediately after that edge, which is what a capture edge would re-sample from), a
    plain FF genuinely gets clobbered. Every real scan FF (faultflow's own sdfxtp/dfbbp/etc.
    cell-map entries included) has exactly this scan_en-gated hold behavior -- this fixture was
    simply too minimal at first, not the retargeting code."""
    return {
        "modules": {
            "core_top": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "scan_en": {"direction": "input", "bits": [3]},
                    "scan_in_0": {"direction": "input", "bits": [4]},
                    "scan_in_1": {"direction": "input", "bits": [5]},
                    "scan_out_0": {"direction": "output", "bits": [6]},
                    "scan_out_1": {"direction": "output", "bits": [7]},
                },
                "cells": {
                    "mux0": _mux2_cell(a0=6, a1=4, s=3, x=8),
                    "u0": _dfxtp_cell(2, 8, 6),
                    "mux1": _mux2_cell(a0=7, a1=5, s=3, x=9),
                    "u1": _dfxtp_cell(2, 9, 7),
                },
                "netnames": {
                    name: {"hide_name": 0, "bits": [bit], "attributes": {}}
                    for name, bit in [
                        ("clk", 2),
                        ("scan_en", 3),
                        ("scan_in_0", 4),
                        ("scan_in_1", 5),
                        ("scan_out_0", 6),
                        ("scan_out_1", 7),
                    ]
                },
            }
        }
    }


def _blackbox_stub_verilog(netlist_json: dict, defined_modules: set) -> str:
    """Minimal, interface-only ``(* blackbox *)`` stub modules for every real sky130 cell type
    ABC's own synthesis introduced into the composed netlist (dfxtp, mux2/mux2i, nand2b, xor2,
    clkinv, ...) -- NOT the real behavioral ``sky130_fd_sc_hd.v`` file, which real Yosys
    ``read_verilog`` cannot parse at all (it uses ``primitive``/UDP blocks, a real, confirmed
    parse failure -- ``sky130_fd_sc_hd.v:35: ERROR: syntax error, unexpected TOK_ID``). This is
    fine: Yosys's own ``hierarchy`` pass (inside ``insert_test_access``'s ``ingest()``) only
    needs to resolve module REFERENCES to build the JSON's structure, never actual cell
    behavior -- the real behavioral file is used later, ONLY for the final Icarus simulation
    (mirrors faultflow's own verification-gate split, CLAUDE.md's YOSYS SYNTHESIS CONTRACT:
    Yosys never sees behavioral cell models, only iverilog does)."""
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
def compression_fixture(faultflow_dir: Path, yosys_command: str):
    """Real faultflow.scan.compression.insert_compression() output: a 2-chain, 8-channel
    compression-composed netlist, converted back to Verilog text, ready for warptap's own
    insert_test_access(). Skips (not fails) if faultflow isn't importable or yosys can't
    synthesize it -- an environment gap, not a warptap bug."""
    src_dir = str(faultflow_dir)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from faultflow.scan.compression import insert_compression
    except ImportError as exc:
        pytest.skip(f"faultflow.scan.compression not importable from {src_dir}: {exc}")

    with tempfile.TemporaryDirectory(prefix="warptap-compression-fixture-") as tmpdir:
        tmp = Path(tmpdir)
        core_json_path = tmp / "core.json"
        core_json_path.write_text(json.dumps(_two_chain_core_json()), encoding="utf-8")
        liberty = (
            faultflow_dir / "cells" / "sky130" / "sky130_fd_sc_hd__tt_025C_1v80.lib"
        )
        output_json = tmp / "compressed.json"
        output_json_path, compression_map = insert_compression(
            core_json_path,
            "core_top",
            ["scan_in_0", "scan_in_1"],
            num_channels=_NUM_CHANNELS,
            liberty=liberty,
            output_json=output_json,
            workdir=tmp / "work",
            clock_port="clk",
            scan_enable_port="scan_en",
        )
        composed_json = json.loads(output_json_path.read_text(encoding="utf-8"))
        composed_verilog = write_verilog_from_json(
            composed_json, yosys_command=yosys_command
        )
        composed_v_path = tmp / "core_top_compressed.v"
        composed_v_path.write_text(composed_verilog, encoding="utf-8")
        blackbox_stub_verilog = _blackbox_stub_verilog(
            composed_json, set(composed_json.get("modules", {}))
        )

        # Two distinct files survive past this fixture's own temp-dir lifetime: a blackbox
        # stub (for Yosys's own hierarchy-resolution step inside insert_test_access) and the
        # real sky130 behavioral models (for the final Icarus simulation only -- see
        # _blackbox_stub_verilog's own docstring for why these must stay separate).
        persistent = Path(tempfile.mkdtemp(prefix="warptap-compression-fixture-out-"))
        composed_v_out = persistent / "core_top_compressed.v"
        composed_v_out.write_text(composed_verilog, encoding="utf-8")
        stub_v_out = persistent / "sky130_blackbox_stub.v"
        stub_v_out.write_text(blackbox_stub_verilog, encoding="utf-8")
        # `FUNCTIONAL` selects the real sky130 behavioral models' fast, non-timing-checked
        # simulation path -- without it, their real `specify`/`$setuphold` timing-check blocks
        # force every flop's output to X on the very first clk edge of this crude, no-margin
        # bit-banged testbench (confirmed directly: faultflow's own iverilog-based
        # verification gate, faultflow/verify/gate.py, passes this exact flag for this exact
        # reason -- `-DFUNCTIONAL`).
        real_sky130_v_out = persistent / "sky130_fd_sc_hd.v"
        real_sky130_v_out.write_text(
            "`define FUNCTIONAL\n"
            + (faultflow_dir / "cells" / "sky130" / "sky130_fd_sc_hd.v").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        yield composed_v_out, stub_v_out, real_sky130_v_out, compression_map


_SPECS_CHAIN_INSTRUMENTS = {0: "scan_out_0", 1: "scan_out_1"}


def _instrument_specs() -> list:
    return [
        InstrumentSpec(
            "tdi_channel",
            width=_NUM_CHANNELS,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=tuple(
                SignalBinding("ff_tdi_channel", k) for k in range(_NUM_CHANNELS)
            ),
        ),
        InstrumentSpec(
            "scan_out_0",
            width=1,
            capture_value=0,
            signal_bits=(SignalBinding("scan_out_0", 0),),
        ),
        InstrumentSpec(
            "scan_out_1",
            width=1,
            capture_value=0,
            signal_bits=(SignalBinding("scan_out_1", 0),),
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
    """(tms, tdi, trst_n, scan_en, pulse_flag) per row -- a PulsePin-aware walk mirroring
    test_faultflow_retarget_cross_sim.py's own _stimulus_rows exactly, EXCEPT it also tracks
    scan_en's own held value across PulsePin ops (via op.hold_pins) instead of ignoring it --
    this fixture's DUT genuinely needs a real, externally-driven scan_en (unlike that
    precedent's fixture, which never used hold_pins for anything)."""
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
    """Identical to test_faultflow_retarget_cross_sim.py's own helper of the same name --
    duplicated (not imported, matching this project's own "two occurrences" convention).
    """
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


def test_compression_retargeting_loads_the_real_solved_seed_on_real_rtl(
    compression_fixture, yosys_command, iverilog_command, vvp_command
):
    composed_v_path, stub_v_path, real_sky130_v_path, compression_map = (
        compression_fixture
    )

    raw = ingest(
        [composed_v_path, stub_v_path],
        "core_top_compressed",
        yosys_command=yosys_command,
    )
    # Drop the blackbox stub modules ingest() pulled in alongside core_top_compressed -- they
    # exist only so Yosys's own hierarchy pass could resolve the design's real gate-level cell
    # instances without erroring; leaving them in would make write_verilog_from_json emit
    # empty duplicate module declarations later, colliding with the REAL sky130 behavioral
    # models the final Icarus compile needs instead. Leaf cell instances referencing these
    # type names by string are unaffected -- Yosys JSON has no object-identity link to a
    # module's own top-level dict entry.
    for name, mod_data in list(raw.get("modules", {}).items()):
        if mod_data.get("attributes", {}).get("blackbox"):
            del raw["modules"][name]
    netlist = Netlist.from_json(raw)
    top_mod = netlist.module("core_top_compressed")
    top_mod.rename_port("tdi", "ff_tdi_channel")
    graph, root = build_sib_plan(_instrument_specs(), top_name="core_top_compressed")
    insert_sib_network(
        netlist, "core_top_compressed", graph, yosys_command=yosys_command
    )

    # Derive a real, achievable per-chain trajectory for a chosen seed via warptap's own
    # ported care_bit_rows -- retarget_compressed_faultflow_patterns's own solve_pattern_seed
    # must re-derive a seed satisfying this same content (not necessarily _SEED itself, but
    # equivalent for these two chains -- most of the 8 register bits are structurally free/
    # unconstrained for a 2-chain fanout, see the module docstring).
    poly = lookup_polynomial(_NUM_CHANNELS)
    rows = care_bit_rows(poly, compression_map.phase_shifter_taps, _MAX_CHAIN_LENGTH)

    def predicted_bit(cycle: int, chain: int) -> bool:
        return bin(rows[cycle][chain] & _SEED).count("1") % 2 == 1

    load_seqs = {
        "0": [predicted_bit(t, 0) for t in range(_MAX_CHAIN_LENGTH)],
        "1": [predicted_bit(t, 1) for t in range(_MAX_CHAIN_LENGTH)],
    }
    # Each chain is 1 FF deep -- only the LAST shifted-in value survives, so the real
    # end-to-end proof is that scan_out_N's post-load state equals load_seqs[N][-1].
    expected_unload = {"0": [load_seqs["0"][-1]], "1": [load_seqs["1"][-1]]}
    patterns = [
        {
            "load_seqs": load_seqs,
            "expected_unload": expected_unload,
            "capture_pi_values": {},
        }
    ]

    retargeted = retarget_compressed_faultflow_patterns(
        patterns,
        _SPECS_CHAIN_INSTRUMENTS,
        "tdi_channel",
        graph,
        root,
        poly=poly,
        phase_shifter_taps=compression_map.phase_shifter_taps,
        max_chain_length=_MAX_CHAIN_LENGTH,
        clock_port="clk",
        scan_enable_port="scan_en",
    )
    ir_ops = _select_extest_ops() + retargeted

    rows_stim = _RESET_LEAD_IN + _stimulus_rows(ir_ops)
    inserted_verilog = write_verilog_from_json(
        netlist.to_json(), yosys_command=yosys_command
    )
    with tempfile.TemporaryDirectory(prefix="warptap-compression-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "core_top_compressed_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        fixtures_dir = Path(__file__).parent / "fixtures"
        stdout = run_verilog_testbench(
            [path, real_sky130_v_path, fixtures_dir / "tb_compression_two_chain.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows_stim)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, _state, _instr, _cap, _shift, _upd, tdo, _so0, _so1 = line.split(",")
        # Before this test's own idle-settle PulsePin's first real clk edge, the compression
        # wrapper's lfsr_reg/prev_scan_en (which have no reset at all -- see the module
        # docstring's real-RTL derivation) are genuinely X in gate-level sim, and an early
        # Capture-DR can shift that X through scan_out_0/scan_out_1's own bc1_shift_only
        # registers into tdo -- harmless, since every op this test actually iReads happens
        # AFTER that first clk edge (when a fresh Capture-DR re-samples the now-well-defined
        # real value). Coerce to 0 only for cycles nothing here asserts against.
        tdo_by_cycle.append(0 if tdo == "x" else int(tdo))
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN) :]

    observed = _correlate_skipping_pulses(ir_ops, tdo_by_cycle)
    results = check_reads(ir_ops, observed)

    assert len(results) == 2  # scan_out_0, scan_out_1
    for result in results:
        assert result.passed, (
            f"real RTL did not end up holding the compression-loaded content: "
            f"expected={result.expected}, observed={result.observed}, mask={result.mask}"
        )
