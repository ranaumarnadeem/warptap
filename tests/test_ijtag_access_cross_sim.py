"""IJTAG_ACCESS (``insert_sib_network``'s ``ijtag_access_opcode``) on real inserted RTL.

The network moves only under IJTAG_ACCESS. EXTEST, SAMPLE and PRELOAD select the 1-bit BYPASS
register (there is no boundary register), so a board-level EXTEST and its DR scans leave the
network, and the signal a WRITE instrument drives, untouched. Same design and checks as
``test_sib_insert_extest_select_cross_sim.py``: real_signal.v with a WRITE instrument driving
``ctrl_in`` (``status_out`` shows any commit one clock later), against TapModel(ijtag_access_opcode)
with the network registered under IJTAG_ACCESS; each run ends with an IJTAG_ACCESS readout scan,
so the network's state is compared too. A random walk then compares every TCK cycle.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest
from test_sib_insert_extest_select_cross_sim import (
    _ALL_ONES,
    _SPECS,
    _TMS_RESET,
    _dr_scan,
    _run_on_rtl,
    _select,
)

from warptap.netlist import Netlist
from warptap.pipeline import insert_test_access
from warptap.sib_insert import SibInsertError, insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sib_plan import build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir_play import play, to_cycles
from warptap.tap_model import (
    OPCODE_EXTEST,
    OPCODE_IDCODE,
    OPCODE_IJTAG_ACCESS,
    OPCODE_SAMPLE_PRELOAD,
    TapModel,
    bypass_opcode,
)
from warptap.yosys_io import ingest, write_verilog_from_json

IR_WIDTH = 4
_BYPASS = bypass_opcode(IR_WIDTH)
_READOUT = _select(OPCODE_IJTAG_ACCESS) + _dr_scan(0)
_RESET_LEAD_IN = [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 1, 1)]  # tb_real_signal.v rows


def _netlist(fixtures_dir, yosys_command, **options) -> tuple[Netlist, object]:
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "real_signal", graph, yosys_command=yosys_command, **options)
    return netlist, graph


@pytest.fixture(scope="module")
def inserted(fixtures_dir, yosys_command):
    netlist, graph = _netlist(fixtures_dir, yosys_command, ijtag_access_opcode=OPCODE_IJTAG_ACCESS)
    return graph, write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)


def _model(graph) -> TapModel:
    model = TapModel(has_idcode=True, ijtag_access_opcode=OPCODE_IJTAG_ACCESS)
    reg = SibNetworkRegister(graph)
    model.register_data_register(model.network_instruction, reg)
    model.reset()
    reg.reset()
    model.tick(0, 0)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE, as the lead-in's last row
    return model


def _run_on_python(graph, segments) -> list[int]:
    """One observed TDO int per ShiftIR/ShiftDR op, as _run_on_rtl returns them."""
    model = _model(graph)
    observed: list[int] = []
    for kind, body in segments:
        if kind == "ops":
            observed += play(model, body)
        else:
            for tms, tdi in body:
                model.tick(tms, tdi)
    return observed


def _rtl(fixtures_dir, iverilog_command, vvp_command, verilog, segments):
    return _run_on_rtl(fixtures_dir, iverilog_command, vvp_command, verilog, segments)


@pytest.mark.parametrize(
    "loaded",
    [_select(OPCODE_EXTEST), _select(OPCODE_SAMPLE_PRELOAD), [], _select(_BYPASS)],
    ids=["extest", "sample_preload", "idcode_after_reset", "bypass"],
)
def test_dr_scans_under_board_instructions_leave_the_network_untouched(
    loaded, inserted, fixtures_dir, iverilog_command, vvp_command
):
    """Two all-ones DR scans would open both SIBs and commit 1 into ctrl_write under the
    network's instruction; under EXTEST, SAMPLE/PRELOAD, IDCODE or BYPASS they do nothing."""
    graph, verilog = inserted
    segments = [("ops", loaded + _dr_scan(_ALL_ONES) + _dr_scan(_ALL_ONES) + _READOUT)]

    python_observed = _run_on_python(graph, segments)
    rtl_observed, instr, status = _rtl(fixtures_dir, iverilog_command, vvp_command, verilog, segments)

    assert rtl_observed == python_observed
    assert python_observed[-1] == 0  # both SIBs still closed, both captured 0
    assert set(status) == {0}  # ctrl_write never committed
    if loaded:
        assert instr[len(to_cycles(loaded))] == _BYPASS  # Update-IR loaded BYPASS


@pytest.mark.parametrize("opcode", [OPCODE_EXTEST, OPCODE_SAMPLE_PRELOAD])
def test_extest_and_sample_preload_shift_through_one_bypass_bit(
    opcode, inserted, fixtures_dir, iverilog_command, vvp_command
):
    graph, verilog = inserted
    pattern = 0b1011_0010
    segments = [("ops", _select(opcode) + _dr_scan(pattern))]

    python_observed = _run_on_python(graph, segments)
    rtl_observed, instr, _status = _rtl(
        fixtures_dir, iverilog_command, vvp_command, verilog, segments
    )

    assert rtl_observed == python_observed
    assert rtl_observed[-1] == (pattern << 1) & 0xFF  # captured 0, then one bit of delay
    assert instr[len(to_cycles(_select(opcode)))] == _BYPASS  # Update-IR loaded BYPASS


def test_the_same_scans_under_ijtag_access_open_and_commit(
    inserted, fixtures_dir, iverilog_command, vvp_command
):
    """Positive control: the gate is on IJTAG_ACCESS, not stuck off."""
    graph, verilog = inserted
    ijtag = _select(OPCODE_IJTAG_ACCESS)
    segments = [("ops", ijtag + _dr_scan(_ALL_ONES) + _dr_scan(_ALL_ONES) + _READOUT)]

    python_observed = _run_on_python(graph, segments)
    rtl_observed, instr, status = _rtl(
        fixtures_dir, iverilog_command, vvp_command, verilog, segments
    )

    assert rtl_observed == python_observed
    assert instr[len(to_cycles(ijtag))] == OPCODE_IJTAG_ACCESS
    assert python_observed[-1] != 0  # the readout sees the opened network
    assert status[-1] == 1  # ctrl_write committed 1 into ctrl_in


def test_test_logic_reset_deselects_ijtag_access(
    inserted, fixtures_dir, iverilog_command, vvp_command
):
    graph, verilog = inserted
    segments = [
        ("ops", _select(OPCODE_IJTAG_ACCESS) + _dr_scan(_ALL_ONES)),  # opens both SIBs
        ("raw", _TMS_RESET),
        ("ops", _dr_scan(_ALL_ONES) + _READOUT),
    ]

    python_observed = _run_on_python(graph, segments)
    rtl_observed, instr, status = _rtl(
        fixtures_dir, iverilog_command, vvp_command, verilog, segments
    )

    assert rtl_observed == python_observed
    tms_reset_start = len(to_cycles(segments[0][1]))
    assert instr[tms_reset_start] == OPCODE_IJTAG_ACCESS
    assert instr[tms_reset_start + len(_TMS_RESET)] == OPCODE_IDCODE
    assert python_observed[-1] != 0  # the SIBs opened before TLR are still open
    assert set(status) == {0}


def _walk(seed: int, cycles: int) -> list[tuple[int, int]]:
    """A random TMS/TDI walk from Run-Test/Idle. Each visit to Shift-IR shifts a whole opcode
    and leaves on its last bit, so Update-IR loads a chosen one: IJTAG_ACCESS half the time,
    else EXTEST, SAMPLE/PRELOAD, IDCODE, BYPASS or any opcode at all. TMS=1 is one cycle in
    ten in Shift-DR (DR scans long enough to move the network) and in Select-IR-Scan, one in
    five in Test-Logic-Reset (whose reload of IDCODE would otherwise undo most loads), and one
    in two elsewhere."""
    tms_odds = {
        TapState.SHIFT_DR: 0.1,
        TapState.SELECT_IR_SCAN: 0.1,
        TapState.TEST_LOGIC_RESET: 0.2,
    }
    rng = random.Random(seed)
    state, rows, forced = TapState.RUN_TEST_IDLE, [], []
    while len(rows) < cycles:
        if state is TapState.SHIFT_IR and not forced:
            board = [OPCODE_EXTEST, OPCODE_SAMPLE_PRELOAD, OPCODE_IDCODE, _BYPASS]
            opcode = rng.choice([OPCODE_IJTAG_ACCESS] * 5 + board + [rng.randrange(16)])
            forced = [(int(k == IR_WIDTH - 1), (opcode >> k) & 1) for k in range(IR_WIDTH)]
        if forced:
            tms, tdi = forced.pop(0)
        else:
            tms, tdi = int(rng.random() < tms_odds.get(state, 0.5)), rng.randint(0, 1)
        rows.append((tms, tdi))
        state = next_state(state, tms)
    return rows


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_random_walk_matches_the_model_every_cycle(
    seed, inserted, fixtures_dir, iverilog_command, vvp_command
):
    """State, instruction, strobes and TDO on every TCK cycle, IJTAG_ACCESS scans (moving the
    network) mixed with scans under every other instruction."""
    graph, verilog = inserted
    walk = _walk(seed, 600)
    model = _model(graph)
    expected = []
    for tms, tdi in walk:
        state = model.state
        row = (
            state.value,
            model.instruction_opcode(),
            int(state is TapState.CAPTURE_DR),
            int(state is TapState.SHIFT_DR),
            int(state is TapState.UPDATE_DR),
        )
        expected.append((*row, model.tick(tms, tdi)))

    rows = _RESET_LEAD_IN + [(tms, tdi, 1, 1) for tms, tdi in walk]
    with tempfile.TemporaryDirectory(prefix="warptap-ijtag-access-walk-") as tmpdir:
        path = Path(tmpdir) / "real_signal_inserted.v"
        path.write_text(verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [path, fixtures_dir / "tb_real_signal.v"],
            extra_inputs={"stimulus.txt": "\n".join(" ".join(map(str, r)) for r in rows) + "\n"},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
    trace = [line.split(",")[1:7] for line in stdout.splitlines() if line.startswith("TRACE,")]
    observed = [tuple(int(v) for v in t) for t in trace[len(_RESET_LEAD_IN):]]

    assert observed == expected
    loaded = [row[1] for row in expected]
    assert OPCODE_IJTAG_ACCESS in loaded and _BYPASS in loaded  # both kinds really ran
    under_ijtag = [row for row in expected if row[1] == OPCODE_IJTAG_ACCESS]
    assert sum(row[3] for row in under_ijtag) > 50  # the network shifted ...
    assert sum(row[4] for row in under_ijtag) >= 3  # ... and updated


def test_only_the_decode_and_tap_core_change(fixtures_dir, yosys_command):
    """The decode compares with IJTAG_ACCESS's opcode, under its own name; without the option
    it is the EXTEST decode it always was."""
    netlist, _ = _netlist(fixtures_dir, yosys_command, ijtag_access_opcode=OPCODE_IJTAG_ACCESS)
    cells = netlist.to_json()["modules"]["real_signal"]["cells"]
    assert "warptap_ijtag_extest_decode" not in cells
    assert cells["warptap_ijtag_access_decode"]["connections"]["B"] == ["0", "0", "1", "1"]

    netlist, _ = _netlist(fixtures_dir, yosys_command)
    cells = netlist.to_json()["modules"]["real_signal"]["cells"]
    assert "warptap_ijtag_access_decode" not in cells
    assert cells["warptap_ijtag_extest_decode"]["connections"]["B"] == ["0", "0", "0", "0"]


@pytest.mark.parametrize("opcode", [OPCODE_EXTEST, OPCODE_IDCODE, _BYPASS, 0b10000])
def test_an_opcode_no_tap_can_give_ijtag_access_is_refused(opcode, fixtures_dir, yosys_command):
    with pytest.raises(SibInsertError, match="IJTAG_ACCESS opcode"):
        _netlist(fixtures_dir, yosys_command, ijtag_access_opcode=opcode)


def test_insert_test_access_refuses_it_before_ingesting(fixtures_dir):
    with pytest.raises(SibInsertError, match="SAMPLE/PRELOAD"):
        insert_test_access(
            [fixtures_dir / "real_signal.v"], "real_signal", _SPECS,
            ijtag_access_opcode=OPCODE_SAMPLE_PRELOAD,
            yosys_command="no-such-yosys",
        )


def test_insert_test_access_passes_the_opcode_through(fixtures_dir, yosys_command):
    verilog, _graph, _root = insert_test_access(
        [fixtures_dir / "real_signal.v"], "real_signal", _SPECS,
        ijtag_access_opcode=OPCODE_IJTAG_ACCESS, yosys_command=yosys_command,
    )
    assert "warptap_current_instruction == 4'hc" in verilog
