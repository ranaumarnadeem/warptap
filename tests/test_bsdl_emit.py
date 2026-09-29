"""Structural and golden-text tests for ``warptap.bsdl_emit.to_bsdl``.

Pure Python, no external tool. The structural tests read the *emitted text* back through
``tests/bsdl_reader.py`` and compare it with ``warptap.tap_model``'s constants (written out here
independently of ``bsdl_emit``), so a formatting slip or a wrong bit order in the emitter fails
here. The golden file only pins the exact format; it does not by itself prove the values are
right -- the structural tests and ``test_bsdl_emit_cross_sim.py`` (real TAP RTL under Icarus) do.

**No independent BSDL parser validates this output**, and that is stated plainly rather than
worked around (the same discipline as the PDL-emit tests). What was tried:

- *UrJTAG* (``sudo apt install urjtag``, Ubuntu noble ``0.10+r2007-1.2build4``): the only
  candidate found that would accept a TAP-only file (it checks syntax and structure, not
  conformance). Both ``jtag``'s ``bsdl test``/``bsdl dump`` and ``bsdl2jtag`` are unusable here:
  every input fails with "BSDL file ... contains errors in VHDL stage" and then SIGSEGV -- a
  hand-written valid ``entity`` and a deliberately invalid one (missing ``;``) behave
  identically, so the tool cannot tell good from bad. ``strace`` shows it opening the file and
  then reading stdin, and ``gdb`` puts the crash in ``bsdl2jtag``'s own ``main`` on the error
  path. A test built on it would prove nothing. Untried: building upstream UrJTAG from source.
- *bsdl-parser* 0.1.1 (PyPI, cyrozap's grammar), installed in a throwaway Python 3.12 venv and run:
  its ``bsdl2json`` fails to import -- ``grako`` 3.99.9 does ``from collections import Mapping``,
  removed in Python 3.10. Its grammar also makes the boundary register mandatory.
- *cb_bsdl_parser* 0.12.0 (PyPI), installed in a separate venv (it needs ANTLR runtime 4.13, which
  conflicts with the 4.7.2 this suite pins) and run: ``CBBsdl(path, run_checks=False)`` raises
  ``IndexError`` in ``build_bsr_content`` for this file, for a deliberately broken copy of it and
  for plain garbage alike, and its lexer reports token errors on text inside BSDL string
  literals (the ``/`` in "SAMPLE/PRELOAD", the ``+`` in ``1.000000e+07``).
- ``antlr/grammars-v4`` has no BSDL grammar.
- Every standard-following parser requires ``BOUNDARY_LENGTH``/``BOUNDARY_REGISTER``, which this
  TAP-only file deliberately omits, so no independent tool could check its conformance anyway.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bsdl_reader import parse_bsdl

from warptap.bsdl_emit import (
    NETWORK_ACCESS_BSDL_INSTRUCTION,
    BsdlEmitError,
    to_bsdl,
)
from warptap.tap_model import (
    CAPTURE_IR_PATTERN,
    DEFAULT_IR_WIDTH,
    IDCODE_VALUE,
    OPCODE_EXTEST,
    OPCODE_IDCODE,
    OPCODE_SAMPLE_PRELOAD,
    bypass_opcode,
)
from warptap.tap_ports import TCK, TDI, TDO, TMS, TRST_N

_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden_bsdl" / "chip_tap.bsd"


def _bits(value: int, width: int) -> str:
    return format(value, f"0{width}b")


def _emit(name: str = "chip", freq: float = 10e6):
    return to_bsdl(name, tck_max_freq_hz=freq)


def test_output_is_byte_identical_to_golden():
    assert _emit() == _GOLDEN.read_bytes().decode("utf-8")


def test_entity_name_appears_in_entity_and_end_lines():
    bsdl = parse_bsdl(_emit("mem_subsystem_mbist"))
    assert bsdl.entity == "mem_subsystem_mbist"
    assert bsdl.end_name == "mem_subsystem_mbist"


def test_ports_are_the_five_tap_pins_with_tdo_the_only_output():
    bsdl = parse_bsdl(_emit())
    assert bsdl.ports == {TCK: "in", TMS: "in", TDI: "in", TRST_N: "in", TDO: "out"}


def test_tap_scan_attributes_bind_the_right_pins():
    bsdl = parse_bsdl(_emit())
    assert bsdl.tap_scan == {
        "TAP_SCAN_IN": TDI,
        "TAP_SCAN_OUT": TDO,
        "TAP_SCAN_MODE": TMS,
        "TAP_SCAN_RESET": TRST_N,
        "TAP_SCAN_CLOCK": TCK,
    }


@pytest.mark.parametrize(
    ("freq", "expected_text"), [(1e6, "1.000000e+06"), (10e6, "1.000000e+07"), (25e5, "2.500000e+06")]
)
def test_tck_frequency_is_a_vhdl_real_with_an_exponent(freq, expected_text):
    text = _emit(freq=freq)
    assert f"({expected_text}, BOTH)" in text
    assert parse_bsdl(text).tap_scan_clock == (pytest.approx(freq), "BOTH")


def test_instruction_length_matches_the_tap():
    assert parse_bsdl(_emit()).instruction_length == DEFAULT_IR_WIDTH == 4


def test_instruction_opcodes_match_the_tap_msb_left():
    opcodes = parse_bsdl(_emit()).opcodes
    assert opcodes == {
        "EXTEST": [_bits(OPCODE_EXTEST, 4)],
        "SAMPLE": [_bits(OPCODE_SAMPLE_PRELOAD, 4)],
        "PRELOAD": [_bits(OPCODE_SAMPLE_PRELOAD, 4)],
        "IDCODE": [_bits(OPCODE_IDCODE, 4)],
        "BYPASS": [_bits(bypass_opcode(4), 4)],
    }
    assert opcodes["EXTEST"] == ["0000"]
    assert opcodes["BYPASS"] == ["1111"]  # IEEE 1149.1 mandates all ones


def test_no_two_distinct_instructions_share_an_opcode_except_sample_preload():
    """BSDL 2001 lets SAMPLE and PRELOAD share one opcode (they are one physical instruction);
    nothing else may collide."""
    opcodes = parse_bsdl(_emit()).opcodes
    by_code: dict[str, list[str]] = {}
    for name, codes in opcodes.items():
        by_code.setdefault(codes[0], []).append(name)
    assert sorted(map(sorted, (v for v in by_code.values() if len(v) > 1))) == [["PRELOAD", "SAMPLE"]]


def test_instruction_capture_ends_in_01_and_matches_the_tap():
    capture = parse_bsdl(_emit()).instruction_capture
    assert capture == _bits(CAPTURE_IR_PATTERN, 4)
    assert len(capture) == DEFAULT_IR_WIDTH
    assert capture.endswith("01")  # IEEE 1149.1: the two LSBs are 01


def test_idcode_register_is_32_bits_with_lsb_one_and_equals_the_tap_value():
    idcode = parse_bsdl(_emit()).idcode_register
    assert len(idcode) == 32
    assert idcode[-1] == "1"
    assert int(idcode, 2) == IDCODE_VALUE


def test_register_access_declares_idcode_but_not_a_network_or_boundary_register():
    access = parse_bsdl(_emit()).register_access
    assert access == {"DEVICE_ID": ["IDCODE"]}


def test_no_boundary_register_attributes_are_declared():
    names = parse_bsdl(_emit()).attribute_names
    assert not [n for n in names if n.startswith("BOUNDARY")]


def test_network_access_instruction_is_declared_and_is_opcode_0000():
    bsdl = parse_bsdl(_emit())
    assert NETWORK_ACCESS_BSDL_INSTRUCTION == "EXTEST"
    assert bsdl.opcodes[NETWORK_ACCESS_BSDL_INSTRUCTION] == [_bits(OPCODE_EXTEST, 4)]


def test_design_warning_states_the_scope_and_the_network_routing():
    warning = parse_bsdl(_emit()).design_warning
    assert "NOT a chip-level BSDL" in warning
    assert "BOUNDARY_REGISTER" in warning
    assert f"{NETWORK_ACCESS_BSDL_INSTRUCTION} ({_bits(OPCODE_EXTEST, 4)})" in warning
    assert f"SAMPLE/PRELOAD ({_bits(OPCODE_SAMPLE_PRELOAD, 4)})" in warning
    assert "IJTAG network" in warning
    assert "placeholder" in warning


def test_design_warning_string_pieces_concatenate_without_stray_quotes():
    text = _emit()
    assert text.count('"') % 2 == 0
    assert "\r" not in text


def test_output_is_deterministic():
    assert _emit() == _emit()


@pytest.mark.parametrize(
    "name",
    ["", "1chip", "chip_", "_chip", "my__chip", "my-chip", "chip$1", "has space", "entity", "PORT", "In"],
)
def test_invalid_entity_name_raises_a_named_error(name):
    with pytest.raises(BsdlEmitError, match="entity name"):
        _emit(name)


@pytest.mark.parametrize("name", ["chip", "Chip2", "mem_subsystem_mbist", "a", "A1_b2_c3"])
def test_valid_entity_names_are_accepted(name):
    assert parse_bsdl(_emit(name)).entity == name


@pytest.mark.parametrize("freq", [0, -1.0, float("nan"), float("inf"), True, "10e6", None])
def test_invalid_tck_frequency_raises_a_named_error(freq):
    with pytest.raises(BsdlEmitError, match="tck_max_freq_hz"):
        _emit(freq=freq)
