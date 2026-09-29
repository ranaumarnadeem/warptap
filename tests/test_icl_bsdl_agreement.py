"""The ICL ``AccessLink`` and the BSDL file must agree: this is the whole point of pairing them.

An ICL grammar accepts any identifier as the instruction name (that is how the old, wrong
``wdr_select`` slipped through), so only a cross-check against the BSDL can catch a name the TAP
does not have. Here the ICL's ``BSDLEntity`` must equal the BSDL's entity, and the instruction
its block names must be declared in the BSDL's ``INSTRUCTION_OPCODE`` -- and be one the RTL routes
to the network (``tests/test_bsdl_emit_cross_sim.py`` proves what each declared opcode does).
"""

from __future__ import annotations

import re

import pytest
from bsdl_reader import parse_bsdl
from test_icl_emit_golden import CASES

from warptap.bsdl_emit import to_bsdl
from warptap.icl_emit import to_icl

_ENTITY = re.compile(r"BSDLEntity (\w+);")
_INSTRUCTION = re.compile(r"^\s+(\w+) \{ ScanInterface \{", re.MULTILINE)


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("entity", [None, "chip_tap"])
def test_access_link_entity_and_instruction_are_declared_in_the_bsdl(case, entity):
    graph, root = CASES[case]()
    icl = to_icl(graph, root, include_access_link=True, bsdl_entity_name=entity)
    bsdl = parse_bsdl(to_bsdl(entity or root.name, tck_max_freq_hz=10e6))

    (icl_entity,) = _ENTITY.findall(icl)
    (icl_instruction,) = _INSTRUCTION.findall(icl)
    assert icl_entity == bsdl.entity
    assert icl_instruction in bsdl.opcodes
    network_instructions = set(bsdl.opcodes) - {"IDCODE", "BYPASS"}
    assert icl_instruction in network_instructions


def test_the_named_instruction_is_opcode_0000_as_the_bsdl_declares():
    graph, root = CASES["flat_read_write"]()
    (instruction,) = _INSTRUCTION.findall(to_icl(graph, root, include_access_link=True))
    assert parse_bsdl(to_bsdl("chip", tck_max_freq_hz=10e6)).opcodes[instruction] == ["0000"]
