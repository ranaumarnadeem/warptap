"""Integration test for the PDL emitter (implementation_plan.md §7 Stage 11): feed a REAL
``PDLInterpreter``-driven history through ``to_pdl()``, mirroring
tests/test_tap_ir_emitters_integration.py's own end-to-end style.

**Self-consistency-only, not an independent-tool validation** -- no independent PDL parser
exists to validate against (see pdl_emit.py's own module docstring for the exhaustive search
and the direct confirmation that the vendored icl_parser's own pdl_parser is unwired). What
this file actually proves: the high-level ``history`` record (what ``to_pdl`` renders) and the
low-level ``self.program`` ops (what actually gets shifted) agree with EACH OTHER for the same
``iApply`` call -- i.e. ``to_pdl()`` isn't silently rendering a stale or wrong value. This is
weaker than Stage 7's OpenOCD/Jam-player validation or Stage 10's icl_parser validation, which
both check conformance against code this project didn't write; here there is no such oracle,
stated permanently and explicitly rather than glossed over.
"""

from __future__ import annotations

from warptap.pdl_emit import to_pdl
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import bits_from_int, bits_to_int

_SPECS = [
    InstrumentSpec("sensor_a", width=3, capture_value=0b101),
    InstrumentSpec("sensor_b", width=2, capture_value=0b11),
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
