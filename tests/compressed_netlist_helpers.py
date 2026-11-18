"""Netlist and stimulus helpers for the cross-sims of a compression-composed design
(``test_faultflow_continuous_cross_sim.py``): a two-chain sky130 core, interface-only blackbox
stubs of the cells synthesis brings in, and the PulsePin-aware stimulus walk that also tracks
the scan enable a ``PulsePin`` holds. They came from the one-shot decompressor's cross-sim,
removed with that retarget (warptap 0.0.5 is the last release that has it).
"""

from __future__ import annotations

from typing import List, Tuple

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

IR_WIDTH = 4


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
    drives on EVERY clk edge, including a retargeting function's own capture
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
