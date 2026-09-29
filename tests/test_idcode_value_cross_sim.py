"""A non-default IDCODE on real inserted RTL, under Icarus.

``insert_test_access(..., idcode_value=V)`` must bake ``V`` into the inserted ``tap_core``: after
TRST the TAP holds IDCODE, and a 32-bit DR scan shifts ``V`` out of TDO. The BSDL written with the
same value must declare exactly what the RTL shifted out, and an integrity program built for
``V`` must pass on that RTL while one built for the default value fails at its first IDCODE check.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bsdl_reader import parse_bsdl

from warptap.bsdl_emit import to_bsdl
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.pipeline import insert_test_access
from warptap.sib_plan import InstrumentSpec
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_integrity import TapConfig, build_integrity_program, check_integrity
from warptap.tap_ir import GotoState, ShiftDR, bits_to_int
from warptap.tap_ir_play import shift_op_ranges, to_cycles
from warptap.tap_model import IDCODE_VALUE

_CUSTOM = 0x5CA1AB1F
_SPECS = [
    InstrumentSpec(
        "ctrl_write", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("ctrl_in", 0),),
    ),
    InstrumentSpec(
        "status_read", width=1, capture_value=0,
        signal_bits=(SignalBinding("status_out", 0),),
    ),
]


@pytest.fixture(scope="module")
def custom(fixtures_dir, yosys_command):
    return insert_test_access(
        [fixtures_dir / "real_signal.v"], "real_signal", _SPECS,
        idcode_value=_CUSTOM, yosys_command=yosys_command,
    )


def _tdo(verilog, rows, fixtures_dir, iverilog_command, vvp_command) -> list[int]:
    """TDO per ``(tms, tdi, trst_n)`` row, sampled before the rising edge
    (``tb_real_signal.v``; the design's own reset follows TRST)."""
    lines = [f"{tms} {tdi} {trst_n} {trst_n}" for tms, tdi, trst_n in rows]
    with tempfile.TemporaryDirectory(prefix="warptap-idcode-") as tmpdir:
        path = Path(tmpdir) / "real_signal_inserted.v"
        path.write_text(verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_real_signal.v"],
            extra_inputs={"stimulus.txt": "\n".join(lines) + "\n"},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
    tdo = [line.split(",")[6] for line in stdout.splitlines() if line.startswith("TRACE,")]
    assert len(tdo) == len(rows)
    return [int(v) if v in ("0", "1") else -1 for v in tdo]


def _read_idcode_after_trst(verilog, fixtures_dir, iverilog_command, vvp_command) -> int:
    lead_in = [(0, 0, 0), (0, 0, 1)]  # TRST, then one TMS=0 edge into Run-Test/Idle
    ops = [GotoState(TapState.SHIFT_DR), ShiftDR(32, 0)]
    rows = lead_in + [(tms, tdi, 1) for tms, tdi in to_cycles(ops)]
    tdo = _tdo(verilog, rows, fixtures_dir, iverilog_command, vvp_command)
    ((start, bits),) = shift_op_ranges(ops)
    return bits_to_int(tdo[len(lead_in) + start : len(lead_in) + start + bits])


def test_the_inserted_tap_shifts_out_the_custom_idcode_and_the_bsdl_declares_it(
    custom, fixtures_dir, iverilog_command, vvp_command
):
    verilog, _graph, root = custom
    observed = _read_idcode_after_trst(verilog, fixtures_dir, iverilog_command, vvp_command)
    assert observed == _CUSTOM

    bsdl = parse_bsdl(to_bsdl(root.name, tck_max_freq_hz=10e6, idcode_value=_CUSTOM))
    assert int(bsdl.idcode_register, 2) == observed


def test_the_default_bsdl_would_misdescribe_the_custom_rtl(
    custom, fixtures_dir, iverilog_command, vvp_command
):
    """Control: the read-back above isn't trivially equal to whatever the BSDL says."""
    verilog, _graph, root = custom
    observed = _read_idcode_after_trst(verilog, fixtures_dir, iverilog_command, vvp_command)
    default_bsdl = parse_bsdl(to_bsdl(root.name, tck_max_freq_hz=10e6))
    assert int(default_bsdl.idcode_register, 2) == IDCODE_VALUE != observed


@pytest.mark.parametrize(
    ("idcode_value", "passes"), [(_CUSTOM, True), (IDCODE_VALUE, False)], ids=["custom", "default"]
)
def test_the_integrity_program_follows_the_configured_idcode(
    idcode_value, passes, custom, fixtures_dir, iverilog_command, vvp_command
):
    verilog, graph, root = custom
    program = build_integrity_program(
        graph, root, tap=TapConfig(idcode_value=idcode_value), live=["status_read"]
    )
    rows = [(c.tms, c.tdi, c.trst_n) for c in program.cycles]
    result = check_integrity(
        program, _tdo(verilog, rows, fixtures_dir, iverilog_command, vvp_command)
    )
    assert result.passed is passes
    if not passes:
        assert result.failures[0].test == "reset_instruction"
