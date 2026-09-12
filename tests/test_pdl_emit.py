"""Pure-Python tests for the PDL emitter (implementation_plan.md §7 Stage 11). No external
tool involved here (none exists to validate against, see pdl_emit.py's own module docstring)
-- structural/textual assertions against hand-built history/PhysicalGraph objects, mirroring
tests/test_tap_ir_svf.py's/test_icl_emit.py's own style.
"""

from __future__ import annotations

import pytest

from warptap.icl_model import Alias, InstrumentNode, PhysicalGraph, SibNode
from warptap.pdl_emit import PdlEmitError, to_pdl
from warptap.pdl_history import (
    PdlApplyStmt,
    PdlReadStmt,
    PdlRunLoopStmt,
    PdlTargetStmt,
    PdlWriteStmt,
)


def _graph(*instruments):
    return PhysicalGraph(
        chain=tuple(
            SibNode(sib_name=f"sib_{instr.name}", instrument=instr) for instr in instruments
        )
    )


def test_empty_history_renders_empty_string():
    assert to_pdl([], _graph()) == ""


def test_itarget_renders_scoping_statement():
    graph = _graph(InstrumentNode(name="sensor_a", width=3, capture_value=0))
    pdl = to_pdl([PdlTargetStmt("sensor_a")], graph)
    assert pdl == "iTarget sensor_a;\n"


def test_iwrite_renders_hex_value_zero_padded_to_instrument_width():
    graph = _graph(InstrumentNode(name="sensor_a", width=16, capture_value=0))
    pdl = to_pdl([PdlWriteStmt("sensor_a", 1)], graph)
    assert pdl == "iWrite sensor_a 0x0001;\n"


def test_iwrite_hex_width_rounds_up_to_nearest_nibble():
    graph = _graph(InstrumentNode(name="sensor_a", width=9, capture_value=0))
    pdl = to_pdl([PdlWriteStmt("sensor_a", 0b101)], graph)  # 9 bits -> ceil(9/4) = 3 hex digits
    assert pdl == "iWrite sensor_a 0x005;\n"


def test_iread_renders_hex_expected_value():
    graph = _graph(InstrumentNode(name="status_b", width=8, capture_value=0))
    pdl = to_pdl([PdlReadStmt("status_b", 0xAB)], graph)
    assert pdl == "iRead status_b 0xAB;\n"


def test_irunloop_with_no_sck_port_renders_tck():
    pdl = to_pdl([PdlRunLoopStmt(100)], _graph())
    assert pdl == "iRunLoop 100 -tck;\n"


def test_irunloop_with_sck_port_renders_the_port_name():
    """Regression test: -sck used to render as a bare flag (no port name), reasoned at the
    time to have "no confirmed place to live" in PDL text. A real, independently-compiled PDL
    grammar (pdl_emit.py's own module docstring) proved that wrong -- -sck takes a mandatory
    port-name operand -- so the bare-flag form was actually invalid PDL syntax."""
    pdl = to_pdl([PdlRunLoopStmt(100, "sysclk")], _graph())
    assert pdl == "iRunLoop 100 -sck sysclk;\n"


def test_iapply_renders_bare_statement():
    pdl = to_pdl([PdlApplyStmt()], _graph())
    assert pdl == "iApply;\n"


def test_full_sequence_renders_one_line_per_statement_in_order():
    graph = _graph(InstrumentNode(name="sensor_a", width=3, capture_value=0))
    history = [
        PdlTargetStmt("sensor_a"),
        PdlWriteStmt("sensor_a", 0b101),
        PdlRunLoopStmt(10),
        PdlApplyStmt(),
    ]
    pdl = to_pdl(history, graph)
    assert pdl.splitlines() == [
        "iTarget sensor_a;",
        "iWrite sensor_a 0x5;",
        "iRunLoop 10 -tck;",
        "iApply;",
    ]


def test_iwrite_unknown_instrument_raises_named_error():
    with pytest.raises(PdlEmitError, match="ghost"):
        to_pdl([PdlWriteStmt("ghost", 1)], _graph())


def test_iread_unknown_instrument_raises_named_error():
    with pytest.raises(PdlEmitError, match="ghost"):
        to_pdl([PdlReadStmt("ghost", 1)], _graph())


def test_unsupported_statement_raises_named_error():
    with pytest.raises(PdlEmitError, match="does not support"):
        to_pdl(["not a statement"], _graph())


# --- Stage 15: named sub-field addressing (real ICL Alias) ---------------------------------


def test_iwrite_field_naming_an_alias_uses_the_aliases_own_width():
    graph = _graph(
        InstrumentNode(
            name="status_reg", width=8, capture_value=0, aliases=(Alias("mode", 4, 7),)
        )
    )
    pdl = to_pdl([PdlWriteStmt("mode", 0b101)], graph)
    assert pdl == "iWrite mode 0x5;\n"  # 4-bit alias -> 1 hex digit, not width=8's 2


def test_iread_field_naming_an_alias_uses_the_aliases_own_width():
    graph = _graph(
        InstrumentNode(
            name="status_reg", width=8, capture_value=0, aliases=(Alias("flag", 0, 0),)
        )
    )
    pdl = to_pdl([PdlReadStmt("flag", 1)], graph)
    assert pdl == "iRead flag 0x1;\n"


def test_field_naming_neither_instrument_nor_alias_raises_named_error():
    graph = _graph(InstrumentNode(name="status_reg", width=8, capture_value=0))
    with pytest.raises(PdlEmitError, match="ghost"):
        to_pdl([PdlWriteStmt("ghost", 1)], graph)
