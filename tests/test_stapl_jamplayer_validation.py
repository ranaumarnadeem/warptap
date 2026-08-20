"""Live STAPL grammar AND CRC validation against a real, independent player
(implementation_plan.md §7 Stage 7, §8's own testing strategy: "validated by feeding output
through an existing consumer... not a self-written parser"). Runs warptap's own STAPL
emitter output through a real, independently-built Altera "Jam STAPL Player" reference
interpreter (jamplayer_check.py's `run_stapl_via_jam`, PORT=UNIX software-only build --
see that module's docstring for how to obtain one). This is what actually caught a real bug
this session: to_stapl()'s CRC statement was computed one newline byte short of what JESD71
Annex B (and this player's own jamcrc.c) require -- self-consistent in this project's own
prior unit tests, but wrong against ground truth. Skips cleanly (WARPTAP_JAM_CMD not set) in
any environment without a locally-built player, same discipline as every other *_command
fixture in this project.
"""

from __future__ import annotations

from warptap.jamplayer_check import run_stapl_via_jam
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import ShiftDR
from warptap.tap_ir_stapl import to_stapl

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]


def _realistic_write_ops() -> list:
    """A real iApply() sequence spanning two different instruments' writes and an
    iRunLoop -- matches a genuine PDL program's shape, not a hand-crafted toy list."""
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b110)
    pdl.iApply()
    pdl.iRunLoop(5)
    pdl.iTarget("sensor_b")
    pdl.iWrite(0b01)
    pdl.iApply()
    return pdl.program


def test_write_only_stapl_passes_crc_and_runs_clean_through_real_jam_player(jam_command):
    """The CRC check alone is a strong, independent proof: the player recomputes the CRC
    itself, from the raw file bytes, using its own C implementation of JESD71 Annex B's
    algorithm -- distinct code, distinct language, same result required."""
    ops = _realistic_write_ops()
    stapl = to_stapl(ops, device="warptap-test-device", date="2026-08-20")
    output = run_stapl_via_jam(stapl, jam_command=jam_command)

    assert "CRC matched" in output
    assert "CRC mismatch" not in output
    assert "Exit code = 0... Success" in output


def test_compare_clause_passes_when_expectation_matches_the_dummy_jtags_fixed_tdo(
    jam_command,
):
    """This player's software-only build always returns TDO=0 (no real chip -- see
    jamplayer_check.py's module docstring), so an all-zero expected value/mask is the one
    COMPARE it can genuinely satisfy -- proving the CRC, COMPARE, BOOLEAN, and enforcement
    IF grammar all round-trip correctly end to end, with no EXIT firing."""
    ops = [ShiftDR(bits=8, tdi=0x00, tdo=0x00, mask=0xFF)]
    stapl = to_stapl(ops, device="warptap-test-device", date="2026-08-20")
    output = run_stapl_via_jam(stapl, jam_command=jam_command)

    assert "CRC matched" in output
    assert "Exit code = 0... Success" in output


def test_compare_clause_mismatch_triggers_the_enforcement_exit(jam_command):
    """Direct contrast: an expectation the fixed TDO=0 can't satisfy must trigger this
    emitter's own `IF <result> == 0 THEN EXIT (1);` line -- proving the enforcement path
    (added specifically because STAPL's COMPARE doesn't abort by itself, unlike SVF's
    TDO/MASK -- see tap_ir_stapl.py's module docstring) actually works at runtime, not just
    that the text is present in the emitted file."""
    ops = [ShiftDR(bits=8, tdi=0x00, tdo=0xFF, mask=0xFF)]
    stapl = to_stapl(ops, device="warptap-test-device", date="2026-08-20")
    output = run_stapl_via_jam(stapl, jam_command=jam_command)

    assert "CRC matched" in output  # still a grammatically/CRC-valid file
    assert "Exit code = 1" in output
