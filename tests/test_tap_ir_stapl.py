"""Pure-Python tests for the STAPL emitter (implementation_plan.md §7 Stage 7)."""

from __future__ import annotations

import pytest

from warptap.stapl_crc16 import stapl_file_crc
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, Runtest, ShiftDR, ShiftIR
from warptap.tap_ir_stapl import TapIrStaplError, to_stapl

_KWARGS = dict(device="warptap-test-device", date="2026-08-20")


def test_mandatory_note_fields_all_present():
    stapl = to_stapl([], **_KWARGS)
    for field in (
        "DEVICE", "DATE", "CREATOR", "STAPL_VERSION", "ALG_VERSION",
        "STACK_DEPTH", "TARGET", "MAX_FREQ",
    ):
        assert f'NOTE "{field}"' in stapl


def test_device_and_date_are_not_fabricated():
    stapl = to_stapl([], device="my_chip", date="2026-01-01")
    assert 'NOTE "DEVICE" "my_chip";' in stapl
    assert 'NOTE "DATE" "2026-01-01";' in stapl


def test_target_defaults_to_first_chain_position_not_a_placeholder_word():
    """TARGET (JESD71 Table 13) is a comma-separated list of 1-based chain-position
    numbers, not free text -- "1" is the correct default for warptap's v1 scope (a
    standalone TAP, never a multi-device chain), not an arbitrary placeholder string."""
    stapl = to_stapl([], **_KWARGS)
    assert 'NOTE "TARGET" "1";' in stapl


def test_action_and_procedure_present():
    stapl = to_stapl([], **_KWARGS)
    assert "ACTION RUN = DO_RUN;" in stapl
    assert "PROCEDURE DO_RUN;" in stapl
    assert "ENDPROC;" in stapl


def test_irstop_drstop_header_present():
    stapl = to_stapl([], **_KWARGS)
    assert "IRSTOP IDLE;" in stapl
    assert "DRSTOP IDLE;" in stapl


def test_gotostate_ops_are_dropped():
    ops = [GotoState(TapState.SHIFT_DR), GotoState(TapState.RUN_TEST_IDLE)]
    stapl = to_stapl(ops, **_KWARGS)
    assert "DRSCAN" not in stapl
    assert "IRSCAN" not in stapl


def test_irscan_and_drscan_render_bits_and_hex():
    ops = [ShiftIR(bits=8, tdi=0x41), ShiftDR(bits=16, tdi=0x1)]
    stapl = to_stapl(ops, **_KWARGS)
    assert "IRSCAN 8, $41;" in stapl
    assert "DRSCAN 16, $0001;" in stapl


def test_exactly_one_of_tdo_or_mask_raises_named_error():
    """STAPL's COMPARE clause needs a compare value AND a mask together (JESD71's grammar
    has no "compare without mask" form) -- an op with only one set is a malformed/impossible
    request, distinct from the normal "no COMPARE at all" case."""
    with pytest.raises(TapIrStaplError, match="COMPARE"):
        to_stapl([ShiftDR(bits=4, tdi=0, tdo=0)], **_KWARGS)
    with pytest.raises(TapIrStaplError, match="COMPARE"):
        to_stapl([ShiftIR(bits=4, tdi=0, mask=0xF)], **_KWARGS)


def test_compare_clause_renders_boolean_declaration_and_enforcement_if():
    """The full COMPARE idiom this emitter reproduces (JESD71 §8.8/§8.18, confirmed against
    real vendor STAPL files and the Altera Jam reference player source): a BOOLEAN result
    variable declared immediately before use, the DRSCAN/IRSCAN's own trailing COMPARE
    clause, then an IF checking that result -- without the IF, a failed COMPARE would
    silently not abort (STAPL's own documented behavior), unlike SVF's TDO/MASK which a
    real player enforces automatically."""
    ops = [ShiftDR(bits=8, tdi=0x00, tdo=0x41, mask=0xFF)]
    stapl = to_stapl(ops, **_KWARGS)
    assert "BOOLEAN compare_result_0;" in stapl
    assert "DRSCAN 8, $00, COMPARE $41, $FF, compare_result_0;" in stapl
    assert "IF compare_result_0 == 0 THEN EXIT (1);" in stapl
    # Declaration must precede use, and the IF must follow it -- not just be present anywhere.
    decl_idx = stapl.index("BOOLEAN compare_result_0;")
    scan_idx = stapl.index("DRSCAN 8, $00, COMPARE")
    if_idx = stapl.index("IF compare_result_0 == 0")
    assert decl_idx < scan_idx < if_idx


def test_compare_clause_works_for_irscan_too():
    ops = [ShiftIR(bits=5, tdi=0x00, tdo=0b10101, mask=0b11111)]
    stapl = to_stapl(ops, **_KWARGS)
    assert "IRSCAN 5, $00, COMPARE $15, $1F, compare_result_0;" in stapl


def test_each_compare_op_gets_its_own_uniquely_named_result_variable():
    ops = [
        ShiftDR(bits=4, tdi=0x0, tdo=0x1, mask=0xF),
        ShiftDR(bits=4, tdi=0x0),  # bare scan in between -- no result variable, no gap in numbering
        ShiftIR(bits=4, tdi=0x0, tdo=0x2, mask=0xF),
    ]
    stapl = to_stapl(ops, **_KWARGS)
    assert "compare_result_0" in stapl
    assert "compare_result_1" in stapl
    assert "compare_result_2" not in stapl
    assert stapl.count("BOOLEAN compare_result_") == 2


def test_runtest_renders_as_wait_cycles():
    ops = [Runtest(100, run_state=TapState.RUN_TEST_IDLE, end_state=TapState.PAUSE_IR)]
    stapl = to_stapl(ops, **_KWARGS)
    assert "WAIT IDLE, 100 CYCLES, IRPAUSE;" in stapl


def test_unsupported_op_raises_named_error():
    with pytest.raises(TapIrStaplError, match="does not support"):
        to_stapl(["not an op"], **_KWARGS)


def test_irunloop_sck_produced_pulsepin_is_rejected():
    """Stage 15: iRunLoop(..., sck_port=...) appends a real PulsePin -- STAPL's own catch-all
    unsupported-op error already covers it (no dedicated PulsePin handling exists, or needs
    to), same as any other PulsePin usage since Stage 13."""
    from warptap.pdl_interpreter import PDLInterpreter
    from warptap.sib_plan import InstrumentSpec, build_sib_plan

    graph, root = build_sib_plan([InstrumentSpec("sensor_a", width=1, capture_value=0)])
    pdl = PDLInterpreter(graph, root)
    pdl.iRunLoop(3, sck_port="sysclk")
    with pytest.raises(TapIrStaplError, match="does not support"):
        to_stapl(pdl.program, **_KWARGS)


def test_crc_statement_is_the_last_line_and_is_hex():
    stapl = to_stapl([ShiftIR(bits=4, tdi=0)], **_KWARGS)
    lines = [line for line in stapl.splitlines() if line.strip()]
    assert lines[-1].startswith("CRC ")
    assert lines[-1].endswith(";")
    hex_part = lines[-1][len("CRC "):-1]
    assert len(hex_part) == 4
    int(hex_part, 16)  # raises ValueError if not valid hex


def test_crc_matches_a_real_recomputation_over_the_body():
    """Not vacuous: recompute the CRC independently over the emitted body text (everything
    before the CRC line) and confirm it matches what to_stapl() actually wrote -- proves
    the "compute before appending" boundary is exactly right, not just present."""
    ops = [ShiftIR(bits=8, tdi=0x41), ShiftDR(bits=4, tdi=0b1010)]
    stapl = to_stapl(ops, **_KWARGS)
    body, _sep, crc_line = stapl.rpartition("\nCRC ")
    assert crc_line  # confirms the split actually found the marker
    # rpartition's `body` includes the body's own trailing newline but NOT the second one
    # (the blank line between ENDPROC; and CRC) -- that second newline is itself covered by
    # the CRC (confirmed against the real Altera Jam STAPL Player reference source; see
    # tap_ir_stapl.py's to_stapl() comment), so it must be added back here to match exactly
    # what to_stapl() itself passed to stapl_file_crc().
    expected_crc = stapl_file_crc(body + "\n")
    written_crc = int(crc_line[:-2], 16)  # strip trailing ";\n"
    assert written_crc == expected_crc


def test_crc_changes_when_ops_change():
    stapl_a = to_stapl([ShiftIR(bits=8, tdi=0x41)], **_KWARGS)
    stapl_b = to_stapl([ShiftIR(bits=8, tdi=0x42)], **_KWARGS)
    crc_a = stapl_a.splitlines()[-1]
    crc_b = stapl_b.splitlines()[-1]
    assert crc_a != crc_b
