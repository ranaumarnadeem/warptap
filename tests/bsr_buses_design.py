"""The boundary-scan design the full-BSDL tests share: ``tests/fixtures/bsr_buses.v`` with a
boundary register inserted, its Verilog, its BSDL, and a live simulation of it.

The design has scalar, multi-bit and bidirectional pins, a ``[lo:hi]`` input, and two ports left
off the register (``clk`` as a clock, ``rst_n`` excluded). It is built with a non-default IDCODE,
so a test can tell the TAP reports the one it was given.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, NamedTuple, Tuple

from jtag_bridge import JtagBridge

from warptap.bsdl_emit import to_bsdl
from warptap.bsr_insert import insert_bsr
from warptap.bsr_plan import BsrPlan, build_bsr_plan
from warptap.netlist import Netlist
from warptap.yosys_io import ingest, write_verilog_from_json

TOP = "bsr_buses"
IDCODE = 0x1234_5679
TCK_HZ = 10e6


class BsrDesign(NamedTuple):
    plan: BsrPlan
    verilog: Path
    bsdl: str
    ports: List[Tuple[str, str, int]]  # (name, direction, width) after insertion
    workdir: Path

    def bridge(self, *, iverilog_command: str, vvp_command: str) -> JtagBridge:
        return JtagBridge(
            [self.verilog], TOP, self.ports, workdir=self.workdir,
            iverilog_command=iverilog_command, vvp_command=vvp_command,
        )


def build(fixtures_dir: Path, workdir: Path, yosys_command: str) -> BsrDesign:
    raw = ingest([fixtures_dir / f"{TOP}.v"], TOP, yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    plan = build_bsr_plan(
        netlist.module(TOP),
        clock_ports=frozenset({"clk"}),
        excluded_ports=frozenset({"rst_n"}),
    )
    insert_bsr(netlist, TOP, plan, idcode_value=IDCODE, yosys_command=yosys_command)
    verilog = workdir / f"{TOP}_inserted.v"
    verilog.write_text(
        write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command), encoding="utf-8"
    )
    ports = [(p.name, p.direction, len(p.bits)) for p in netlist.module(TOP).ports()]
    bsdl = to_bsdl(TOP, tck_max_freq_hz=TCK_HZ, idcode_value=IDCODE, boundary=plan)
    return BsrDesign(plan, verilog, bsdl, ports, workdir)
