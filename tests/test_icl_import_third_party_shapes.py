"""ICL import of networks written the way other tools and authors write them, not the way
:mod:`warptap.icl_emit` does: other module and port names, wrapper instances, sub-network
modules, a select register in an instance of its own, ``[MSB:0]`` registers. Each recognized
shape is checked against the graph it should give and re-imported through :func:`to_icl`; each
recognizer has a control, a near miss it must reject, with the reason pinned.

The ICL is written here, in this file: small modules in the style of IEEE 1687's examples, none
of it taken from a benchmark set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from icl_reimport import reimport

from warptap.icl_import import (
    FOREIGN_SELECT,
    MULTI_VALUE_ARM,
    NON_REGISTER_SELECT,
    NOT_A_TREE,
    NOT_ELABORATED,
    PARTIAL_REGISTER,
    REGISTER_OUTSIDE_SLOT,
    REGISTERS_IN_SERIES,
    SIB_OPENS_ON_ZERO,
    SIB_REGISTER_BEFORE_MUX,
    TOO_FEW_ARMS,
    IclImportError,
    import_icl,
)
from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SibNode,
)

# Modules the networks below are built from. Registers are declared [MSB:0], so the scan-out
# bit is bit 0.
LIBRARY = """
Module Reg8 {
    ScanInPort si;
    ScanOutPort so { Source R[0]; }
    ScanRegister R[7:0] { ScanInSource si; ResetValue 8'h00; }
}

Module Ctl4 {
    ScanInPort si;
    ScanOutPort so { Source R[0]; }
    DataOutPort q[3:0] { Source D; }
    ScanRegister R[3:0] { ScanInSource si; ResetValue 4'h0; }
    DataRegister D[3:0] { WriteDataSource R; ResetValue 4'h0; }
}

Module Gate {
    ScanInPort si;
    ScanOutPort so { Source bit; }
    SelectPort en;
    ScanInPort ret;
    ScanOutPort out { Source si; }
    LogicSignal open { bit & en; }
    ToSelectPort out_en { Source open; }
    ScanRegister bit { ScanInSource pick; ResetValue 1'b0; }
    ScanMux pick SelectedBy bit { 1'b0 : si; 1'b1 : ret; }
}

Module Sel1 {
    ScanInPort si;
    ScanOutPort so { Source R; }
    DataOutPort q { Source R; }
    ScanRegister R { ScanInSource si; ResetValue 1'b0; }
}

Module Sel2 {
    ScanInPort si;
    ScanOutPort so { Source R[0]; }
    DataOutPort q[1:0] { Source R; }
    ScanRegister R[1:0] { ScanInSource si; ResetValue 2'b00; }
}

Module Probe {
    ScanInPort si;
    ScanOutPort so { Source core.so; }
    Instance core Of Reg8 { InputPort si = si; }
}

Module Knob {
    ScanInPort si;
    ScanOutPort so { Source core.so; }
    Instance core Of Ctl4 { InputPort si = si; }
}
"""


# A top module's scan client interface, which the parser requires of one: its scan ports, a
# select, the shift, capture and update enables, a reset and a clock. _import adds it after each
# top module's ``ScanInPort tdi;``.
CLIENT_PORTS = """    SelectPort en;
    ShiftEnPort se;
    CaptureEnPort ce;
    UpdateEnPort ue;
    ResetPort rst;
    TCKPort tck;
    ScanInterface client { Port tdi; Port tdo; Port en; Port se; Port ce; Port ue; Port rst; Port tck; }
"""


def _import(tmp_path: Path, top_text: str, icl_parser_module, top: str = "Top", library: str = LIBRARY):
    path = tmp_path / "network.icl"
    top_text = top_text.replace("    ScanInPort tdi;\n", "    ScanInPort tdi;\n" + CLIENT_PORTS)
    path.write_text(library + top_text, encoding="utf-8")
    return import_icl([path], top, icl_parser_module=icl_parser_module)


def _round_trips(tmp_path: Path, graph: PhysicalGraph, root, icl_parser_module) -> None:
    """The imported graph, emitted by warptap and imported again, comes back unchanged."""
    again, again_root = reimport(graph, root, tmp_path, icl_parser_module)
    assert again == graph
    assert again_root == root


def _read(name: str, width: int) -> InstrumentNode:
    return InstrumentNode(name=name, width=width, capture_value=0, direction=InstrumentDirection.READ)


def _write(name: str, width: int) -> InstrumentNode:
    return InstrumentNode(name=name, width=width, capture_value=0, direction=InstrumentDirection.WRITE)


# -- recognized shapes ------------------------------------------------------------------------

TWO_GATES = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source g2.so; }
    Instance g1 Of Gate { InputPort si = tdi; InputPort ret = r1.so; }
    Instance r1 Of Reg8 { InputPort si = g1.out; }
    Instance g2 Of Gate { InputPort si = g1.so; InputPort ret = c1.so; }
    Instance c1 Of Ctl4 { InputPort si = g2.out; }
}
"""


def test_a_sib_with_other_names_is_recognized_by_its_structure(tmp_path, icl_parser_module):
    """``Gate`` is SIB_mux_pre under other names: its 1-bit register ``bit`` is fed by the mux
    ``pick`` it selects, ``0`` bypassing and ``1`` taking the segment. ``Reg8`` (only a
    ScanRegister) is READ; ``Ctl4`` (a DataRegister behind it) is WRITE."""
    graph, root = _import(tmp_path, TWO_GATES, icl_parser_module)
    assert graph == PhysicalGraph(chain=(
        SibNode("g1", instrument=_read("r1", 8)),
        SibNode("g2", instrument=_write("c1", 4)),
    ))
    assert root.name == "Top"
    assert [c.name for c in root.children] == ["r1", "c1"]
    _round_trips(tmp_path, graph, root, icl_parser_module)


def test_wrapper_instances_are_looked_through(tmp_path, icl_parser_module):
    """A wrapper module around an instrument register (``Probe``, ``Knob``), and one around a
    whole SIB with its instrument (``GatedProbe``), are only wires on the scan path; the
    outermost wrapper names what it wraps, and the register's module is still what makes the
    instrument WRITE."""
    top = """
Module GatedProbe {
    ScanInPort si;
    ScanOutPort so { Source g.so; }
    Instance g Of Gate { InputPort si = si; InputPort ret = p.so; }
    Instance p Of Probe { InputPort si = g.out; }
}

Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source g2.so; }
    Instance w Of GatedProbe { InputPort si = tdi; }
    Instance g2 Of Gate { InputPort si = w.so; InputPort ret = k.so; }
    Instance k Of Knob { InputPort si = g2.out; }
}
"""
    graph, root = _import(tmp_path, top, icl_parser_module)
    assert graph == PhysicalGraph(chain=(
        SibNode("w", instrument=_read("w__p", 8)),
        SibNode("g2", instrument=_write("k", 4)),
    ))
    _round_trips(tmp_path, graph, root, icl_parser_module)


def test_a_sub_network_module_behind_a_sib_becomes_a_nested_chain(tmp_path, icl_parser_module):
    """A module holding a chain of its own (``Core``), gated by a SIB in the top module: the
    SIB's segment is that chain, its slots named by their instance path."""
    top = """
Module Core {
    ScanInPort si;
    ScanOutPort so { Source g2.so; }
    Instance g1 Of Gate { InputPort si = si; InputPort ret = r1.so; }
    Instance r1 Of Reg8 { InputPort si = g1.out; }
    Instance g2 Of Gate { InputPort si = g1.so; InputPort ret = r2.so; }
    Instance r2 Of Ctl4 { InputPort si = g2.out; }
}

Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source tg.so; }
    Instance tg Of Gate { InputPort si = tdi; InputPort ret = core.so; }
    Instance core Of Core { InputPort si = tg.out; }
}
"""
    graph, root = _import(tmp_path, top, icl_parser_module)
    assert graph == PhysicalGraph(chain=(
        SibNode("tg", None, nested=(
            SibNode("core__g1", instrument=_read("core__r1", 8)),
            SibNode("core__g2", instrument=_write("core__r2", 4)),
        )),
    ))
    assert [c.name for c in root.children] == ["core__r1", "core__r2"]
    _round_trips(tmp_path, graph, root, icl_parser_module)


SCB_MUX = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source ctl.so; }
    Instance a Of Reg8 { InputPort si = tdi; }
    Instance b Of Ctl4 { InputPort si = tdi; }
    ScanMux m SelectedBy {SELECT} { 2'b00 : tdi; 2'b01 : a.so; 2'b10 : b.so; }
    Instance ctl Of Sel2 { InputPort si = m; }
}
"""


def test_a_bare_scan_mux_with_its_select_register_in_the_next_instance(tmp_path, icl_parser_module):
    """A ScanMux in the top module whose output goes into its 2-bit select register, which
    sits in an instance of its own (``ctl``, a segment control register read through a
    DataOutPort): a ScanMux slot, arms 1 and 2, value 0 a bypass."""
    graph, root = _import(tmp_path, SCB_MUX.replace("{SELECT}", "ctl.q"), icl_parser_module)
    assert graph == PhysicalGraph(chain=(
        ScanMuxNode("m", select_width=2, arms=(
            ScanArm(values=(1,), instrument=_read("a", 8)),
            ScanArm(values=(2,), instrument=_write("b", 4)),
        )),
    ))
    _round_trips(tmp_path, graph, root, icl_parser_module)


def test_a_one_bit_select_with_two_held_arms_is_a_scan_mux_not_a_sib(tmp_path, icl_parser_module):
    """A 1-bit register after a 2-way mux it selects is a SIB only when one arm is a bypass;
    with something on both arms it is a 2-arm ScanMux."""
    top = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source sc.so; }
    Instance a Of Reg8 { InputPort si = tdi; }
    Instance b Of Ctl4 { InputPort si = tdi; }
    ScanMux m SelectedBy sc.q { 1'b0 : a.so; 1'b1 : b.so; }
    Instance sc Of Sel1 { InputPort si = m; }
}
"""
    graph, root = _import(tmp_path, top, icl_parser_module)
    assert graph == PhysicalGraph(chain=(
        ScanMuxNode("m", select_width=1, arms=(
            ScanArm(values=(0,), instrument=_read("a", 8)),
            ScanArm(values=(1,), instrument=_write("b", 4)),
        )),
    ))
    _round_trips(tmp_path, graph, root, icl_parser_module)


def test_either_bit_order_reads_from_the_scan_in_end(tmp_path, icl_parser_module):
    """``[0:7]`` with its scan-out at bit 7 and ``[7:0]`` with it at bit 0 are the same 8-bit
    register to the importer."""
    top = """
Module Up8 {
    ScanInPort si;
    ScanOutPort so { Source R[7]; }
    ScanRegister R[0:7] { ScanInSource si; ResetValue 8'h00; }
}

Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source g2.so; }
    Instance g1 Of Gate { InputPort si = tdi; InputPort ret = up.so; }
    Instance up Of Up8 { InputPort si = g1.out; }
    Instance g2 Of Gate { InputPort si = g1.so; InputPort ret = down.so; }
    Instance down Of Reg8 { InputPort si = g2.out; }
}
"""
    graph, _ = _import(tmp_path, top, icl_parser_module)
    assert [slot.instrument for slot in graph.chain] == [_read("up", 8), _read("down", 8)]


# -- controls: near misses each recognizer must reject ----------------------------------------

CHIP_WITH_ACCESS_LINK = """
Module Chip {
    ScanInPort tdi;
    ScanOutPort tdo { Source g.so; }
    Instance g Of Gate { InputPort si = tdi; InputPort ret = r.so; }
    Instance r Of Reg8 { InputPort si = g.out; }
    AccessLink Tap Of STD_1149_1_2001 {
        BSDLEntity Chip;
        IJTAG_EN { ScanInterface { g; } }
    }
}
"""


def test_an_access_link_in_a_module_the_network_does_not_use_is_left_alone(tmp_path, icl_parser_module):
    """The files can hold an AccessLink elsewhere, as a benchmark set's shared files do: only
    the modules the top instantiates are elaborated."""
    graph, _ = _import(tmp_path, CHIP_WITH_ACCESS_LINK + TWO_GATES, icl_parser_module)
    assert [slot.sib_name for slot in graph.chain] == ["g1", "g2"]


def test_control_an_access_link_in_the_network(tmp_path, icl_parser_module):
    with pytest.raises(IclImportError, match=f"^{NOT_ELABORATED}: AccessLink \\(warptap's own"):
        _import(tmp_path, CHIP_WITH_ACCESS_LINK, icl_parser_module, top="Chip")


def test_control_a_register_on_the_path_outside_any_slot(tmp_path, icl_parser_module):
    """An always-on register: on the scan path, but not behind a SIB or ScanMux."""
    top = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source g.so; }
    Instance r Of Reg8 { InputPort si = tdi; }
    Instance g Of Gate { InputPort si = r.so; InputPort ret = c.so; }
    Instance c Of Ctl4 { InputPort si = g.out; }
}
"""
    with pytest.raises(IclImportError, match=f"^{REGISTER_OUTSIDE_SLOT}: 'r.R' sits in the top"):
        _import(tmp_path, top, icl_parser_module)


def test_control_two_registers_in_series_behind_one_sib(tmp_path, icl_parser_module):
    top = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source g.so; }
    Instance g Of Gate { InputPort si = tdi; InputPort ret = r2.so; }
    Instance r1 Of Reg8 { InputPort si = g.out; }
    Instance r2 Of Reg8 { InputPort si = r1.so; }
}
"""
    with pytest.raises(IclImportError, match=f"^{REGISTERS_IN_SERIES}: 'r1.R', 'r2.R' in SIB 'g'"):
        _import(tmp_path, top, icl_parser_module)


def test_control_a_register_beside_a_sib_in_a_segment(tmp_path, icl_parser_module):
    """A segment holding a SIB and a bare register: the register is outside any slot."""
    top = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source g.so; }
    Instance g Of Gate { InputPort si = tdi; InputPort ret = r.so; }
    Instance inner Of Gate { InputPort si = g.out; InputPort ret = c.so; }
    Instance c Of Ctl4 { InputPort si = inner.out; }
    Instance r Of Reg8 { InputPort si = inner.so; }
}
"""
    with pytest.raises(IclImportError, match=f"^{REGISTER_OUTSIDE_SLOT}: 'r.R' in SIB 'g'"):
        _import(tmp_path, top, icl_parser_module)


def test_control_sib_mux_post(tmp_path, icl_parser_module):
    """The register first and the mux after it, the mux's 0 arm reading the register: a SIB
    in the SIB_mux_post form, which warptap's SIB is not."""
    top = """
Module PostGate {
    ScanInPort si;
    ScanOutPort so { Source pick; }
    ScanInPort ret;
    ScanOutPort out { Source bit; }
    ScanRegister bit { ScanInSource si; ResetValue 1'b0; }
    ScanMux pick SelectedBy bit { 1'b0 : bit; 1'b1 : ret; }
}

Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source g.so; }
    Instance g Of PostGate { InputPort si = tdi; InputPort ret = r.so; }
    Instance r Of Reg8 { InputPort si = g.out; }
}
"""
    with pytest.raises(IclImportError, match=f"^{re.escape(SIB_REGISTER_BEFORE_MUX)}: 'g.bit' feeds 'g.pick', which it selects"):
        _import(tmp_path, top, icl_parser_module)


def test_control_a_sib_that_opens_on_zero(tmp_path, icl_parser_module):
    top = """
Module Gate0 {
    ScanInPort si;
    ScanOutPort so { Source bit; }
    ScanInPort ret;
    ScanOutPort out { Source si; }
    ScanRegister bit { ScanInSource pick; ResetValue 1'b1; }
    ScanMux pick SelectedBy bit { 1'b0 : ret; 1'b1 : si; }
}

Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source g.so; }
    Instance g Of Gate0 { InputPort si = tdi; InputPort ret = r.so; }
    Instance r Of Reg8 { InputPort si = g.out; }
}
"""
    with pytest.raises(IclImportError, match=f"^{SIB_OPENS_ON_ZERO}: 'g.bit' and 'g.pick'"):
        _import(tmp_path, top, icl_parser_module)


def test_control_a_mux_selected_by_part_of_its_register(tmp_path, icl_parser_module):
    """Same network as the SCB-style ScanMux, but selected by one bit of the 2-bit register."""
    top = SCB_MUX.replace("{SELECT}", "ctl.q[0]").replace("2'b00", "1'b0").replace(
        "2'b01", "1'b1"
    ).replace("2'b10 : b.so; ", "")
    with pytest.raises(IclImportError, match=f"^{FOREIGN_SELECT}: 'm' is selected by part of scan register 'ctl.R'"):
        _import(tmp_path, top, icl_parser_module)


def test_control_a_mux_selected_by_another_slots_register(tmp_path, icl_parser_module):
    """``m2`` is selected by ``sc.R``, the select register of the slot ``m1`` heads, earlier on
    the path, not by a register of its own right after it."""
    top = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source m2; }
    Instance a Of Reg8 { InputPort si = tdi; }
    ScanMux m1 SelectedBy sc.q { 1'b0 : tdi; 1'b1 : a.so; }
    Instance sc Of Sel1 { InputPort si = m1; }
    Instance g Of Gate { InputPort si = sc.so; InputPort ret = r.so; }
    Instance r Of Reg8 { InputPort si = g.out; }
    Instance b Of Ctl4 { InputPort si = g.so; }
    ScanMux m2 SelectedBy sc.q { 1'b0 : g.so; 1'b1 : b.so; }
}
"""
    with pytest.raises(IclImportError, match=f"^{FOREIGN_SELECT}: 'm2' is selected by 'sc.R'"):
        _import(tmp_path, top, icl_parser_module)


@pytest.mark.parametrize(
    "select, declaration, what",
    [
        ("sel_in", "DataInPort sel_in;", "IclDataInPort 'sel_in'"),
        ("both", "LogicSignal both { ctl.q[0] & ctl.q[1]; }", "IclLogicSignal 'both'"),
    ],
)
def test_control_a_mux_selected_by_a_port_or_logic(tmp_path, icl_parser_module, select, declaration, what):
    """A ScanMux selected from outside the scan network (the top module's own data input) or
    by logic over registers: something other than a scan register."""
    top = SCB_MUX.replace("{SELECT}", select).replace(
        "ScanInPort tdi;", f"ScanInPort tdi;\n    {declaration}"
    ).replace("2'b00", "1'b0").replace("2'b01", "1'b1").replace("2'b10 : b.so; ", "")
    with pytest.raises(IclImportError, match=f"^{NON_REGISTER_SELECT}: 'm' is selected by {what}"):
        _import(tmp_path, top, icl_parser_module)


def test_control_an_arm_selected_by_two_values(tmp_path, icl_parser_module):
    top = SCB_MUX.replace("{SELECT}", "ctl.q").replace("2'b01 : a.so;", "2'b01 | 2'b11 : a.so;")
    with pytest.raises(IclImportError, match=f"^{MULTI_VALUE_ARM}: 'ctl.R' and 'm', values \\[1, 3\\]"):
        _import(tmp_path, top, icl_parser_module)


def test_control_a_mux_whose_arms_are_all_bypass(tmp_path, icl_parser_module):
    top = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source ctl.so; }
    ScanMux m SelectedBy ctl.q { 2'b00 : tdi; 2'b01 : tdi; }
    Instance ctl Of Sel2 { InputPort si = m; }
}
"""
    with pytest.raises(IclImportError, match=f"^{TOO_FEW_ARMS}: 'ctl.R' and 'm'"):
        _import(tmp_path, top, icl_parser_module)


def test_control_a_register_on_two_paths(tmp_path, icl_parser_module):
    """``r1`` is both what ``g1`` gates and the 1 arm of a ScanMux after it: a shared segment,
    not a tree."""
    top = """
Module Top {
    ScanInPort tdi;
    ScanOutPort tdo { Source sc.so; }
    Instance g1 Of Gate { InputPort si = tdi; InputPort ret = r1.so; }
    Instance r1 Of Reg8 { InputPort si = g1.out; }
    ScanMux m SelectedBy sc.q { 1'b0 : g1.so; 1'b1 : r1.so; }
    Instance sc Of Sel1 { InputPort si = m; }
}
"""
    with pytest.raises(IclImportError, match=f"^{NOT_A_TREE}: 'r1.R' is on more than one scan path"):
        _import(tmp_path, top, icl_parser_module)


def test_control_a_path_that_leaves_a_register_early(tmp_path, icl_parser_module):
    """``R[7:0]`` scans out of bit 0; a path taken from bit 3 skips part of the register."""
    library = LIBRARY.replace("ScanOutPort so { Source R[0]; }\n    ScanRegister R[7:0]",
                              "ScanOutPort so { Source R[3]; }\n    ScanRegister R[7:0]")
    with pytest.raises(IclImportError, match=f"^{PARTIAL_REGISTER}: the path takes bit\\(s\\) \\[3\\] of 'r1.R'"):
        _import(tmp_path, TWO_GATES, icl_parser_module, library=library)
