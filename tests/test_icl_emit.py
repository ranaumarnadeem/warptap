"""Pure-Python tests for the ICL emitter (implementation_plan.md §7 Stage 10). No external
tool involved here -- structural/textual assertions against hand-built PhysicalGraph/
ModuleInstance objects, mirroring tests/test_tap_ir_svf.py's own style.
"""

from __future__ import annotations

import pytest

from warptap.icl_emit import (
    IclEmitError,
    render_instrument_module,
    render_sib_instances,
    render_sib_module_type,
    to_icl,
)
from warptap.icl_model import (
    Alias,
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    SibNode,
    SignalBinding,
)


def _read_instrument(name="sensor_a", width=3, capture_value=0b101, signal_bits=(), aliases=()):
    return InstrumentNode(
        name=name, width=width, capture_value=capture_value, signal_bits=signal_bits,
        aliases=aliases,
    )


def _write_instrument(name="ctrl", width=1, capture_value=0, signal_bits=None, aliases=()):
    if signal_bits is None:
        signal_bits = (SignalBinding("bist_start"),)
    return InstrumentNode(
        name=name,
        width=width,
        capture_value=capture_value,
        direction=InstrumentDirection.WRITE,
        signal_bits=signal_bits,
        aliases=aliases,
    )


def test_module_blocks_have_no_endmodule_keyword():
    """A wrong Verilog-family instinct this project has never had to avoid before -- real ICL
    is brace-delimited with no closing keyword at all."""
    sib_block = render_sib_module_type()
    assert "endmodule" not in sib_block
    assert sib_block.startswith("Module ")
    assert sib_block.rstrip().endswith("}")


def test_sib_module_type_has_classifiable_select_port():
    """Confirmed directly against the vendored icl_parser this session: a ScanInterface with
    no TMSPort/ShiftEnPort/SelectPort raises a real, named error. SelectPort must be present."""
    block = render_sib_module_type()
    assert "SelectPort SEL;" in block
    assert "ToSelectPort toSEL" in block


def test_read_instrument_fixed_stub_has_no_fabricated_capture_source():
    block = render_instrument_module(_read_instrument(signal_bits=()))
    # A real CaptureSource STATEMENT (not just the word appearing in an explanatory comment)
    # would be a line starting with "CaptureSource" after stripping leading whitespace.
    assert not any(line.strip().startswith("CaptureSource") for line in block.splitlines())
    assert "capture_value=0x5" in block


def test_read_instrument_with_signal_bits_has_real_capture_source():
    block = render_instrument_module(
        _read_instrument(signal_bits=(SignalBinding("status_out"),))
    )
    assert "CaptureSource DR[2:0];" in block
    assert "status_out[0]" in block


def test_write_instrument_uses_datawriteregister_pair_not_scanregister_writedatasource():
    """WriteDataSource/WriteEnSource belong to DataRegister, never ScanRegister -- confirmed
    against the real vendored icl_parser grammar this session (an assumption this emitter got
    wrong on a first pass and corrected against the real tool)."""
    block = render_instrument_module(_write_instrument())
    assert "DataRegister DR[0:0]" in block
    assert "WriteDataSource SR[0:0];" in block
    assert "ScanRegister SR[0:0]" in block
    # ScanRegister itself must NOT carry WriteDataSource/WriteEnSource.
    scan_reg_block = block.split("DataRegister")[0]
    assert "WriteDataSource" not in scan_reg_block
    assert "WriteEnSource" not in scan_reg_block


def test_write_instrument_with_no_signal_bits_raises_named_error():
    with pytest.raises(IclEmitError, match="signal_bits"):
        render_instrument_module(_write_instrument(signal_bits=()))


def test_write_instrument_has_dataoutport():
    block = render_instrument_module(_write_instrument())
    assert "DataOutPort DO[0:0]" in block


def test_sib_instances_chain_tdi_side_first():
    graph = PhysicalGraph(
        chain=(
            SibNode(sib_name="sib_a", instrument=_read_instrument(name="a", width=1)),
            SibNode(sib_name="sib_b", instrument=_read_instrument(name="b", width=1)),
        )
    )
    lines = render_sib_instances(graph)
    joined = "\n".join(lines)
    assert "InputPort SI = tdi;" in joined  # first slot reads from TDI
    assert "InputPort SI = warptap_sib_a.SO;" in joined  # second slot chains from the first


def test_nested_sib_raises_named_error():
    graph = PhysicalGraph(
        chain=(
            SibNode(
                sib_name="sib_a",
                instrument=_read_instrument(name="a", width=1),
                nested=(SibNode(sib_name="sib_nested", instrument=None),),
            ),
        )
    )
    with pytest.raises(IclEmitError, match="nested"):
        render_sib_instances(graph)


def test_instrument_less_slot_raises_named_error():
    graph = PhysicalGraph(chain=(SibNode(sib_name="sib_a", instrument=None),))
    with pytest.raises(IclEmitError, match="no instrument"):
        render_sib_instances(graph)


def test_to_icl_top_module_named_after_root():
    graph = PhysicalGraph(
        chain=(SibNode(sib_name="sib_a", instrument=_read_instrument(name="a", width=1)),)
    )
    root = ModuleInstance(name="my_real_chip", children=(ModuleInstance(name="a"),))
    text = to_icl(graph, root)
    assert "Module my_real_chip {" in text


def test_to_icl_access_link_optional():
    graph = PhysicalGraph(
        chain=(SibNode(sib_name="sib_a", instrument=_read_instrument(name="a", width=1)),)
    )
    root = ModuleInstance(name="chip", children=(ModuleInstance(name="a"),))
    with_link = to_icl(graph, root, include_access_link=True)
    without_link = to_icl(graph, root, include_access_link=False)
    assert "AccessLink" in with_link
    assert "STD_1149_1_2001" in with_link
    assert "AccessLink" not in without_link


def test_to_icl_access_link_wdr_select_has_no_activesignals():
    """Confirmed real: ActiveSignals belongs to wir_select (naming the IR-decode signal), not
    wdr_select -- an earlier draft of this emitter incorrectly copied it into wdr_select too."""
    graph = PhysicalGraph(
        chain=(SibNode(sib_name="sib_a", instrument=_read_instrument(name="a", width=1)),)
    )
    root = ModuleInstance(name="chip", children=(ModuleInstance(name="a"),))
    text = to_icl(graph, root, include_access_link=True)
    wdr_line = next(line for line in text.splitlines() if "wdr_select" in line)
    assert "ActiveSignals" not in wdr_line


def test_to_icl_each_distinct_instrument_type_rendered_once():
    """Two SIBs gating the SAME instrument spec by name would be a build_sib_plan-level
    impossibility (duplicate names raise there), but to_icl() should still only emit one
    Module block per distinct instrument name it actually sees."""
    graph = PhysicalGraph(
        chain=(
            SibNode(sib_name="sib_a", instrument=_read_instrument(name="a", width=1)),
            SibNode(sib_name="sib_b", instrument=_read_instrument(name="b", width=2)),
        )
    )
    root = ModuleInstance(
        name="chip", children=(ModuleInstance(name="a"), ModuleInstance(name="b"))
    )
    text = to_icl(graph, root)
    assert text.count("Module warptap_instr_a ") == 1
    assert text.count("Module warptap_instr_b ") == 1


# --- Stage 15: named sub-field addressing (real ICL Alias) ---------------------------------


def test_read_instrument_alias_references_dr_with_rebased_lhs_range():
    instrument = _read_instrument(
        name="status_reg", width=8, capture_value=0, aliases=(Alias("mode", 4, 7),)
    )
    text = render_instrument_module(instrument)
    # LHS rebased to [3:0] (a 4-bit alias, 0-indexed) even though the RHS references the
    # register's own absolute bit positions [7:4] -- confirmed real ICL convention.
    assert "Alias mode[3:0] = DR[7:4];" in text


def test_write_instrument_alias_references_sr_not_dr():
    instrument = _write_instrument(
        name="ctrl", width=4, capture_value=0, aliases=(Alias("lo", 0, 1),)
    )
    text = render_instrument_module(instrument)
    assert "Alias lo[1:0] = SR[1:0];" in text
    assert "Alias lo[1:0] = DR[1:0];" not in text


def test_single_bit_alias_renders_bare_name_and_index_no_range():
    """Matches a real fixture's own convention (Alias okay = DO[0];) -- a bare name/index,
    no [0:0] range, for a 1-bit alias."""
    instrument = _read_instrument(
        name="status_reg", width=4, capture_value=0, aliases=(Alias("flag", 2, 2),)
    )
    text = render_instrument_module(instrument)
    assert "Alias flag = DR[2];" in text


def test_instrument_with_no_aliases_renders_no_alias_declaration_at_all():
    instrument = _read_instrument(name="sensor_a", width=3)
    text = render_instrument_module(instrument)
    assert "Alias" not in text
