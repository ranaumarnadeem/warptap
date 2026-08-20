"""CRC-16 for STAPL files (JESD71 Annex B, implementation_plan.md §7 Stage 7's own testing
strategy: "STAPL CRC16 unit-tested directly against JESD71 Annex A's worked example (expected
CRC 3759) -- a free, spec-provided oracle").

**Algorithm, confirmed by two independent sources** (JESD71 Annex B's own reference C code,
and Altera/Actel's real ``jamcrc.c`` STAPL-player reference implementation, which agree
exactly): shift register initialized to ``0xFFFF``; each byte processed LSB-first against the
*reflected* polynomial mask ``0x8408`` (the bit-reversal of the generator polynomial
``X^16 + X^12 + X^5 + 1`` = ``0x1021``, stated in Annex B's prose); final CRC is the bitwise
complement of the register. This exact parameter set (poly ``0x1021``, init ``0xFFFF``,
refin=True, refout=True, xorout ``0xFFFF``) is the standard, named variant catalogued as
**CRC-16/IBM-SDLC** (a.k.a. CRC-16/X-25, CRC-16/ISO-HDLC, CRC-B, X.25/V.41 FCS) -- *not* the
same as "CRC-16/CCITT-FALSE" (same poly/init but unreflected) or "XModem" (unreflected,
init 0), both also colloquially called "CCITT," which is exactly the ambiguity this
docstring exists to head off.

**Scope, per JESD71 Annex B**: the CRC is a plain whole-file *source-text* checksum -- every
raw ASCII byte of the STAPL file (NOTE/ACTION/PROCEDURE/DATA text, comments, whitespace, all
included) from the start of the file up to (but excluding) the file's own ``CRC`` statement,
with ``\\r`` bytes unconditionally skipped (so CR-LF vs. LF-only line endings never change the
value). It is *not* computed over JTAG bits shifted, and *not* per-``ACTION``.

**The ``CRC`` statement's own operand is hexadecimal, not decimal** (JESD71 §8.6,
``CRC <4-digit hexadecimal number>;`` -- confirmed by two worked instances in Annex A:
``CRC 3759;`` and ``CRC 0374;``, the latter's leading zero confirming a fixed-width,
zero-padded 4-hex-digit field). So the plan's own cited oracle value "3759" is the literal hex
digits ``0x3759`` (decimal 14169), not decimal 3759 -- an easy, easy-to-get-wrong detail this
module's own tests lock in explicitly.
"""

from __future__ import annotations

_POLY_MASK = 0x8408  # bit-reversal of the generator polynomial 0x1021 (X^16+X^12+X^5+1)
_INIT = 0xFFFF
_XOROUT = 0xFFFF


def crc16_x25(data: bytes) -> int:
    """The plain, generic CRC-16/IBM-SDLC (a.k.a. CRC-16/X-25) algorithm over arbitrary
    bytes -- no STAPL-specific scoping (no ``\\r`` skipping, no "stop before the CRC
    statement" boundary) baked in here; see :func:`stapl_file_crc` for that. Verified in
    this module's own tests against the standard CRC-16/X-25 catalog check value
    (``crc16_x25(b"123456789") == 0x906E``)."""
    crc = _INIT
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ _POLY_MASK
            else:
                crc >>= 1
    return crc ^ _XOROUT


def stapl_file_crc(text_before_crc_statement: str) -> int:
    """The STAPL-specific wrapper JESD71 Annex B actually specifies: strip every ``\\r``
    byte, encode as ASCII, and run :func:`crc16_x25` over the result. ``text_before_crc_
    statement`` must be exactly the file's own text up to (not including) its own ``CRC
    <hex4>;`` statement -- since :mod:`warptap.tap_ir_stapl` *constructs* the file forward
    (compute the CRC, then append the CRC line), this boundary is exact by construction
    and needs none of a streaming parser's own "rewind past the CRC keyword" logic."""
    return crc16_x25(text_before_crc_statement.replace("\r", "").encode("ascii"))
