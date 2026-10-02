"""chip_reset: every WRITE instrument also clears on the chip reset, through a TCK-domain
reset synchronizer (rtl/tck_reset_sync.v -> rtl/instrument_write_clr.v), so the signals it
drives come up deasserted after a chip reset even when TRST never pulsed. Off by default."""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest
from test_sib_insert_write_instrument_cross_sim import (
    _RESET_LEAD_IN,
    _pdl_program,
    _select_extest_ops,
    run_on_python,
    run_on_rtl,
)

import warptap
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_insert import SibInsertError, insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_ir_play import to_cycles
from warptap.yosys_io import ingest, write_verilog_from_json

_RTL_DIR = Path(warptap.__file__).resolve().parent / "rtl"

_SPECS = [
    InstrumentSpec(
        "ctrl_write", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("ctrl_in", 0),),
    ),
    InstrumentSpec(
        "status_read", width=1, capture_value=1,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("status_out", 0),),
    ),
]


def _render(rows) -> str:
    return "\n".join(" ".join(str(v) for v in row) for row in rows) + "\n"


# --- the cells, against Python references ------------------------------------------------


class _ResetSyncRef:
    def __init__(self) -> None:
        self.stages = [0, 0]

    def tick(self, *, trst_n: int, chip_rst_n: int) -> int:
        if not (trst_n and chip_rst_n):
            self.stages = [0, 0]
        clr_n = self.stages[1]
        if trst_n and chip_rst_n:
            self.stages = [1, self.stages[0]]
        return clr_n


class _WriteClrRef:
    def __init__(self) -> None:
        self.shift_ff = 0
        self.po = 0

    def tick(self, *, trst_n, clr_n, si, select, capture_dr, shift_dr, update_dr):
        if not (trst_n and clr_n):
            self.shift_ff = self.po = 0
        out = (self.shift_ff, self.po)
        if trst_n and clr_n:
            shift_ff, po = self.shift_ff, self.po
            if capture_dr:
                shift_ff = self.po
            elif shift_dr:
                shift_ff = si
            if update_dr and select:
                po = self.shift_ff
            self.shift_ff, self.po = shift_ff, po
        return out


def _cell_trace(cell: str, tb: str, rows, fixtures_dir, iverilog_command, vvp_command):
    stdout = run_verilog_testbench(
        [_RTL_DIR / f"{cell}.v", fixtures_dir / tb],
        extra_inputs={"stimulus.txt": _render(rows)},
        iverilog_command=iverilog_command,
        vvp_command=vvp_command,
    )
    return [
        tuple(int(v) for v in line.split(",")[1:])
        for line in stdout.splitlines()
        if line.startswith("TRACE,")
    ]


def test_the_reset_synchronizer_releases_two_edges_late(
    fixtures_dir, iverilog_command, vvp_command
):
    rng = random.Random(1001)
    rows = [(0, 0), (1, 0), (1, 1), (1, 1), (1, 1), (0, 1), (1, 1), (1, 1), (1, 1)]
    rows += [(int(rng.random() > 0.05), int(rng.random() > 0.1)) for _ in range(300)]
    ref = _ResetSyncRef()
    expected = [(ref.tick(trst_n=t, chip_rst_n=c),) for t, c in rows]
    rtl = _cell_trace(
        "tck_reset_sync", "tb_tck_reset_sync.v", rows,
        fixtures_dir, iverilog_command, vvp_command,
    )
    assert rtl == expected
    # Released on the third row's and fourth row's edges: clr_n reads 1 on the fifth.
    assert [c for (c,) in rtl[:5]] == [0, 0, 0, 0, 1]


def test_the_write_cell_clears_on_either_reset(fixtures_dir, iverilog_command, vvp_command):
    rng = random.Random(2002)
    rows = [(0, 0, 0, 0, 0, 0, 0)]
    for _ in range(400):
        strobe = rng.choice(("capture", "shift", "update", "none"))
        rows.append(
            (
                int(rng.random() > 0.03),
                int(rng.random() > 0.05),
                rng.randint(0, 1),
                rng.randint(0, 1),
                int(strobe == "capture"),
                int(strobe == "shift"),
                int(strobe == "update"),
            )
        )
    ref = _WriteClrRef()
    columns = ("trst_n", "clr_n", "si", "select", "capture_dr", "shift_dr", "update_dr")
    expected = [ref.tick(**dict(zip(columns, row))) for row in rows]
    rtl = _cell_trace(
        "instrument_write_clr", "tb_instrument_write_clr.v", rows,
        fixtures_dir, iverilog_command, vvp_command,
    )
    assert rtl == expected


# --- inserted into a design -----------------------------------------------------------------


def _inserted(fixtures_dir, yosys_command, **options):
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(_SPECS, top_name="real_signal")
    insert_sib_network(netlist, "real_signal", graph, yosys_command=yosys_command, **options)
    return netlist, graph, root


def _status(netlist, rows, fixtures_dir, yosys_command, iverilog_command, vvp_command):
    """status_out on every row: real_signal's ctrl_in, one clock late."""
    verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    with tempfile.TemporaryDirectory(prefix="warptap-chip-reset-") as tmpdir:
        path = Path(tmpdir) / "real_signal_inserted.v"
        path.write_text(verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_real_signal.v"],
            extra_inputs={"stimulus.txt": _render(rows)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
    return [line.split(",")[-1] for line in stdout.splitlines() if line.startswith("TRACE,")]


def _write_one_then_pulse_the_chip_reset(graph, root) -> list[tuple[int, ...]]:
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    pdl.iApply()
    rows = list(_RESET_LEAD_IN)
    rows += [(tms, tdi, 1, 1) for tms, tdi in to_cycles(_select_extest_ops() + pdl.program)]
    rows += [(0, 0, 1, 1)] * 4  # status_out follows ctrl_in = 1
    rows += [(0, 0, 1, 0)] * 2  # the chip reset, TAP idle in Run-Test/Idle
    rows += [(0, 0, 1, 1)] * 6
    return rows


@pytest.mark.parametrize(("options", "after"), [({"chip_reset": "rst_n"}, "0"), ({}, "1")])
def test_a_chip_reset_clears_a_written_control_bit(
    fixtures_dir, yosys_command, iverilog_command, vvp_command, options, after
):
    """Written to 1 over JTAG, the control bit is 1; after a chip reset it is 0 with
    chip_reset, still 1 without."""
    netlist, graph, root = _inserted(fixtures_dir, yosys_command, **options)
    rows = _write_one_then_pulse_the_chip_reset(graph, root)
    status = _status(netlist, rows, fixtures_dir, yosys_command, iverilog_command, vvp_command)
    assert status[-9] == "1", status  # just before the chip reset
    assert status[-1] == after, status


@pytest.mark.parametrize(("options", "after"), [({"chip_reset": "rst_n"}, "0"), ({}, "x")])
def test_without_trst_a_chip_reset_still_clears_the_control_bits(
    fixtures_dir, yosys_command, iverilog_command, vvp_command, options, after
):
    """Power up with TRST never pulsed: a chip reset alone brings the control bit to 0;
    without chip_reset it stays unknown."""
    netlist, _, _ = _inserted(fixtures_dir, yosys_command, **options)
    rows = [(1, 0, 1, 0)] * 3 + [(1, 0, 1, 1)] * 6
    status = _status(netlist, rows, fixtures_dir, yosys_command, iverilog_command, vvp_command)
    assert status[-1] == after, status


def test_jtag_access_matches_the_model_with_the_clear(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    netlist, graph, root = _inserted(fixtures_dir, yosys_command, chip_reset="rst_n")
    ir_ops = _select_extest_ops() + _pdl_program(graph, root)
    python_observed, _ = run_on_python(graph, ir_ops)
    assert (
        run_on_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops)
        == python_observed
    )


def test_only_the_write_cells_change(fixtures_dir, yosys_command):
    netlist, _, _ = _inserted(fixtures_dir, yosys_command, chip_reset="rst_n")
    cells = netlist.to_json()["modules"]["real_signal"]["cells"]
    assert cells["warptap_sib_ctrl_write_inst_0"]["type"] == "instrument_write_clr"
    assert cells["warptap_sib_status_read_inst_0"]["type"] == "bc1_shift_only"
    assert cells["warptap_chip_reset_sync"]["type"] == "tck_reset_sync"


def test_off_by_default(fixtures_dir, yosys_command):
    netlist, _, _ = _inserted(fixtures_dir, yosys_command)
    modules = netlist.to_json()["modules"]
    assert modules["real_signal"]["cells"]["warptap_sib_ctrl_write_inst_0"]["type"] == (
        "instrument_write"
    )
    assert "instrument_write_clr" not in modules and "tck_reset_sync" not in modules


def test_an_active_high_chip_reset_is_inverted(fixtures_dir, yosys_command):
    netlist, _, _ = _inserted(
        fixtures_dir, yosys_command, chip_reset="rst_n", chip_reset_active_low=False
    )
    cells = netlist.to_json()["modules"]["real_signal"]["cells"]
    assert cells["warptap_chip_reset_invert"]["type"] == "$not"


@pytest.mark.parametrize("port", ["status_out", "no_such_port"])
def test_the_chip_reset_must_be_an_input(fixtures_dir, yosys_command, port):
    with pytest.raises(SibInsertError, match="not a single-bit input"):
        _inserted(fixtures_dir, yosys_command, chip_reset=port)
