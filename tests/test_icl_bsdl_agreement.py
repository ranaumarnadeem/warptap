"""The ICL ``AccessLink`` and the BSDL file must agree: this is the whole point of pairing them.

An ICL grammar accepts any identifier as the instruction name (that is how the old, wrong
``wdr_select`` slipped through), so only a cross-check against the BSDL can catch a name the TAP
does not have. Here the ICL's ``BSDLEntity`` must equal the BSDL's entity, and the instruction
its block names must be declared in the BSDL's ``INSTRUCTION_OPCODE`` -- and be one the RTL routes
to the network: one ``REGISTER_ACCESS`` gives no register of its own
(``tests/test_bsdl_emit_cross_sim.py`` proves what each declared opcode does). Both for the
default TAP (EXTEST) and for one with IJTAG_ACCESS, where EXTEST selects BYPASS.
"""

from __future__ import annotations

import re

import pytest
from bsdl_reader import Bsdl, parse_bsdl
from test_icl_emit_golden import CASES

from warptap.bsdl_emit import to_bsdl
from warptap.icl_emit import to_icl
from warptap.tap_model import OPCODE_IJTAG_ACCESS

_ENTITY = re.compile(r"BSDLEntity (\w+);")
_INSTRUCTION = re.compile(r"^\s+(\w+) \{ ScanInterface \{", re.MULTILINE)


def _network_instructions(bsdl: Bsdl) -> set[str]:
    given = {name for names in bsdl.register_access.values() for name in names}
    return set(bsdl.opcodes) - {"IDCODE", "BYPASS"} - given


def _bsdl(entity: str, ijtag_access: bool) -> Bsdl:
    opcode = OPCODE_IJTAG_ACCESS if ijtag_access else None
    return parse_bsdl(to_bsdl(entity, tck_max_freq_hz=10e6, ijtag_access_opcode=opcode))


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("entity", [None, "chip_tap"])
@pytest.mark.parametrize("ijtag_access", [False, True])
def test_access_link_entity_and_instruction_are_declared_in_the_bsdl(case, entity, ijtag_access):
    graph, root = CASES[case]()
    icl = to_icl(
        graph, root, include_access_link=True, bsdl_entity_name=entity, ijtag_access=ijtag_access
    )
    bsdl = _bsdl(entity or root.name, ijtag_access)

    (icl_entity,) = _ENTITY.findall(icl)
    (icl_instruction,) = _INSTRUCTION.findall(icl)
    assert icl_entity == bsdl.entity
    assert icl_instruction in bsdl.opcodes
    assert icl_instruction in _network_instructions(bsdl)


def test_the_named_instruction_is_opcode_0000_as_the_bsdl_declares():
    graph, root = CASES["flat_read_write"]()
    (instruction,) = _INSTRUCTION.findall(to_icl(graph, root, include_access_link=True))
    assert parse_bsdl(to_bsdl("chip", tck_max_freq_hz=10e6)).opcodes[instruction] == ["0000"]


@pytest.mark.parametrize("ijtag_access", [False, True])
def test_a_mismatched_pair_is_caught(ijtag_access):
    """Control: an ICL for one TAP against the BSDL of the other fails the check above -- the
    EXTEST ICL because that BSDL gives EXTEST the BYPASS register, the IJTAG_ACCESS ICL because
    the default BSDL has no such instruction."""
    graph, root = CASES["flat_read_write"]()
    icl = to_icl(graph, root, include_access_link=True, ijtag_access=ijtag_access)
    (instruction,) = _INSTRUCTION.findall(icl)
    assert instruction not in _network_instructions(_bsdl(root.name, not ijtag_access))
