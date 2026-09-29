"""BSDL (IEEE 1149.1 Boundary-Scan Description Language) emitter for the TAP warptap inserts.

Scope is exactly what an IEEE 1687 ICL ``AccessLink ... Of STD_1149_1_2001`` needs to point at:
the entity, the five TAP pins, the TAP scan attributes, the instruction register (length,
opcodes, capture pattern), the IDCODE register, and the instruction that reaches the IJTAG
network. Every value comes from :mod:`warptap.tap_model` / :mod:`warptap.tap_ports` -- no literal
is duplicated here -- so this file can only drift from ``rtl/tap_core.v`` if ``tap_model`` itself
does, which ``tests/test_tap_fsm_cross_sim.py`` checks against the RTL.

**This is NOT a chip-level BSDL.** It declares no ``BOUNDARY_LENGTH`` and no
``BOUNDARY_REGISTER``. Standard-following BSDL parsers and boundary-scan tools treat the boundary
register as mandatory (IEEE 1149.1's BYPASS/SAMPLE/PRELOAD/EXTEST requirements all concern it), so
expect such a tool to reject this file or report it incomplete; a permissive parser that only
checks syntax (UrJTAG's ``bsdl test``) accepts it. The mandatory-attribute list here was taken
from secondary sources (ASSET InterTech's BSDL overview, Lauterbach's boundary-scan guide, the
IEEE 1149.1 working group's published ``STD_1149_1_2012`` package declarations); the primary IEEE
1149.1 text is paywalled and was not read. When a design has a boundary-scan register (via
:mod:`warptap.bsr_insert`), extending this emitter to describe it is a follow-up, not something
this module attempts.

**What EXTEST means here.** In real 1149.1, EXTEST drives the boundary register. In a warptap
SIB design there is none: ``rtl/tap_core.v`` presents the SIB chain's tail (``external_dr_tdo``) at
``tdo`` for every instruction that is neither IDCODE nor BYPASS, so EXTEST *and* SAMPLE/PRELOAD
reach the IJTAG network (see :data:`warptap.tap_model.NETWORK_ACCESS_INSTRUCTION`). The emitted
``DESIGN_WARNING`` says so. The network's length depends on SIB state, so it cannot be declared as
a fixed-length ``REGISTER_ACCESS`` entry and is deliberately not.

**The IDCODE value is a placeholder** (see :data:`warptap.tap_model.IDCODE_VALUE`), emitted as-is
because the BSDL must match the hardware.

**No independent BSDL parser validates this output.** The one candidate that would accept a
TAP-only file, UrJTAG (its parser checks syntax and structure only), was installed and tried; the
Ubuntu-packaged 0.10+r2007 build is unusable -- see ``tests/test_bsdl_emit.py``'s docstring for the
evidence. Standard-following parsers require the boundary register, so no independent tool could
check conformance of this file anyway: it is non-conformant by design. What is checked instead:
the emitted text is read back and compared with ``tap_model`` (``tests/test_bsdl_emit.py``), and
every behavioral claim is verified by driving the real TAP RTL under Icarus
(``tests/test_bsdl_emit_cross_sim.py``).
"""

from __future__ import annotations

import math
import re
from typing import List

from warptap.errors import WarptapError
from warptap.tap_model import (
    CAPTURE_IR_PATTERN,
    DEFAULT_IR_WIDTH,
    IDCODE_VALUE,
    NETWORK_ACCESS_INSTRUCTION,
    OPCODE_EXTEST,
    OPCODE_IDCODE,
    OPCODE_SAMPLE_PRELOAD,
    bypass_opcode,
)
from warptap.tap_ports import TCK, TDI, TDO, TMS, TRST_N

NETWORK_ACCESS_BSDL_INSTRUCTION = NETWORK_ACCESS_INSTRUCTION.value
"""The BSDL instruction name that selects the IJTAG network -- what an ICL ``AccessLink`` names."""

_PHYSICAL_PIN_MAP = "UNPACKAGED"
_PIN_MAP_STRING_WIDTH = 68

_IDENTIFIER = re.compile(r"^[A-Za-z](_?[A-Za-z0-9])*$")

# VHDL-93 reserved words: a BSDL entity name is a VHDL identifier and may not be one of these.
_VHDL_RESERVED = frozenset(
    "abs access after alias all and architecture array assert attribute begin block body "
    "buffer bus case component configuration constant disconnect downto else elsif end entity "
    "exit file for function generate generic group guarded if impure in inertial inout is "
    "label library linkage literal loop map mod nand new next nor not null of on open or "
    "others out package port postponed procedure process pure range record register reject "
    "rem report return rol ror select severity shared signal sla sll sra srl subtype then to "
    "transport type unaffected units until use variable wait when while with xnor xor".split()
)


class BsdlEmitError(WarptapError):
    """Raised for an entity name that isn't a valid BSDL/VHDL identifier, or a TCK frequency
    that isn't a finite positive number."""


def _bits(value: int, width: int) -> str:
    """MSB-left bit string: BSDL writes the bit closest to TDI on the left, so the rightmost
    character is the first bit shifted out of TDO (the LSB, matching this project's LSB-first
    shifting everywhere)."""
    return format(value, f"0{width}b")


def _vhdl_string(text: str, width: int = _PIN_MAP_STRING_WIDTH) -> str:
    """``text`` as a concatenation of VHDL string literals, split at word boundaries so no line
    is long. The pieces concatenate back to exactly ``text``."""
    assert '"' not in text, "BSDL string content must not contain a double quote"
    pieces: List[str] = []
    current = ""
    for token in re.findall(r"\S+\s*", text):
        if current and len(current) + len(token) > width:
            pieces.append(current)
            current = ""
        current += token
    if current:
        pieces.append(current)
    return " &\n      ".join(f'"{piece}"' for piece in pieces)


def _design_warning() -> str:
    """Everything a reader of this BSDL must know that the standard attributes can't say. Built
    from the same constants as the attributes above it so the two can't disagree."""
    ir_width = DEFAULT_IR_WIDTH
    extest = _bits(OPCODE_EXTEST, ir_width)
    sample_preload = _bits(OPCODE_SAMPLE_PRELOAD, ir_width)
    return (
        "Generated by warptap. TAP-only description, NOT a chip-level BSDL: no BOUNDARY_LENGTH "
        "or BOUNDARY_REGISTER is declared, so a tool that requires a boundary register will "
        "reject this file. "
        f"{NETWORK_ACCESS_BSDL_INSTRUCTION} ({extest}) and SAMPLE/PRELOAD ({sample_preload}) do "
        "not select a boundary register: they connect TDO to the IEEE 1687 IJTAG network (the "
        "SIB chain) described by the companion ICL AccessLink. Its length depends on SIB state, "
        "so it is not declared in REGISTER_ACCESS. "
        f"Load {NETWORK_ACCESS_BSDL_INSTRUCTION} to reach that network. "
        "Not fully IEEE 1149.1 conformant: TDO changes on the rising edge of TCK and is driven "
        "low, not tri-stated, outside the shift states, and the IDCODE value is a placeholder, "
        "not a registered manufacturer ID. "
        f"PHYSICAL_PIN_MAP {_PHYSICAL_PIN_MAP} is a placeholder: no physical package is "
        "described."
    )


def to_bsdl(entity_name: str, *, tck_max_freq_hz: float) -> str:
    """Render the BSDL for the TAP warptap inserts, as the entity ``entity_name`` (use the same
    string passed to ``to_icl`` as its ``BSDLEntity`` -- the real top module's name).

    ``tck_max_freq_hz`` is required: BSDL's mandatory ``TAP_SCAN_CLOCK`` states the maximum TCK
    frequency, and TCK timing is not something the RTL tells warptap -- the integrator must
    state it. The clock is declared safe to stop in either state (``BOTH``): ``rtl/tap_core.v``
    is fully static edge-triggered logic. Raises :class:`BsdlEmitError` for an entity name that
    isn't a valid BSDL/VHDL identifier (letters, digits, single underscores, starts with a letter,
    not a VHDL reserved word) or a non-positive/non-finite frequency.

    See the module docstring for what this file is not (a chip-level BSDL) and what is and isn't
    independently validated."""
    if not _IDENTIFIER.match(entity_name) or entity_name.lower() in _VHDL_RESERVED:
        raise BsdlEmitError(
            f"{entity_name!r} is not a valid BSDL entity name: it must be a VHDL identifier "
            "(starts with a letter; letters, digits and single underscores only; not a VHDL "
            "reserved word). Pass a different entity_name and give the ICL BSDLEntity the same one."
        )
    if (
        isinstance(tck_max_freq_hz, bool)
        or not isinstance(tck_max_freq_hz, (int, float))
        or not math.isfinite(tck_max_freq_hz)
        or tck_max_freq_hz <= 0
    ):
        raise BsdlEmitError(
            f"tck_max_freq_hz must be a finite number > 0 (Hz), got {tck_max_freq_hz!r}"
        )

    name = entity_name
    ir_width = DEFAULT_IR_WIDTH
    pins = (TCK, TMS, TDI, TRST_N, TDO)
    pin_width = max(len(p) for p in pins)

    port_lines = [
        f"    {pin.ljust(pin_width)} : {'out' if pin == TDO else 'in'} bit" for pin in pins
    ]
    pin_map_lines = [
        f'    "{pin.ljust(pin_width)} : {pin}{"," if pin != pins[-1] else ""}"' for pin in pins
    ]

    version = IDCODE_VALUE >> 28
    part = (IDCODE_VALUE >> 12) & 0xFFFF
    manufacturer = (IDCODE_VALUE >> 1) & 0x7FF
    required_one = IDCODE_VALUE & 1

    opcode_entries = [
        ("EXTEST", OPCODE_EXTEST),
        ("SAMPLE", OPCODE_SAMPLE_PRELOAD),
        ("PRELOAD", OPCODE_SAMPLE_PRELOAD),
        ("IDCODE", OPCODE_IDCODE),
        ("BYPASS", bypass_opcode(ir_width)),
    ]
    name_width = max(len(n) for n, _ in opcode_entries)
    opcode_lines = [
        f'    "{n.ljust(name_width)} ({_bits(op, ir_width)}){"," if i < len(opcode_entries) - 1 else ""}"'
        for i, (n, op) in enumerate(opcode_entries)
    ]
    opcode_body = " &\n".join(opcode_lines)

    lines = [
        "-- BSDL for the IEEE 1149.1 TAP inserted by warptap (TAP only; no boundary register).",
        "-- Read the DESIGN_WARNING attribute below before using this file with a boundary-scan tool.",
        "",
        f"entity {name} is",
        "",
        f'  generic (PHYSICAL_PIN_MAP : string := "{_PHYSICAL_PIN_MAP}");',
        "",
        "  port (",
        ";\n".join(port_lines),
        "  );",
        "",
        "  use STD_1149_1_2001.all;",
        "",
        f'  attribute COMPONENT_CONFORMANCE of {name} : entity is "STD_1149_1_2001";',
        "",
        f"  attribute PIN_MAP of {name} : entity is PHYSICAL_PIN_MAP;",
        "",
        f"  constant {_PHYSICAL_PIN_MAP} : PIN_MAP_STRING :=",
        " &\n".join(pin_map_lines) + ";",
        "",
        f"  attribute TAP_SCAN_IN    of {TDI.ljust(pin_width)} : signal is true;",
        f"  attribute TAP_SCAN_MODE  of {TMS.ljust(pin_width)} : signal is true;",
        f"  attribute TAP_SCAN_OUT   of {TDO.ljust(pin_width)} : signal is true;",
        f"  attribute TAP_SCAN_CLOCK of {TCK.ljust(pin_width)} : signal is ({tck_max_freq_hz:.6e}, BOTH);",
        f"  attribute TAP_SCAN_RESET of {TRST_N.ljust(pin_width)} : signal is true;",
        "",
        f"  attribute INSTRUCTION_LENGTH of {name} : entity is {ir_width};",
        "",
        f"  attribute INSTRUCTION_OPCODE of {name} : entity is",
        opcode_body + ";",
        "",
        f'  attribute INSTRUCTION_CAPTURE of {name} : entity is "{_bits(CAPTURE_IR_PATTERN, ir_width)}";',
        "",
        f"  attribute IDCODE_REGISTER of {name} : entity is",
        f'    "{_bits(version, 4)}" &{" " * 18}-- version',
        f'    "{_bits(part, 16)}" &{" " * 6}-- part number',
        f'    "{_bits(manufacturer, 11)}" &{" " * 11}-- manufacturer (placeholder)',
        f'    "{_bits(required_one, 1)}";{" " * 22}-- required by IEEE 1149.1',
        "",
        f"  attribute REGISTER_ACCESS of {name} : entity is",
        '    "DEVICE_ID (IDCODE)";',
        "",
        f"  attribute DESIGN_WARNING of {name} : entity is",
        "      " + _vhdl_string(_design_warning()) + ";",
        "",
        f"end {name};",
        "",
    ]
    return "\n".join(lines)


__all__ = ["BsdlEmitError", "NETWORK_ACCESS_BSDL_INSTRUCTION", "to_bsdl"]
