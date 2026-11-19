"""Gate-level evidence on IHP SG13G2: warptap-inserted designs mapped to the library's standard
cells and simulated with its own cell models (``gate_level.py`` has the flow, and what it does
and does not show).

Each design's RTL cross-simulation reruns unchanged on the mapped netlist. The RTL test's own
function runs twice: once as it is, recording what each simulation prints, and once with its
module's two simulation hooks swapped, ``write_verilog_from_json`` for the mapped netlist and
``run_verilog_testbench`` for the same run plus the cell models. So the RTL test's own stimulus
and assertions judge the gate-level run, and the two runs' traces must match row for row. Two
differences are allowed, each where the RTL's semantics are not a circuit's, and each only in the
test that hits it: in the openMBIST design's reset lead-in, an unreset register makes the RTL's
``case`` read 0 where the mapped mux reads ``x`` (the TAP and TDO columns match there too); and in
``trivial_bidir``, a register that samples the floating ``io`` pin holds ``z`` in the RTL, which a
Verilog ``reg`` can, and ``x`` at gate level, which is what a flip-flop holds.

- The TDO pin of a SIB network and of a boundary-scan register, tri-stated by a ``$tribuf``
  mapped to ``sg13g2_ebufn_2``, against the TDO cross-simulation's rules: a bit in the shift
  states, released elsewhere, moving only at falling edges.
- The boundary-scan register's EXTEST path (its output3 pins on ``sg13g2_ebufn_2``), and an inout
  pin's BC_7 cell over the design's own tri-state driver.
- alexforencich/verilog-uart's ``uart_tx`` with three instruments: the byte written through JTAG
  comes out of ``txd``.
- openMBIST's ``mem_subsystem_mbist`` with its eight instruments; its three SRAM macros stay
  macros.

Control: the same flow with the tri-state buffer's enable not inverted fails the TDO rules.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

import pytest

import gate_level as gl
import test_bsr_insert_bidir as bidir
import test_bsr_insert_cross_sim as bsr
import test_mem_subsystem_mbist_pdl_verify as mbist
import test_tdo_drive_cross_sim as tdo
import test_uart_tx_cross_sim as uart
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.yosys_io import write_verilog_from_json

SRAM_MACROS = ("sky130_sram_32b256w", "sky130_sram_32b512w", "sky130_sram_8b1024w")


@pytest.fixture(scope="module")
def sg13g2() -> gl.Sg13g2:
    lib, why = gl.find_sg13g2()
    if lib is None:
        pytest.skip(why)
    return lib


def _top_of(netlist_json: dict) -> str:
    tops = [
        name for name, module in netlist_json["modules"].items()
        if int(str(module.get("attributes", {}).get("top", "0")), 2)
    ]
    assert len(tops) == 1, tops
    return tops[0]


class _Hooks:
    """Swap an RTL cross-simulation module's two simulation hooks and record what each run
    prints. Without ``lib`` the run is the RTL one, only recorded."""

    def __init__(
        self,
        monkeypatch,
        module,
        lib: Optional[gl.Sg13g2] = None,
        *,
        macros: Sequence[str] = (),
        tbuf_techmap: str = gl.TBUF_TECHMAP,
    ):
        self.outputs: List[str] = []
        self.mapped: List[gl.Mapped] = []

        def emit(netlist_json, *, yosys_command=None):
            if lib is None:
                return write_verilog_from_json(netlist_json, yosys_command=yosys_command)
            mapped = gl.map_to_sg13g2(
                netlist_json, _top_of(netlist_json), lib, macros=macros,
                tbuf_techmap=tbuf_techmap, yosys_command=yosys_command,
            )
            gl.check_mapped(mapped, macros, lib)
            self.mapped.append(mapped)
            return gl.simulation_verilog(mapped)

        def run(verilog_files, **kwargs):
            if lib is not None:
                verilog_files = [*verilog_files, *lib.models]
            out = run_verilog_testbench(verilog_files, **kwargs)
            self.outputs.append(out)
            return out

        monkeypatch.setattr(module, "write_verilog_from_json", emit)
        monkeypatch.setattr(module, "run_verilog_testbench", run)


def _rows(stdout: str) -> List[str]:
    return [line for line in stdout.splitlines() if line.startswith("TRACE,")]


# Every fixture testbench prints the TAP state, instruction, capture/shift/update and tdo first.
_TAP_COLUMNS = 7


def _same_value(rtl: str, gate: str, floating_samples: bool) -> bool:
    """With ``floating_samples``, a register that sampled a floating pin holds ``z`` in the RTL,
    which a Verilog ``reg`` can, and ``x`` at gate level, which is what a flip-flop holds."""
    return rtl == gate or (floating_samples and rtl == "z" and gate == "x")


def _assert_same_rows(a: List[str], b: List[str], *, lead_in: int = 0, floating_samples: bool = False) -> None:
    assert a and len(a) == len(b), (len(a), len(b))
    for i, (x, y) in enumerate(zip(a, b)):
        cols_x, cols_y = x.split(","), y.split(",")
        if all(_same_value(p, q, floating_samples) for p, q in zip(cols_x, cols_y)):
            continue
        assert i < lead_in, f"row {i} differs:\n  rtl  {x}\n  gate {y}"
        assert cols_x[:_TAP_COLUMNS] == cols_y[:_TAP_COLUMNS], (x, y)


def _assert_same_traces(rtl: _Hooks, gate: _Hooks, **how) -> None:
    assert gate.mapped, "nothing was mapped"
    assert len(rtl.outputs) == len(gate.outputs) > 0
    for rtl_out, gate_out in zip(rtl.outputs, gate.outputs):
        _assert_same_rows(_rows(rtl_out), _rows(gate_out), **how)


def _run_rtl_and_gate(monkeypatch, module, lib, test, args_for, *, macros=(), **how) -> _Hooks:
    """Run the RTL test ``test`` as it is, then at gate level; ``args_for("rtl" | "gate")``
    gives each run's arguments (so each can have a directory of its own)."""
    rtl = _Hooks(monkeypatch, module)
    test(*args_for("rtl"))
    gate = _Hooks(monkeypatch, module, lib, macros=macros)
    test(*args_for("gate"))
    _assert_same_traces(rtl, gate, **how)
    return gate


def _subdir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


# -- the TDO pin ------------------------------------------------------------------------------


def _tdo_samples(insertion, lib, fixtures_dir, yosys_command, iverilog_command, vvp_command,
                 tbuf_techmap=gl.TBUF_TECHMAP):
    """The TDO cross-simulation's run (its stimulus, testbench and sample parse), RTL and gate
    level, from one inserted netlist."""
    netlist, top = tdo._INSERTIONS[insertion](fixtures_dir, yosys_command, "tristate")
    netlist_json = netlist.to_json()
    stimulus = {"stimulus.txt": "\n".join(" ".join(map(str, r)) for r in tdo._stimulus()) + "\n"}
    bench_text = tdo._testbench(top, netlist_json["modules"][top]["ports"], tdo._ENABLE["tristate"])
    mapped = gl.map_to_sg13g2(netlist_json, top, lib, tbuf_techmap=tbuf_techmap, yosys_command=yosys_command)
    gl.check_mapped(mapped, lib=lib)
    with tempfile.TemporaryDirectory(prefix="warptap-sg13g2-tdo-") as tmpdir:
        bench = Path(tmpdir) / "tb_tdo_drive.v"
        bench.write_text(bench_text, encoding="utf-8")
        design = Path(tmpdir) / f"{top}_inserted.v"
        design.write_text(write_verilog_from_json(netlist_json, yosys_command=yosys_command), encoding="utf-8")
        tools = {"iverilog_command": iverilog_command, "vvp_command": vvp_command}
        rtl = run_verilog_testbench([design, bench], extra_inputs=stimulus, **tools)
        gate = gl.run_gate_level(mapped, lib, [bench], extra_inputs=stimulus, **tools)

    def samples(stdout):
        out = []
        for line in _rows(stdout):
            _, state, *values = line.split(",")
            out.append(tdo.Sample(TapState(int(state)), tuple(values[:3]), tuple(values[3:])))
        assert len(out) == len(tdo._stimulus())
        return out

    return samples(rtl), samples(gate), mapped


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_the_tristated_tdo_pin_follows_ieee_1149_1_at_gate_level(
    insertion, sg13g2, fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    rtl, gate, mapped = _tdo_samples(
        insertion, sg13g2, fixtures_dir, yosys_command, iverilog_command, vvp_command
    )
    assert mapped.total(gl.TRISTATE_CELL) == mapped.tristate_drivers >= 1
    tdo._check_tristate(gate)
    tdo._check_enable(gate)
    edges = {(s.tdo[1], s.tdo[2]) for s in gate}
    assert edges & {("z", "0"), ("z", "1")}, "never driven again at a falling edge"
    assert edges & {("0", "z"), ("1", "z")}, "never released at a falling edge"
    assert gate == rtl


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_control_a_tristate_buffer_mapped_with_its_enable_uninverted_fails(
    insertion, sg13g2, fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    rtl, gate, _ = _tdo_samples(
        insertion, sg13g2, fixtures_dir, yosys_command, iverilog_command, vvp_command,
        tbuf_techmap=gl.TBUF_TECHMAP_WRONG_POLARITY,
    )
    with pytest.raises(AssertionError, match="not released"):
        tdo._check_tristate(gate)
    assert gate != rtl


# -- the boundary-scan register -----------------------------------------------------------------


def test_bsr_extest_shift_matches_the_python_model_at_gate_level(
    sg13g2, monkeypatch, fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    gate = _run_rtl_and_gate(
        monkeypatch, bsr, sg13g2, bsr.test_extest_shift_through_real_chain_matches_python_model,
        lambda _: (fixtures_dir, yosys_command, iverilog_command, vvp_command),
    )
    (mapped,) = gate.mapped
    assert mapped.total(gl.TRISTATE_CELL) == mapped.tristate_drivers >= 2  # tdo and output3 y


def test_bidir_bc7_cell_drives_the_shifted_value_at_gate_level(
    sg13g2, monkeypatch, fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """``trivial_bidir`` registers its ``io`` pin (``sensed``), and its BC_7 cell captures it:
    while the pin floats, the RTL stores ``z`` in both and the mapped flip-flops ``x``."""
    gate = _run_rtl_and_gate(
        monkeypatch, bidir, sg13g2,
        bidir.test_extest_shift_and_update_drives_io_to_the_shifted_value,
        lambda _: (fixtures_dir, yosys_command, iverilog_command, vvp_command),
        floating_samples=True,
    )
    (mapped,) = gate.mapped
    # tdo, the design's own driver of io, and the output3 pin sensed
    assert mapped.total(gl.TRISTATE_CELL) == mapped.tristate_drivers >= 3


def test_bidir_normal_mode_matches_the_original_design_at_gate_level(
    sg13g2, monkeypatch, fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """``test_bidir_insertion_preserves_normal_mode_behavior``'s scenario and comparison, at gate
    level. That test compares ``sensed`` with the original design's RTL, which stores ``z`` from
    the floating pin where a flip-flop stores ``x``; ``io`` itself, the pin, must match exactly."""
    gate = _Hooks(monkeypatch, bidir, sg13g2)
    netlist, _plan = bidir._build_and_insert(fixtures_dir, yosys_command)
    oe_val_pairs = [(0, 0), (1, 0), (1, 1), (0, 1), (1, 1), (0, 0), (1, 1)]
    baseline = bidir._run_baseline(fixtures_dir, iverilog_command, vvp_command, [(0, 0)] * 2 + oe_val_pairs)
    rows = [(0, 0, 0, 1, 0, 0)] * 2 + [(0, 0, 1, 1, oe, val) for oe, val in oe_val_pairs]
    inserted = bidir._run_rtl(fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, rows)
    assert gate.mapped
    assert [r[6] for r in inserted] == [b[1] for b in baseline]  # io
    assert all(
        _same_value(str(b[0]), str(r[7]), floating_samples=True) for r, b in zip(inserted, baseline)
    ), ([r[7] for r in inserted], [b[0] for b in baseline])  # sensed
    assert {r[6] for r in inserted} >= {"0", "1", "z"}  # driven both ways and released


# -- real external designs ----------------------------------------------------------------------


def test_uart_tx_transmits_the_byte_written_through_jtag_at_gate_level(
    sg13g2, monkeypatch, verilog_uart_dir, yosys_command, iverilog_command, vvp_command, tmp_path
):
    _run_rtl_and_gate(
        monkeypatch, uart, sg13g2, uart.test_uart_tx_transmits_the_written_byte_on_real_rtl,
        lambda run: (verilog_uart_dir, yosys_command, iverilog_command, vvp_command,
                     _subdir(tmp_path, run)),
    )


def test_mem_subsystem_mbist_self_repair_access_at_gate_level(
    sg13g2, monkeypatch, openmbist_dir, autombist_generator, yosys_command, iverilog_command,
    vvp_command, fixtures_dir, tmp_path,
):
    """The first row of the reset lead-in differs in ``rdata`` alone: openMBIST's read mux
    selects on ``sel_q``, a register with no reset, which is ``x`` until the first clock edge.
    The RTL's ``case`` matches none of its items and takes ``default: rdata = '0``; the mapped
    mux passes the ``x`` through."""
    gate = _run_rtl_and_gate(
        monkeypatch, mbist, sg13g2,
        mbist.test_self_repair_start_write_and_busy_read_on_real_mem_subsystem_mbist,
        lambda run: (openmbist_dir, autombist_generator, yosys_command, iverilog_command,
                     vvp_command, fixtures_dir, _subdir(tmp_path, run)),
        macros=SRAM_MACROS, lead_in=len(mbist._RESET_LEAD_IN),
    )
    (mapped,) = gate.mapped
    assert set(SRAM_MACROS) <= {t for counts in mapped.cells.values() for t in counts}
    assert all(f"module {name}" in mapped.macros_verilog for name in SRAM_MACROS)
