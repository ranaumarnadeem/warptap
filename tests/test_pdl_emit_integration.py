"""Integration test for the PDL emitter (implementation_plan.md §7 Stage 11): feed a REAL
``PDLInterpreter``-driven history through ``to_pdl()``, mirroring
tests/test_tap_ir_emitters_integration.py's own end-to-end style.

**Self-consistency-only here, not an independent-tool validation** -- independent *grammar*
validation (a real, compiled PDL parser) lives in ``test_pdl_emit_grammar_validation.py``; see
``pdl_emit.py``'s own module docstring for what that does and doesn't prove (grammar
correctness, not retargeting-semantics correctness -- no tool checks the latter for any
format). What this file actually proves: the high-level ``history`` record (what ``to_pdl``
renders) and the low-level ``self.program`` ops (what actually gets shifted) agree with EACH
OTHER for the same ``iApply`` call -- i.e. ``to_pdl()`` isn't silently rendering a stale or
wrong value.
"""

from __future__ import annotations

from warptap.icl_model import Alias
from warptap.pdl_emit import to_pdl
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import PulsePin, bits_from_int, bits_to_int

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
]

_ALIASED_SPECS = [
    InstrumentSpec(
        "status_reg", width=4, capture_value=0, aliases=(Alias("lo", 0, 1), Alias("hi", 2, 3))
    ),
]


def _interpreter():
    graph, root = build_sib_plan(_SPECS)
    return graph, PDLInterpreter(graph, root)


def test_rendered_iwrite_value_matches_actual_phase2_shifted_content_bits():
    graph, pdl = _interpreter()
    pdl.iTarget("sensor_a")  # width 3, chained first (nearest TDI)
    pdl.iWrite(0b101)
    ops = pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert "iWrite sensor_a 0x5;" in pdl_text

    # Decode phase 2's actual tdi back to position order and pull out sensor_a's own 3 content
    # bits (LSB-first, matching test_pdl_interpreter.py's own established decoding convention).
    phase2 = ops[4]
    fed_bits = bits_from_int(phase2.tdi, phase2.bits)
    position_bits = list(reversed(fed_bits))
    content_bits = position_bits[0:3]
    assert bits_to_int(content_bits) == 0b101


def test_rendered_iread_expected_matches_actual_phase2_tdo_content_bits():
    graph, pdl = _interpreter()
    pdl.iTarget("sensor_b")  # width 2, chained second
    pdl.iRead(0b10)
    ops = pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert "iRead sensor_b 0x2;" in pdl_text

    phase2 = ops[4]
    fed_tdo_bits = bits_from_int(phase2.tdo, phase2.bits)
    position_tdo = list(reversed(fed_tdo_bits))
    # sensor_a stays closed (1 select bit) ahead of sensor_b's own 2 content bits.
    content_bits = position_tdo[1:3]
    assert bits_to_int(content_bits) == 0b10


def test_full_sequence_renders_and_self_consistency_holds_across_two_applies():
    graph, pdl = _interpreter()
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b011)
    pdl.iRunLoop(5)
    pdl.iApply()
    pdl.iTarget("sensor_b")
    pdl.iWrite(0b01)
    pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert pdl_text.splitlines() == [
        "iTarget sensor_a;",
        "iWrite sensor_a 0x3;",
        "iRunLoop 5 -tck;",
        "iApply;",
        "iTarget sensor_b;",
        "iWrite sensor_b 0x1;",
        "iApply;",
    ]


def test_rendered_alias_field_write_matches_actual_phase2_shifted_content_bits():
    """Stage 15: a real, PDLInterpreter-driven field= write, rendered by its alias name, and
    self-consistency-checked against the actual phase 2 bits the SAME way the whole-instrument
    tests above already do."""
    graph, root = build_sib_plan(_ALIASED_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("status_reg")
    pdl.iWrite(0b11, field="hi")  # bits[3:2]
    ops = pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert "iTarget status_reg;" in pdl_text
    assert "iWrite hi 0x3;" in pdl_text  # "hi", not "status_reg" -- the alias's own name

    phase2 = ops[4]
    fed_bits = bits_from_int(phase2.tdi, phase2.bits)
    position_bits = list(reversed(fed_bits))
    content_bits = position_bits[0:4]  # the whole 4-bit instrument's own content
    assert bits_to_int(content_bits) == 0b1100  # hi=0b11 landed at bits[3:2], lo left at 0


def test_irunloop_with_sck_port_produces_pulsepin_and_renders_sck():
    graph, root = build_sib_plan(_SPECS)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iWrite(0b101)
    pdl.iApply()
    pdl.iRunLoop(3, sck_port="sysclk")

    assert isinstance(pdl.program[-1], PulsePin)
    assert pdl.program[-1] == PulsePin("sysclk", 3)

    pdl_text = to_pdl(pdl.history, graph)
    assert pdl_text.splitlines()[-1] == "iRunLoop 3 -sck sysclk;"
