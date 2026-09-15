"""Pure-Python tests for the ICL emitter's OneHotDataGroup support (indirect/paged addressing
plan Phase 2). No external tool involved here -- structural/textual assertions against
hand-built OneHotDataGroup/OneHotDataRegister objects, mirroring test_icl_emit_scan_mux.py's
own style closely. Live validation against the vendored icl_parser lives in
test_icl_emit_iclparser_validation.py, alongside every other external-tool-validated shape.
"""

from __future__ import annotations

import pytest

from warptap.icl_emit import IclEmitError, render_one_hot_data_group_module
from warptap.icl_model import (
    OneHotDataGroup,
    OneHotDataGroupError,
    OneHotDataRegister,
    SignalBinding,
)


def _reg(name="cfg0", address=0, width=8, reset_value=None, writable=False, readable=False):
    return OneHotDataRegister(
        name=name,
        address=address,
        width=width,
        reset_value=reset_value,
        write_signal_bits=tuple(SignalBinding(f"{name}_in", i) for i in range(width)) if writable else (),
        read_signal_bits=tuple(SignalBinding(f"{name}_out", i) for i in range(width)) if readable else (),
    )


def _group(*registers, name="regfile", address_width=2, data_width=8, writable=True, readable=True):
    if not registers:
        registers = (_reg(writable=writable, readable=readable),)
    return OneHotDataGroup(
        name=name,
        address_width=address_width,
        data_width=data_width,
        registers=registers,
        writable=writable,
        readable=readable,
    )


def test_module_block_has_no_endmodule_keyword():
    block = render_one_hot_data_group_module(_group())
    assert "endmodule" not in block
    assert block.startswith("Module warptap_one_hot_group_regfile {")
    assert block.rstrip().endswith("}")


def test_bus_ports_declared_at_module_scope_not_inside_the_group():
    """Confirmed real (Phase 0 finding #2): AddressPort/WriteEnPort/etc. are Module-level
    siblings of OneHotDataGroup, never a ``Port <signal>;`` binding inside it (the real,
    unconditionally-rejected construct -- see test_group_body_contains_only_data_register_lines
    for the precise check; a loose "Port " substring check would false-positive on
    "DataInPort DI"/"DataOutPort DO" themselves, since e.g. "...InPort DI" itself contains
    "Port DI")."""
    block = render_one_hot_data_group_module(_group())
    assert "AddressPort ADDR[1:0];" in block
    assert "WriteEnPort WEN;" in block
    assert "DataInPort DI[7:0];" in block
    assert "ReadEnPort REN;" in block
    assert "DataOutPort DO[7:0];" in block


def test_write_only_group_omits_read_ports_entirely():
    reg = _reg(writable=True, readable=False)
    block = render_one_hot_data_group_module(_group(reg, writable=True, readable=False))
    assert "WriteEnPort WEN;" in block
    assert "DataInPort DI[7:0];" in block
    assert "ReadEnPort" not in block
    assert "DataOutPort" not in block


def test_read_only_group_omits_write_ports_entirely():
    reg = _reg(writable=False, readable=True)
    block = render_one_hot_data_group_module(_group(reg, writable=False, readable=True))
    assert "ReadEnPort REN;" in block
    assert "DataOutPort DO[7:0];" in block
    assert "WriteEnPort" not in block
    assert "DataInPort" not in block


def test_group_body_contains_only_data_register_lines():
    """Regression test directly encoding Phase 0 finding #1: a future edit must never
    reintroduce a ``Port <signal>;`` or ``Instance ... Of ...;`` item inside the
    OneHotDataGroup body -- both are grammar-legal but unconditionally rejected by the real
    vendored listener ("Post source not supported" / "Instance not supported"). Checked
    line-by-line against the real illegal statement shape rather than a raw substring: a loose
    "Port " check would false-positive on this function's own "DataInPort DI"/"DataOutPort DO"
    host-binding comments, since e.g. "...InPort DI" itself contains "Port DI"."""
    block = render_one_hot_data_group_module(_group())
    group_start = block.index("OneHotDataGroup regfile")
    group_body = block[group_start:]
    for line in group_body.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("Instance ")
        assert not stripped.startswith("Port ")
    assert "DataRegister cfg0" in group_body


def test_address_value_and_reset_value_render():
    reg = _reg(name="cfg0", address=3, width=8, reset_value=0xAB, writable=True, readable=True)
    block = render_one_hot_data_group_module(_group(reg))
    assert "AddressValue 3;" in block
    assert "ResetValue 8'b10101011;" in block


def test_reset_value_omitted_when_not_given():
    reg = _reg(name="cfg0", address=0, writable=True, readable=True)
    block = render_one_hot_data_group_module(_group(reg))
    assert "ResetValue" not in block


def test_host_bindings_render_as_comments_only():
    """Like an instrument's CaptureSource/WriteDataSource, a register's real host-net binding
    has no ICL grammar mechanism to attach to per-register at all -- see
    render_one_hot_data_group_module's own docstring."""
    reg = _reg(name="cfg0", writable=True, readable=True)
    block = render_one_hot_data_group_module(_group(reg))
    assert "// DataInPort DI drives real host port bit(s) via WEN: cfg0_in[0]" in block
    assert "// DataOutPort DO fans out to real host port bit(s) via REN: cfg0_out[0]" in block


def test_module_name_uses_group_name_verbatim_not_uppercased():
    """An earlier draft arbitrarily uppercased the OneHotDataGroup's own construct name,
    copied unreflectively from the Phase 0 spike's own stylistic convention -- fixed before
    any live testing, since forcing a case transformation would silently break round-tripping.
    """
    block = render_one_hot_data_group_module(_group(name="regfile"))
    assert "Module warptap_one_hot_group_regfile {" in block
    assert "OneHotDataGroup regfile[7:0] {" in block
    assert "REGFILE" not in block


def test_zero_registers_raises_named_error():
    """render_one_hot_data_group_module calls validate_one_hot_data_group first, so this (and
    the neither-writable-nor-readable case below) surfaces as OneHotDataGroupError, not
    IclEmitError -- unlike the missing-signal_bits checks above, which validate_one_hot_data_
    group doesn't cover (it only rejects a register's signal_bits the group can't support, the
    opposite direction from the group offering a capability the register doesn't use)."""
    group = OneHotDataGroup(
        name="empty", address_width=1, data_width=8, registers=(), writable=True,
    )
    with pytest.raises(OneHotDataGroupError, match="no registers"):
        render_one_hot_data_group_module(group)


def test_writable_group_with_register_missing_write_signal_bits_raises_named_error():
    reg = _reg(name="cfg0", writable=False, readable=False)
    group = _group(reg, writable=True, readable=False)
    with pytest.raises(IclEmitError, match="no write_signal_bits"):
        render_one_hot_data_group_module(group)


def test_readable_group_with_register_missing_read_signal_bits_raises_named_error():
    reg = _reg(name="cfg0", writable=False, readable=False)
    group = _group(reg, writable=False, readable=True)
    with pytest.raises(IclEmitError, match="no read_signal_bits"):
        render_one_hot_data_group_module(group)


def test_group_neither_writable_nor_readable_raises_named_error():
    """render_one_hot_data_group_module re-validates validate_one_hot_data_group's own
    invariants belt-and-suspenders, since a OneHotDataGroup can be constructed directly
    without that function ever being called."""
    reg = _reg(name="cfg0")
    group = OneHotDataGroup(
        name="regfile", address_width=1, data_width=8, registers=(reg,),
        writable=False, readable=False,
    )
    with pytest.raises(OneHotDataGroupError, match="neither writable nor readable"):
        render_one_hot_data_group_module(group)


def test_mixed_register_widths_within_one_group_are_allowed():
    """Confirmed real (Phase 0 sub-step 5): the vendored checker accepts a register narrower
    than the shared bus -- mixed widths are a deliberate accepted shape, not an oversight."""
    wide = _reg(name="cfg0", address=0, width=8, writable=True, readable=True)
    narrow = _reg(name="status1", address=1, width=4, writable=True, readable=True)
    block = render_one_hot_data_group_module(_group(wide, narrow, data_width=8))
    assert "DataRegister cfg0[7:0]" in block
    assert "DataRegister status1[3:0]" in block


def test_address_width_one_omits_bracket_suffix():
    reg = _reg(name="cfg0", address=0, width=8, writable=True, readable=True)
    block = render_one_hot_data_group_module(_group(reg, address_width=1))
    assert "AddressPort ADDR;" in block
    assert "AddressPort ADDR[" not in block
