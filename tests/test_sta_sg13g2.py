"""Static timing analysis of warptap-inserted designs mapped to IHP SG13G2, with OpenSTA.

The netlists are the gate-level tests' (``gate_level.map_to_sg13g2``), timed against
``fixtures/sta/warptap.sdc`` with TCK at 50 MHz (20 ns), at the library's slow corner (1.08 V,
125 C) and its fast corner (1.32 V, -40 C). Every setup, hold, recovery, removal, pulse-width
and period check must pass at both. The SDC says what it assumes: a tester that drives TDI, TMS
and TRST on TCK's falling edge and samples TDO on its rising edge, with a tenth of a period of
skew each way, and each design's own clock asynchronous to TCK.

What this doesn't cover: placement, wires and a clock tree (the netlist is unplaced, so a net's
delay is its pins' load), and the SRAM macros' timing (openMBIST's macros are black boxes here,
so paths into and out of them are not timed).

Control: with TCK far too fast for the network, setup fails.

Skips without OpenSTA (``WARPTAP_STA_CMD``, or ``sta`` on PATH), as in CI, which doesn't build
it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import gate_level as gl
import test_mem_subsystem_mbist_pdl_verify as mbist
import test_tdo_drive_cross_sim as tdo
import test_uart_tx_cross_sim as uart
from test_gate_level_sg13g2 import SRAM_MACROS
from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import build_sib_plan
from warptap.yosys_io import ingest

STATED_TCK_PERIOD = 20.0  # ns: 50 MHz
FUNCTIONAL_CLOCKS = {"clk": 20.0}  # every design here has one clock of its own, named clk


@pytest.fixture(scope="module")
def sg13g2() -> gl.Sg13g2:
    lib, why = gl.find_sg13g2()
    if lib is None:
        pytest.skip(why)
    return lib


@pytest.fixture(scope="module")
def sta_command() -> str:
    command = gl.find_sta()
    if command is None:
        pytest.skip("OpenSTA not found: set WARPTAP_STA_CMD or put sta on PATH")
    return command


def _mapped(netlist_json, top, lib, yosys_command, macros=()):
    mapped = gl.map_to_sg13g2(netlist_json, top, lib, macros=macros, yosys_command=yosys_command)
    gl.check_mapped(mapped, macros, lib)
    return mapped


def _assert_meets_timing(mapped, lib, sta_command) -> gl.StaResult:
    result = gl.run_sta(
        mapped, lib, tck_period=STATED_TCK_PERIOD, functional_clocks=FUNCTIONAL_CLOCKS,
        sta_command=sta_command,
    )
    assert result.violators == "", result.violators
    slacks = {k: v for k, v in result.values.items() if "_slack_" in k}
    assert set(slacks) == {"max_slack_slow", "max_slack_fast", "min_slack_slow", "min_slack_fast"}
    assert all(v >= 0 for v in slacks.values()), slacks
    assert all(v == 0 for k, v in result.values.items() if "_tns_" in k), result.values
    return result


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_the_tristated_tdo_designs_meet_timing_at_50_mhz(
    insertion, sg13g2, sta_command, fixtures_dir, yosys_command
):
    netlist, top = tdo._INSERTIONS[insertion](fixtures_dir, yosys_command, "tristate")
    _assert_meets_timing(_mapped(netlist.to_json(), top, sg13g2, yosys_command), sg13g2, sta_command)


def test_uart_tx_meets_timing_at_50_mhz(sg13g2, sta_command, verilog_uart_dir, yosys_command):
    raw = ingest([verilog_uart_dir / "rtl" / "uart_tx.v"], "uart_tx", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan(uart._SPECS, top_name="uart_tx")
    insert_sib_network(netlist, "uart_tx", graph, yosys_command=yosys_command)
    _assert_meets_timing(_mapped(netlist.to_json(), "uart_tx", sg13g2, yosys_command), sg13g2, sta_command)


def test_mem_subsystem_mbist_meets_timing_at_50_mhz(
    sg13g2, sta_command, openmbist_dir, autombist_generator, yosys_command, tmp_path: Path
):
    sources = mbist._source_list(openmbist_dir, mbist._generate_wrappers(autombist_generator, tmp_path))
    raw = ingest(sources, "mem_subsystem_mbist", yosys_command=yosys_command, use_sv=True)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan(mbist._SPECS)
    insert_sib_network(netlist, "mem_subsystem_mbist", graph, yosys_command=yosys_command)
    mapped = _mapped(netlist.to_json(), "mem_subsystem_mbist", sg13g2, yosys_command, SRAM_MACROS)
    _assert_meets_timing(mapped, sg13g2, sta_command)


def test_control_tck_far_too_fast_fails_setup(sg13g2, sta_command, fixtures_dir, yosys_command):
    netlist, top = tdo._INSERTIONS["sib"](fixtures_dir, yosys_command, "tristate")
    mapped = _mapped(netlist.to_json(), top, sg13g2, yosys_command)
    result = gl.run_sta(
        mapped, sg13g2, tck_period=0.2, functional_clocks=FUNCTIONAL_CLOCKS, sta_command=sta_command
    )
    assert result.setup_slack < 0
    assert "VIOLATED" in result.violators
