"""The inserted SIB network moves only under EXTEST (sib_insert.py's
``warptap_ijtag_extest_decode``). tap_core's capture/shift/update strobes fire on every
instruction's DR scan; before the network was gated on EXTEST, two all-ones DR scans under
IDCODE (loaded by every reset) or BYPASS (a board chain passing through) opened every SIB and
committed 1 into every WRITE instrument.

Real RTL (real_signal.v, a WRITE instrument driving ``ctrl_in``, so ``status_out`` shows any
commit one clock later) against TapModel + SibNetworkRegister, which TapModel drives only
under EXTEST. Each run ends with an EXTEST readout scan, so the network's state (which SIBs
are open, what each instrument holds) is compared too, not just the scans' own TDO.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftDR, ShiftIR
from warptap.tap_ir_play import play, shift_op_ranges, to_cycles
from warptap.tap_model import (
    OPCODE_EXTEST,
    OPCODE_IDCODE,
    Instruction,
    TapModel,
    bypass_opcode,
)
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4
SCAN_BITS = 8  # longer than the fully open network: 2 SIBs + 1 + 2 instrument bits

_SPECS = [
    InstrumentSpec(
        "ctrl_write", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("ctrl_in", 0),),
    ),
    InstrumentSpec("sensor", width=2, capture_value=0b10),  # fixed stub: model == RTL
]

_RESET_LEAD_IN = [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 1, 1)]  # tms, tdi, trst_n, rst_n
_TMS_RESET = [(1, 0)] * 5 + [(0, 0)]  # -> TEST_LOGIC_RESET -> RUN_TEST_IDLE, no TRST pulse


def _select(opcode: int) -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=opcode),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _dr_scan(tdi: int) -> list:
    return [
        GotoState(TapState.SHIFT_DR),
        ShiftDR(SCAN_BITS, tdi=tdi),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


_ALL_ONES = (1 << SCAN_BITS) - 1
_READOUT = _select(OPCODE_EXTEST) + _dr_scan(0)


@pytest.fixture(scope="module")
def inserted(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "real_signal", graph, yosys_command=yosys_command)
    verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    return graph, verilog


def _run_on_python(graph, segments) -> list[int]:
    """``segments``: ("ops", ir_ops) played through tap_ir_play, or ("raw", [(tms, tdi)])
    ticked directly. Returns one observed TDO int per ShiftIR/ShiftDR op."""
    model = TapModel(has_idcode=True)
    reg = SibNetworkRegister(graph)
    model.register_data_register(Instruction.EXTEST, reg)
    model.reset()
    reg.reset()
    model.tick(0, 0)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE settle
    observed: list[int] = []
    for kind, body in segments:
        if kind == "ops":
            observed += play(model, body)
        else:
            for tms, tdi in body:
                model.tick(tms, tdi)
    return observed


def _run_on_rtl(fixtures_dir, iverilog_command, vvp_command, verilog, segments):
    """Returns (one observed TDO int per shift op, per-cycle instruction, per-cycle
    status_out), cycles counted after the reset lead-in."""
    cycles: list[tuple[int, int]] = []
    ranges: list[tuple[int, int]] = []
    for kind, body in segments:
        if kind == "ops":
            ranges += [(len(cycles) + start, bits) for start, bits in shift_op_ranges(body)]
            cycles += to_cycles(body)
        else:
            cycles += list(body)
    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1) for tms, tdi in cycles]
    with tempfile.TemporaryDirectory(prefix="warptap-extest-select-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "real_signal_sib_inserted.v"
        path.write_text(verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_real_signal.v"],
            extra_inputs={
                "stimulus.txt": "\n".join(" ".join(str(v) for v in r) for r in rows) + "\n"
            },
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
    trace = [line.split(",") for line in stdout.splitlines() if line.startswith("TRACE,")]
    trace = trace[len(_RESET_LEAD_IN):]
    tdo = [int(t[6]) for t in trace]
    observed = [
        sum(bit << i for i, bit in enumerate(tdo[start : start + bits]))
        for start, bits in ranges
    ]
    return observed, [int(t[2]) for t in trace], [int(t[7]) for t in trace]


@pytest.mark.parametrize(
    "loaded", [[], _select(bypass_opcode(IR_WIDTH))], ids=["idcode_after_reset", "bypass"]
)
def test_dr_scans_outside_extest_leave_the_network_untouched(
    loaded, inserted, fixtures_dir, iverilog_command, vvp_command
):
    graph, verilog = inserted
    segments = [("ops", loaded + _dr_scan(_ALL_ONES) + _dr_scan(_ALL_ONES) + _READOUT)]

    python_observed = _run_on_python(graph, segments)
    rtl_observed, _instr, status = _run_on_rtl(
        fixtures_dir, iverilog_command, vvp_command, verilog, segments
    )

    assert rtl_observed == python_observed
    assert python_observed[-1] == 0  # both SIBs still closed, both captured 0
    assert set(status) == {0}  # ctrl_write never committed


def test_the_same_scans_under_extest_open_and_commit(
    inserted, fixtures_dir, iverilog_command, vvp_command
):
    """Positive control: the gate is on EXTEST, not stuck off."""
    graph, verilog = inserted
    extest = _select(OPCODE_EXTEST)
    segments = [("ops", extest + _dr_scan(_ALL_ONES) + _dr_scan(_ALL_ONES) + _READOUT)]

    python_observed = _run_on_python(graph, segments)
    rtl_observed, _instr, status = _run_on_rtl(
        fixtures_dir, iverilog_command, vvp_command, verilog, segments
    )

    assert rtl_observed == python_observed
    assert python_observed[-1] != 0  # the readout sees the opened network
    assert status[-1] == 1  # ctrl_write committed 1 into ctrl_in


def test_test_logic_reset_deselects_the_network(
    inserted, fixtures_dir, iverilog_command, vvp_command
):
    """Five TMS=1 cycles reload IDCODE (no TRST pulse), so the next all-ones scan, which
    under EXTEST would commit into the already-open network, commits nothing. TLR doesn't
    reset the network itself: the readout still sees both SIBs open."""
    graph, verilog = inserted
    segments = [
        ("ops", _select(OPCODE_EXTEST) + _dr_scan(_ALL_ONES)),  # opens both SIBs
        ("raw", _TMS_RESET),
        ("ops", _dr_scan(_ALL_ONES) + _READOUT),
    ]

    python_observed = _run_on_python(graph, segments)
    rtl_observed, instr, status = _run_on_rtl(
        fixtures_dir, iverilog_command, vvp_command, verilog, segments
    )

    assert rtl_observed == python_observed
    tms_reset_start = len(to_cycles(segments[0][1]))
    assert instr[tms_reset_start] == OPCODE_EXTEST  # not vacuous: EXTEST was loaded going in
    assert instr[tms_reset_start + len(_TMS_RESET)] == OPCODE_IDCODE
    assert python_observed[-1] != 0  # the SIBs opened before TLR are still open
    assert set(status) == {0}
