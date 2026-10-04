"""A minimal, test-local reader for the BSDL that ``warptap.bsdl_emit.to_bsdl`` emits.

Deliberately NOT a general BSDL parser and NOT an independent oracle: it exists so tests can make
claims from the *emitted text* (what a tool reading the file would believe) rather than from
``warptap.tap_model``'s constants, which the emitter itself was built from. It understands exactly
the attribute shapes this emitter writes and raises ``ValueError`` on anything else.

``strict_problems`` holds a file to what IEEE 1149.1-2001 BSDL requires of every component, so a
test can show the TAP-only file lacks nothing but the boundary register it omits on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

_TOKEN = re.compile(r'"[^"]*"|--[^\n]*')
_ATTRIBUTE = re.compile(
    r'attribute\s+(\w+)\s+of\s+(\w+)\s*:\s*(entity|signal)\s+is\s+((?:"[^"]*"|[^";])*);',
    re.DOTALL,
)
_STRING = re.compile(r'"([^"]*)"')


@dataclass
class Bsdl:
    entity: str
    ports: Dict[str, str]
    instruction_length: int
    opcodes: Dict[str, List[str]]
    instruction_capture: str
    idcode_register: str
    register_access: Dict[str, List[str]]
    tap_scan: Dict[str, str]
    tap_scan_clock: Tuple[float, str]
    design_warning: str
    attribute_names: List[str] = field(default_factory=list)
    end_name: str = ""


def _strip_comments(text: str) -> str:
    return _TOKEN.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)


def _joined_strings(value: str) -> str:
    return "".join(_STRING.findall(value))


def _entries(joined: str) -> Dict[str, List[str]]:
    """``"A (x, y), B (z)"`` -> ``{"A": ["x", "y"], "B": ["z"]}``."""
    out: Dict[str, List[str]] = {}
    for name, body in re.findall(r"(\w+)\s*\(([^)]*)\)", joined):
        out[name] = [part.strip() for part in body.split(",")]
    return out


def _ports(clean: str) -> Dict[str, str]:
    port_block = re.search(r"\bport\s*\((.*?)\)\s*;", clean, re.DOTALL)
    if not port_block:
        raise ValueError("no port clause")
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"(\w+)\s*:\s*(in|out)\s+bit", port_block.group(1))
    }


def _attributes(clean: str) -> Tuple[Dict[Tuple[str, str], str], List[str]]:
    """``{(attribute, of): value}``, and the attribute names in file order."""
    attrs: Dict[Tuple[str, str], str] = {}
    names: List[str] = []
    for m in _ATTRIBUTE.finditer(clean):
        attrs[(m.group(1), m.group(2))] = m.group(4).strip()
        names.append(m.group(1))
    return attrs, names


def parse_bsdl(text: str) -> Bsdl:
    clean = _strip_comments(text)
    entity = re.search(r"\bentity\s+(\w+)\s+is\b", clean)
    if not entity:
        raise ValueError("no entity declaration")
    end = re.search(r"\bend\s+(\w+)\s*;", clean)
    ports = _ports(clean)
    attrs, names = _attributes(clean)

    def entity_attr(attr: str) -> str:
        try:
            return attrs[(attr, entity.group(1))]
        except KeyError:
            raise ValueError(f"missing attribute {attr}") from None

    tap_scan = {
        attr: sig for (attr, sig), _ in attrs.items() if attr in ("TAP_SCAN_IN", "TAP_SCAN_OUT", "TAP_SCAN_MODE", "TAP_SCAN_RESET")
    }
    clock_signal, clock_value = next(
        (sig, val) for (attr, sig), val in attrs.items() if attr == "TAP_SCAN_CLOCK"
    )
    clock = re.fullmatch(r"\(\s*([^,\s]+)\s*,\s*(\w+)\s*\)", clock_value)
    if not clock:
        raise ValueError(f"unparsable TAP_SCAN_CLOCK value {clock_value!r}")
    tap_scan["TAP_SCAN_CLOCK"] = clock_signal

    return Bsdl(
        entity=entity.group(1),
        ports=ports,
        instruction_length=int(entity_attr("INSTRUCTION_LENGTH")),
        opcodes=_entries(_joined_strings(entity_attr("INSTRUCTION_OPCODE"))),
        instruction_capture=_joined_strings(entity_attr("INSTRUCTION_CAPTURE")),
        idcode_register=_joined_strings(entity_attr("IDCODE_REGISTER")),
        register_access=_entries(_joined_strings(entity_attr("REGISTER_ACCESS"))),
        tap_scan=tap_scan,
        tap_scan_clock=(float(clock.group(1)), clock.group(2)),
        design_warning=_joined_strings(entity_attr("DESIGN_WARNING")),
        attribute_names=names,
        end_name=end.group(1) if end else "",
    )


# What IEEE 1149.1-2001 BSDL requires of every component, from the same secondary sources
# warptap.bsdl_emit's docstring names (the IEEE text itself is paywalled and was not read).
MANDATORY_ATTRIBUTES = (
    "COMPONENT_CONFORMANCE",
    "PIN_MAP",
    "TAP_SCAN_IN",
    "TAP_SCAN_MODE",
    "TAP_SCAN_OUT",
    "TAP_SCAN_CLOCK",
    "INSTRUCTION_LENGTH",
    "INSTRUCTION_OPCODE",
    "INSTRUCTION_CAPTURE",
    "BOUNDARY_LENGTH",
    "BOUNDARY_REGISTER",
)
MANDATORY_INSTRUCTIONS = ("BYPASS", "EXTEST", "SAMPLE", "PRELOAD")
_TAP_SCAN = ("TAP_SCAN_IN", "TAP_SCAN_OUT", "TAP_SCAN_MODE", "TAP_SCAN_CLOCK", "TAP_SCAN_RESET")


def strict_problems(text: str, *, waive: Iterable[str] = ()) -> List[str]:
    """Why a reader holding ``text`` to IEEE 1149.1-2001 would refuse it; empty when nothing.

    Checked: every mandatory attribute is present, except those ``waive`` names, and
    IDCODE_REGISTER with an IDCODE instruction; a ``use`` clause for an STD_1149_1 package; every
    port is a TAP pin (bound by a TAP_SCAN_* attribute -- nothing else could describe one without
    a boundary register) and in the PIN_MAP_STRING constant PHYSICAL_PIN_MAP defaults to; the
    mandatory instructions are declared; every opcode is INSTRUCTION_LENGTH bits; the all-ones
    opcode is BYPASS's alone and BYPASS has no other; INSTRUCTION_CAPTURE is INSTRUCTION_LENGTH
    bits ending in 01; IDCODE_REGISTER is 32 bits ending in 1; REGISTER_ACCESS names only declared
    instructions. Still this module's reading of the shapes the emitter writes, not an
    independent parser."""
    clean = _strip_comments(text)
    entity = re.search(r"\bentity\s+(\w+)\s+is\b", clean)
    if not entity:
        return ["no entity declaration"]
    attrs, names = _attributes(clean)

    def value(attr: str) -> Optional[str]:
        return attrs.get((attr, entity.group(1)))

    waived = set(waive)
    problems = [
        f"missing attribute {attr}"
        for attr in MANDATORY_ATTRIBUTES
        if attr not in names and attr not in waived
    ]
    if not re.search(r"\buse\s+STD_1149_1_\d{4}\.all\s*;", clean):
        problems.append("no use clause for an STD_1149_1 package")

    ports = _ports(clean)
    tap_pins = {pin for attr, pin in attrs if attr in _TAP_SCAN}
    problems += [f"port {port} is not a TAP pin" for port in ports if port not in tap_pins]
    generic = re.search(r'\bPHYSICAL_PIN_MAP\s*:\s*string\s*:=\s*"(\w+)"', clean)
    constant = generic and re.search(
        rf'\bconstant\s+{generic.group(1)}\s*:\s*PIN_MAP_STRING\s*:=\s*((?:"[^"]*"|[^";])*);',
        clean,
    )
    if not constant:
        problems.append("no PIN_MAP_STRING constant for PHYSICAL_PIN_MAP's default")
    else:
        mapped = set(re.findall(r"(\w+)\s*:", _joined_strings(constant.group(1))))
        problems += [f"PIN_MAP_STRING does not map port {p}" for p in ports if p not in mapped]

    length_text, opcode_text = value("INSTRUCTION_LENGTH"), value("INSTRUCTION_OPCODE")
    if length_text is None or opcode_text is None:
        return problems
    length = int(length_text)
    opcodes = _entries(_joined_strings(opcode_text))
    problems += [
        f"missing mandatory instruction {name}"
        for name in MANDATORY_INSTRUCTIONS
        if name not in opcodes
    ]
    for name, codes in opcodes.items():
        problems += [
            f"{name} opcode {code!r} is not {length} bits"
            for code in codes
            if len(code) != length or set(code) - {"0", "1"}
        ]
    ones = "1" * length
    owners = sorted(name for name, codes in opcodes.items() if ones in codes)
    if owners != ["BYPASS"]:
        problems.append(f"the all-ones opcode belongs to {owners}, not to BYPASS alone")
    if any(code != ones for code in opcodes.get("BYPASS", [])):
        problems.append("BYPASS has an opcode that isn't all ones")
    capture = value("INSTRUCTION_CAPTURE")
    if capture is not None:
        bits = _joined_strings(capture)
        if len(bits) != length or not bits.endswith("01"):
            problems.append(f"INSTRUCTION_CAPTURE {bits!r} is not {length} bits ending in 01")
    if "IDCODE" in opcodes:
        idcode = value("IDCODE_REGISTER")
        if idcode is None:
            problems.append("IDCODE is declared but IDCODE_REGISTER is not")
        elif not re.fullmatch(r"[01]{31}1", _joined_strings(idcode)):
            problems.append("IDCODE_REGISTER is not 32 bits ending in 1")
    access = value("REGISTER_ACCESS")
    if access is not None:
        for register, instructions in _entries(_joined_strings(access)).items():
            problems += [
                f"REGISTER_ACCESS gives undeclared instruction {name} the {register} register"
                for name in instructions
                if name not in opcodes
            ]
    return problems
