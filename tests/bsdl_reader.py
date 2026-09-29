"""A minimal, test-local reader for the BSDL that ``warptap.bsdl_emit.to_bsdl`` emits.

Deliberately NOT a general BSDL parser and NOT an independent oracle: it exists so tests can make
claims from the *emitted text* (what a tool reading the file would believe) rather than from
``warptap.tap_model``'s constants, which the emitter itself was built from. It understands exactly
the attribute shapes this emitter writes and raises ``ValueError`` on anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

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


def parse_bsdl(text: str) -> Bsdl:
    clean = _strip_comments(text)
    entity = re.search(r"\bentity\s+(\w+)\s+is\b", clean)
    if not entity:
        raise ValueError("no entity declaration")
    end = re.search(r"\bend\s+(\w+)\s*;", clean)

    port_block = re.search(r"\bport\s*\((.*?)\)\s*;", clean, re.DOTALL)
    if not port_block:
        raise ValueError("no port clause")
    ports = {
        m.group(1): m.group(2)
        for m in re.finditer(r"(\w+)\s*:\s*(in|out)\s+bit", port_block.group(1))
    }

    attrs: Dict[Tuple[str, str], str] = {}
    names: List[str] = []
    for m in _ATTRIBUTE.finditer(clean):
        attrs[(m.group(1), m.group(2))] = m.group(4).strip()
        names.append(m.group(1))

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
