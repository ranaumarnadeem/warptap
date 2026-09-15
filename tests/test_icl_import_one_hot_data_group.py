"""Round-trip tests for ICL import's OneHotDataGroup support (indirect/paged addressing plan
Phase 3): build a OneHotDataGroup by hand, emit it with render_one_hot_data_group_module(),
parse it back with import_one_hot_data_group(), and confirm the recovered group matches the
original -- mirroring test_icl_import_scan_mux.py's own round-trip style and conventions
closely. Unlike ScanMux/SIB import, always requires icl_parser_module live -- no pure-Python-
only tier exists for import anywhere in this codebase already.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from warptap.icl_emit import render_one_hot_data_group_module
from warptap.icl_import import IclImportError, import_one_hot_data_group
from warptap.icl_model import OneHotDataGroup, OneHotDataRegister, SignalBinding


def _reg(name, address, width=8, reset_value=None, writable=False, readable=False):
    return OneHotDataRegister(
        name=name,
        address=address,
        width=width,
        reset_value=reset_value,
        write_signal_bits=tuple(SignalBinding(f"{name}_in", i) for i in range(width)) if writable else (),
        read_signal_bits=tuple(SignalBinding(f"{name}_out", i) for i in range(width)) if readable else (),
    )


def _round_trip(group: OneHotDataGroup, icl_parser_module) -> OneHotDataGroup:
    icl_text = render_one_hot_data_group_module(group)
    module_name = f"warptap_one_hot_group_{group.name}"
    with tempfile.TemporaryDirectory(prefix="warptap-icl-import-") as tmpdir:
        path = Path(tmpdir) / "warptap.icl"
        path.write_text(icl_text, encoding="utf-8")
        return import_one_hot_data_group([path], module_name, icl_parser_module=icl_parser_module)


def test_write_and_read_group_round_trips_cleanly(icl_parser_module):
    group = OneHotDataGroup(
        name="regfile", address_width=2, data_width=8, writable=True, readable=True,
        registers=(
            _reg("cfg0", address=0, width=8, reset_value=0xAB, writable=True, readable=True),
            _reg("status1", address=1, width=4, writable=True, readable=True),
        ),
    )
    imported = _round_trip(group, icl_parser_module)

    assert imported.name == "regfile"
    assert imported.address_width == 2
    assert imported.data_width == 8
    assert imported.writable is True
    assert imported.readable is True
    assert len(imported.registers) == 2

    orig_by_name = {reg.name: reg for reg in group.registers}
    imp_by_name = {reg.name: reg for reg in imported.registers}
    assert set(imp_by_name) == set(orig_by_name) == {"cfg0", "status1"}
    for name, orig_reg in orig_by_name.items():
        imp_reg = imp_by_name[name]
        assert imp_reg.address == orig_reg.address
        assert imp_reg.width == orig_reg.width
        # Real, permanent round-trip limitations (see import_one_hot_data_group's own
        # docstring): ResetValue is parsed but never stored by the vendored tool at all, and
        # no per-register Source clause exists in real ICL for a host-net binding.
        assert imp_reg.reset_value is None
        assert imp_reg.write_signal_bits == ()
        assert imp_reg.read_signal_bits == ()


def test_write_only_group_round_trips_with_readable_false(icl_parser_module):
    group = OneHotDataGroup(
        name="ctrl", address_width=1, data_width=8, writable=True, readable=False,
        registers=(_reg("cfg0", address=0, width=8, writable=True),),
    )
    imported = _round_trip(group, icl_parser_module)
    assert imported.writable is True
    assert imported.readable is False


def test_read_only_group_round_trips_with_writable_false(icl_parser_module):
    group = OneHotDataGroup(
        name="stat", address_width=1, data_width=8, writable=False, readable=True,
        registers=(_reg("status0", address=0, width=8, readable=True),),
    )
    imported = _round_trip(group, icl_parser_module)
    assert imported.writable is False
    assert imported.readable is True


def test_registers_come_back_sorted_by_address_regardless_of_declaration_order(icl_parser_module):
    group = OneHotDataGroup(
        name="regfile", address_width=2, data_width=8, writable=True, readable=True,
        registers=(
            _reg("third", address=2, writable=True, readable=True),
            _reg("first", address=0, writable=True, readable=True),
            _reg("second", address=1, writable=True, readable=True),
        ),
    )
    imported = _round_trip(group, icl_parser_module)
    assert [reg.address for reg in imported.registers] == [0, 1, 2]
    assert [reg.name for reg in imported.registers] == ["first", "second", "third"]


def test_mixed_register_widths_round_trip(icl_parser_module):
    group = OneHotDataGroup(
        name="regfile", address_width=1, data_width=8, writable=True, readable=True,
        registers=(
            _reg("wide", address=0, width=8, writable=True, readable=True),
            _reg("narrow", address=1, width=3, writable=True, readable=True),
        ),
    )
    imported = _round_trip(group, icl_parser_module)
    imp_by_name = {reg.name: reg.width for reg in imported.registers}
    assert imp_by_name == {"wide": 8, "narrow": 3}


def test_module_name_without_prefix_is_rejected(icl_parser_module):
    group = OneHotDataGroup(
        name="ctrl", address_width=1, data_width=8, writable=True,
        registers=(_reg("cfg0", address=0, width=8, writable=True),),
    )
    icl_text = render_one_hot_data_group_module(group)
    with tempfile.TemporaryDirectory(prefix="warptap-icl-import-") as tmpdir:
        path = Path(tmpdir) / "warptap.icl"
        path.write_text(icl_text, encoding="utf-8")
        # Rename the module in the emitted text to something outside warptap's own convention,
        # keeping everything else identical -- confirms the rejection is about the *name*,
        # not the shape.
        renamed = icl_text.replace("warptap_one_hot_group_ctrl", "custom_ctrl_module")
        path.write_text(renamed, encoding="utf-8")
        with pytest.raises(IclImportError, match="doesn't start with"):
            import_one_hot_data_group([path], "custom_ctrl_module", icl_parser_module=icl_parser_module)


def test_nonexistent_module_name_is_rejected(icl_parser_module):
    group = OneHotDataGroup(
        name="ctrl", address_width=1, data_width=8, writable=True,
        registers=(_reg("cfg0", address=0, width=8, writable=True),),
    )
    icl_text = render_one_hot_data_group_module(group)
    with tempfile.TemporaryDirectory(prefix="warptap-icl-import-") as tmpdir:
        path = Path(tmpdir) / "warptap.icl"
        path.write_text(icl_text, encoding="utf-8")
        with pytest.raises(IclImportError, match="no module"):
            import_one_hot_data_group(
                [path], "warptap_one_hot_group_typo", icl_parser_module=icl_parser_module
            )
