"""Live SVF grammar validation against a real, independent player (implementation_plan.md
§7 Stage 7, §8's own testing strategy: "validated by feeding output through an existing
consumer... not a self-written parser"). Runs warptap's own SVF emitter output through a real
OpenOCD ``svf`` command (``svf_openocd_check.run_svf_via_openocd``'s ``dummy``-adapter setup)
-- proving grammar/syntax correctness against code warptap didn't write, independent of
warptap's own test suite. This was the one gap Stage 7 shipped with undone (blocked earlier
this session by a WSL sudo credential issue); closed here now that OpenOCD is installed.

Deliberately checks GRAMMAR, not CONTENT: the ``dummy`` interface has no real chip behind it,
so an SDR/SIR carrying a TDO/MASK expected-value clause naturally can't match real captured
content -- that's already checked elsewhere (test_pdl_interpreter_cross_sim.py, the RTL
cross-sim suite). What these tests confirm, independently, is that OpenOCD's own separately-
implemented parser accepts every statement warptap emits and decodes every hex literal
exactly as intended -- confirmed directly this session: a deliberately malformed SVF file
produces "unknown parameter"/"fail to run command at line N", distinct wording this module
asserts never appears for warptap's real output.
"""

from __future__ import annotations

from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.svf_openocd_check import run_svf_via_openocd
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_svf import to_svf

IR_WIDTH = 4

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _realistic_write_ops() -> list:
    """A real iApply() sequence spanning IR-select, two different instruments' writes, and
    an iRunLoop -- exercises SIR, SDR, and RUNTEST all in one file, matching a genuine PDL
    program's shape, not a hand-crafted toy list."""
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b110)
    pdl.iApply()
    pdl.iRunLoop(20)
    pdl.iTarget("sensor_b")
    pdl.iWrite(0b01)
    pdl.iApply()
    return _select_extest_ops() + pdl.program


def _realistic_read_ops() -> list:
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b101)
    pdl.iRead(0b101)
    return pdl.iApply()


def test_write_only_svf_is_grammatically_clean_through_real_openocd(openocd_command):
    """No TDO/MASK anywhere in this file -- the dummy adapter has nothing real to disagree
    with, so a clean pass here isolates pure grammar correctness (SIR/SDR/RUNTEST, hex TDI
    encoding, the ENDIR/ENDDR header) with zero confounding content mismatches."""
    ops = _realistic_write_ops()
    svf = to_svf(ops)
    output = run_svf_via_openocd(svf, tap_irlen=IR_WIDTH, openocd_command=openocd_command)

    assert "unknown parameter" not in output
    assert "fail to run command" not in output
    assert "svf file programmed successfully" in output
    assert "with 0 errors" in output


def test_read_svf_grammar_is_correct_even_though_content_cant_match_a_fake_chip(
    openocd_command,
):
    """Grammar-only check for the TDO/MASK path: the dummy driver's own fixed TDO behavior
    generally can't match a real captured value, so a content ("tdo check error") mismatch
    here is EXPECTED, not a failure -- what's asserted in that branch is that OpenOCD's own
    independently-decoded WANT/MASK hex values match exactly what warptap's emitter wrote,
    proving the encoding itself (not just "OpenOCD didn't crash") round-trips correctly
    through a real, separate parser. If content happens to match anyway, that is an even
    stronger (full) pass, handled by the other branch."""
    ops = _realistic_read_ops()
    svf = to_svf(ops)
    output = run_svf_via_openocd(svf, tap_irlen=IR_WIDTH, openocd_command=openocd_command)

    assert "unknown parameter" not in output
    assert "fail to run command" not in output
    assert "svf processing file" in output

    read_op = next(op for op in ops if getattr(op, "tdo", None) is not None)
    if "tdo check error" in output:
        assert f"0x{read_op.tdo:x}" in output
        assert f"0x{read_op.mask:x}" in output
    else:
        assert "svf file programmed successfully" in output
