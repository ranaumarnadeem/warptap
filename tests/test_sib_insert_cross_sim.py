"""RTL/Python cross-simulation for the full SIB network (implementation_plan.md §7 Stage 4,
testing layer 5): drives identical stimulus through the real inserted RTL (via Icarus) and
through TapModel + SibNetworkRegister (registered as EXTEST's data register, the same
extension point bsr_model.BoundaryScanRegister uses), asserting identical per-cycle traces.
Mirrors tests/test_bsr_insert_cross_sim.py's shape.

Directed scenario: open exactly one SIB (via a closed-network shift while every SIB is still
closed -- same "shift a pattern, Update-DR commits it" technique test_bsr_insert_bidir.py's
control-cell test established), then Capture-DR + Shift-DR the now-dynamically-longer network,
proving its instrument's fixed capture_value shifts out and no other slot's bits leak in.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_model import SibNetworkRegister
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_model import Instruction, TapModel
from warptap.yosys_io import ingest, write_verilog_from_json

OPCODE_EXTEST = 0
IR_WIDTH = 4

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]


def test_sib_network_register_captures_only_the_open_slots_instrument_bits():
    """Pure-Python direct-state check, no RTL involved: right after opening sensor_a's
    SIB and calling capture(), the live layout must be exactly
    [a_inst0, a_inst1, a_inst2, a_sib_self, b_sib_self] with sensor_a's bits equal to
    its configured capture_value and sensor_b's (closed) instrument bits absent from
    the live layout entirely -- checked via internal state, not by hand-deriving raw
    serial tdo timing, which is fragile and orthogonal to what this property claims."""
    graph, _root = build_sib_plan(_SPECS)
    reg = SibNetworkRegister(graph)

    sensor_a_slot = reg._slots[0]
    sensor_b_slot = reg._slots[1]
    assert sensor_a_slot.sib_name == "sib_sensor_a"
    assert sensor_b_slot.sib_name == "sib_sensor_b"

    sensor_a_slot.sib_po = 1  # simulate having already opened sensor_a's SIB
    reg.capture()

    assert sensor_a_slot.inst_shift_ff == [1, 0, 1]  # 0b101, bit0 first
    assert sensor_a_slot.sib_shift_ff == 1  # self-capture of po=1
    assert sensor_b_slot.sib_shift_ff == 0  # self-capture of po=0 (still closed)

    layout = reg._live_layout()
    assert [(ref.slot.sib_name, ref.bit) for ref in layout] == [
        ("sib_sensor_a", 0), ("sib_sensor_a", 1), ("sib_sensor_a", 2), ("sib_sensor_a", None),
        ("sib_sensor_b", None),
    ]


def _build_and_insert(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, "trivial", graph, yosys_command=yosys_command)
    return netlist, graph


def _navigate_and_select_extest() -> list[tuple[int, int]]:
    seq = [(1, 0), (1, 0), (0, 0), (0, 0)]
    for _ in range(IR_WIDTH - 1):
        seq.append((0, 0))  # OPCODE_EXTEST is all zero bits
    seq.append((1, 0))
    seq.append((1, 0))
    seq.append((0, 0))
    return seq


def _shift_dr(tdi_bits: list[int]) -> list[tuple[int, int]]:
    n = len(tdi_bits)
    seq = [(1, 0), (0, 0), (0, 0)]
    for i in range(n - 1):
        seq.append((0, tdi_bits[i]))
    seq.append((1, tdi_bits[n - 1]))
    seq.append((1, 0))
    seq.append((0, 0))
    return seq


def _directed_stimulus() -> list[tuple[int, int, int, int, int, int]]:
    """(tms, tdi, trst_n, rst_n, a, b) per cycle. Reset, select EXTEST, then a
    closed-network shift (chain length = 2: sib_sensor_a nearest TDI, sib_sensor_b
    next) that lands a 1 at sib_sensor_a's position -- feed [0, 1] so the LAST fed
    bit (1) lands at position 0 (same "last fed bit lands at cell0" convention every
    prior stage's directed tests use) -- Update-DR then commits po=1 for
    sib_sensor_a only. Then Capture-DR + 5 Shift-DR cycles on the now 5-bit-long
    open network (sensor_a's 3 instrument bits + its own self-capture bit +
    sensor_b's closed self-capture bit), deliberately WITHOUT ever exiting back
    through Update-DR again: that second Update-DR would re-commit every SIB's po
    from whatever its own shift_ff happens to hold after those 5 shifts (a real,
    correct property of the design -- both engines agree on it -- but not what this
    scenario is testing), which would silently close sib_sensor_a again since this
    stimulus feeds an all-zero tdi that doesn't happen to land back on 1."""
    tms_tdi: list[tuple[int, int]] = [(0, 0), (0, 0), (0, 0)]
    tms_tdi += _navigate_and_select_extest()
    tms_tdi += _shift_dr([0, 1])  # opens sib_sensor_a only
    tms_tdi += [(0, 0)] * 2
    tms_tdi += [(1, 0), (0, 0), (0, 0)]  # -> SELECT_DR_SCAN -> CAPTURE_DR -> SHIFT_DR
    tms_tdi += [(0, 0)] * 5  # 5 shifts, staying in SHIFT_DR throughout (no exit)

    stim = [(0, 0, 0, 0, 0, 0)]
    for tms, tdi in tms_tdi:
        stim.append((tms, tdi, 1, 1, 0, 0))
    return stim


def run_on_python(graph, stimulus) -> tuple[list[tuple], SibNetworkRegister]:
    model = TapModel(has_idcode=True)
    reg = SibNetworkRegister(graph)
    model.register_data_register(Instruction.EXTEST, reg)

    trace = []
    for tms, tdi, trst_n, _rst_n, _a, _b in stimulus:
        if not trst_n:
            model.reset()
            reg.reset()
            pre_state, pre_instr = TapState.TEST_LOGIC_RESET, model.instruction_opcode()
            capture_dr = shift_dr = update_dr = 0
            tdo = 0
        else:
            pre_state = model.state
            pre_instr = model.instruction_opcode()
            capture_dr = int(pre_state is TapState.CAPTURE_DR)
            shift_dr = int(pre_state is TapState.SHIFT_DR)
            update_dr = int(pre_state is TapState.UPDATE_DR)
            tdo = model.tick(tms, tdi)
        trace.append((pre_state.value, pre_instr, capture_dr, shift_dr, update_dr, tdo))
    return trace, reg


def run_on_rtl(netlist, yosys_command, iverilog_command, vvp_command, fixtures_dir, stimulus) -> list[tuple]:
    inserted_verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
    lines = "\n".join(" ".join(str(v) for v in row) for row in stimulus) + "\n"

    with tempfile.TemporaryDirectory(prefix="warptap-sib-cross-sim-") as tmpdir:
        inserted_path = Path(tmpdir) / "trivial_sib_inserted.v"
        inserted_path.write_text(inserted_verilog, encoding="utf-8")
        stdout = run_verilog_testbench(
            [inserted_path, fixtures_dir / "tb_sib_trivial.v"],
            extra_inputs={"stimulus.txt": lines},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    trace = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, state, instr, cap, shift, upd, tdo, _y = line.split(",")
        trace.append((int(state), int(instr), int(cap), int(shift), int(upd), int(tdo)))
    return trace


def test_extest_shift_through_real_network_matches_python_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    stimulus = _directed_stimulus()

    netlist, graph = _build_and_insert(fixtures_dir, yosys_command)
    python_trace, reg = run_on_python(graph, stimulus)
    rtl_trace = run_on_rtl(
        netlist, yosys_command, iverilog_command, vvp_command, fixtures_dir, stimulus
    )

    assert rtl_trace == python_trace
    # Not vacuous: sensor_a's SIB really did open (po=1), sensor_b's stayed closed --
    # confirmed via the Python model's own internal state, matching the RTL trace it
    # was just proven identical to.
    sensor_a_slot = next(s for s in reg._slots if s.sib_name == "sib_sensor_a")
    sensor_b_slot = next(s for s in reg._slots if s.sib_name == "sib_sensor_b")
    assert sensor_a_slot.sib_po == 1
    assert sensor_b_slot.sib_po == 0
    # And real, distinguishable data moved through the final open-network shift --
    # not a degenerate all-same trace.
    final_shift_tdos = [t[5] for t in rtl_trace[-5:]]
    assert len(set(final_shift_tdos)) > 1


def test_all_closed_network_behaves_as_pure_bypass_of_chain_length(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """Baseline scenario: with every SIB left closed, the network is just `len(chain)`
    1-bit cells chained together -- an ordinary shift register, no instrument content
    ever reachable. Shifts a known pattern through both engines and confirms they
    still agree (a simpler, fully-closed cross-check complementing the directed
    open-one-SIB scenario above)."""
    tms_tdi: list[tuple[int, int]] = [(0, 0), (0, 0), (0, 0)]
    tms_tdi += _navigate_and_select_extest()
    tms_tdi += _shift_dr([1, 0])
    tms_tdi += [(0, 0)] * 2
    stimulus = [(0, 0, 0, 0, 0, 0)] + [(tms, tdi, 1, 1, 0, 0) for tms, tdi in tms_tdi]

    netlist, graph = _build_and_insert(fixtures_dir, yosys_command)
    python_trace, _reg = run_on_python(graph, stimulus)
    rtl_trace = run_on_rtl(
        netlist, yosys_command, iverilog_command, vvp_command, fixtures_dir, stimulus
    )

    assert rtl_trace == python_trace
