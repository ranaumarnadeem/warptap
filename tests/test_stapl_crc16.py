"""Pure-Python tests for the STAPL CRC-16 algorithm (implementation_plan.md §7 Stage 7).
JESD71 Annex B's algorithm is CRC-16/IBM-SDLC (a.k.a. CRC-16/X-25) -- verified against that
standard's own published catalog check value, independent of STAPL-specific framing.
"""

from __future__ import annotations

from warptap.stapl_crc16 import crc16_x25, stapl_file_crc


def test_crc16_x25_matches_standard_catalog_check_value():
    """The well-known CRC-16/X-25 check value: CRC-16/X-25 of the ASCII bytes
    "123456789" is 0x906E -- a standard, independently-citable reference for this exact
    parameter set (poly 0x1021 reflected, init 0xFFFF, xorout 0xFFFF), corroborating
    JESD71 Annex B's own algorithm choice without needing the STAPL-specific framing."""
    assert crc16_x25(b"123456789") == 0x906E


def test_crc16_x25_of_empty_input_is_zero():
    """init=0xFFFF XOR xorout=0xFFFF == 0 -- a direct, cheap sanity check of the
    init/xorout pairing itself."""
    assert crc16_x25(b"") == 0


def test_stapl_file_crc_strips_carriage_returns():
    """CR-LF vs LF-only line endings must never change the computed CRC (JESD71 Annex
    B's own documented behavior) -- proven by checking the two encodings agree, not by
    asserting a specific value."""
    lf_only = "line one\nline two\n"
    crlf = "line one\r\nline two\r\n"
    assert stapl_file_crc(lf_only) == stapl_file_crc(crlf)


def test_stapl_file_crc_is_ascii_bytes_of_crc16_x25():
    text = "hello world"
    assert stapl_file_crc(text) == crc16_x25(text.encode("ascii"))
