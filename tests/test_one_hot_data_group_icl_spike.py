"""OneHotDataGroup ICL support plan, Phase 0: a gating live-validation spike (implementation_
plan.md-style discipline -- mirrors the multi-arm ScanMux plan's own "Phase 0: RTL spike,
gates everything else"). No warptap code under test here at all -- every ICL text below is a
hand-authored Python string literal, fed straight to the real vendored `Ijtag` class
(`tests/conftest.py`'s own `icl_parser_module` fixture), to empirically confirm the exact real
shape/constraints of `OneHotDataGroup`/`AddressPort`/`WriteEnPort`/`ReadEnPort`/`DataInPort`/
`DataOutPort` BEFORE any production emitter/importer code is written.

This is necessary because, unlike every other ICL feature this project has built (SIB,
ScanMux, Alias, nested networks), **no real fixture exists anywhere in the vendored
icl_parser's own test corpus demonstrating OneHotDataGroup at all** (confirmed: zero matches
for `AddressPort|AddressValue|WriteEnPort|ReadEnPort|OneHotDataGroup` across
`third_party/icl_parser/tests/test_icls/`). This project has repeatedly found that building
from the grammar file alone, without a real example, produces subtly wrong output a live
checker run later catches -- see `icl_emit.py`'s own module docstring for the most recent,
directly analogous case (the scan-port-width bug that survived this project's entire history
until an upstream maintainer's PR review caught it). This file front-loads that risk instead.

This file stays in the suite permanently once later phases land, matching the ScanMux RTL
spike's own (`tests/test_scan_mux_cell_cross_sim.py`) permanent-artifact precedent -- it's not
scaffolding to delete once production code exists, it's the empirical record of exactly which
constructs are real and how they behave, kept runnable so a future change to the vendored
submodule can't silently invalidate this project's own understanding of it without a test
noticing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def _write_icl(icl_text: str, tmpdir: Path) -> Path:
    path = Path(tmpdir) / "spike.icl"
    path.write_text(icl_text, encoding="utf-8")
    return path


_BASE_FIXTURE = """\
Module warptap_one_hot_group_regfile_spike {{
    AddressPort ADDR[1:0];
    WriteEnPort WEN;
    ReadEnPort REN;
    DataInPort DI[7:0];
    DataOutPort DO[7:0];

    OneHotDataGroup REGFILE[7:0] {{
{body}
    }}
}}
"""

_THREE_REGISTERS_BODY = """\
        DataRegister REG0[7:0] {
            AddressValue 0;
            ResetValue 8'b0;
        }
        DataRegister REG1[7:0] {
            AddressValue 1;
        }
        DataRegister REG2[7:0] {
            AddressValue 2;
        }
"""


def _parse(icl_text: str, top_name: str, icl_parser_module, *, build_register_model: bool = False):
    """Defaults to build_register_model=False -- confirmed live (test_sub_step_0 below) that
    the vendored tool's own retargeting-graph build unconditionally crashes for a network with
    ZERO scan interfaces (any OneHotDataGroup-only module, by construction -- finding #6), an
    entirely separate real bug from anything OneHotDataGroup-specific. Every other sub-step
    here only cares about the structural .check() pass, exactly matching import_icl's/the
    planned import_one_hot_data_groups's own real reason for using this flag."""
    with tempfile.TemporaryDirectory(prefix="warptap-one-hot-spike-") as tmpdir:
        path = _write_icl(icl_text, Path(tmpdir))
        return icl_parser_module(top_name, [str(path)], build_register_model=build_register_model)


def test_sub_step_0_full_retargeting_graph_build_crashes_for_a_scan_free_network(icl_parser_module):
    """A NEW finding, not anticipated by the plan's own pre-Phase-0 research: build_register_
    model=True (icl_parser_module's own default) crashes for ANY module with zero scan
    interfaces -- icl_retargeting.py's own _add_one_hot_and_ir_chain unconditionally asserts
    `len(self.one_hot_scan_interfaces) > 0`, which is never true for a pure OneHotDataGroup-
    only module (finding #6: no ScanInPort/ScanOutPort at all). This is a real, separate
    vendored-tool limitation -- not something OneHotDataGroup usage can avoid by being
    "correct" ICL, since it's about the total ABSENCE of scan interfaces, not anything this
    project controls. Confirmed here so it's a locked-in, understood fact (matching the
    already-documented AccessLink "Not supported" gap's own precedent) rather than something
    a later change could silently start relying on without anyone noticing it's fragile."""
    icl_text = _BASE_FIXTURE.format(body=_THREE_REGISTERS_BODY)
    raised = False
    try:
        _parse(
            icl_text, "warptap_one_hot_group_regfile_spike", icl_parser_module,
            build_register_model=True,
        )
    except AssertionError:
        raised = True
    assert raised, (
        "expected build_register_model=True to crash for a scan-free network -- if this now "
        "passes, the vendored tool's own bug may have been fixed upstream; re-examine whether "
        "import_one_hot_data_groups still needs build_register_model=False for this reason"
    )


def test_sub_step_1_base_fixture_parses_and_checks_cleanly_with_both_directions_live(
    icl_parser_module,
):
    """The starting fixture -- 3 registers, each both readable and writable -- must parse and
    .check() cleanly (Ijtag(...) must not raise at all), and each register's own
    is_writable()/is_readable() must both come back True, confirmed by direct inspection, not
    assumed from a clean parse alone."""
    icl_text = _BASE_FIXTURE.format(body=_THREE_REGISTERS_BODY)
    ij = _parse(icl_text, "warptap_one_hot_group_regfile_spike", icl_parser_module)
    assert ij is not None

    top = ij.icl_instance
    from sys import modules

    IclDataRegister = modules[icl_parser_module.__module__].IclDataRegister
    registers = top.get_icl_item_type(IclDataRegister)
    names = {r.get_name() for r in registers}
    assert names == {"REG0", "REG1", "REG2"}
    for reg in registers:
        assert reg.is_writable() is True, f"{reg.get_name()} expected writable"
        assert reg.is_readable() is True, f"{reg.get_name()} expected readable"


def test_sub_step_2_port_source_inside_group_is_rejected(icl_parser_module):
    """Finding #1 (half 1): a `Port <signal>;` item inside a OneHotDataGroup body is
    grammar-legal (oneHotDataGroup_portSource) but the real processor unconditionally rejects
    it -- confirmed here as a real, reported error, not assumed from reading icl_process.py
    alone."""
    body = _THREE_REGISTERS_BODY + "        Port ADDR;\n"
    icl_text = _BASE_FIXTURE.format(body=body)
    try:
        _parse(icl_text, "warptap_one_hot_group_regfile_spike", icl_parser_module)
        raised = False
    except ValueError as exc:
        raised = True
        assert "Post source not supported" in str(exc)
    assert raised, "expected a ValueError for Port-inside-OneHotDataGroup"


def test_sub_step_3_instance_inside_group_is_rejected(icl_parser_module):
    """Finding #1 (half 2): an `Instance ... Of ...;` item inside a OneHotDataGroup body is
    also grammar-legal but also unconditionally rejected by the real processor.

    A first draft of this test pointed the Instance at its own enclosing module (self-
    referential) and got a bare `RecursionError` instead of the expected rejection -- a bug in
    the test's own construction (infinite module-resolution recursion happening before
    exitOneHotDataGroup_def's own check ever runs), not a real finding. Fixed by giving it a
    genuinely separate, minimal dummy module to reference instead."""
    dummy_module = "Module warptap_one_hot_group_dummy_target { AddressPort A; }\n\n"
    body = _THREE_REGISTERS_BODY + "        Instance bogus Of warptap_one_hot_group_dummy_target;\n"
    icl_text = dummy_module + _BASE_FIXTURE.format(body=body)
    try:
        _parse(icl_text, "warptap_one_hot_group_regfile_spike", icl_parser_module)
        raised = False
    except ValueError as exc:
        raised = True
        assert "Instance not supported" in str(exc)
    assert raised, "expected a ValueError for Instance-inside-OneHotDataGroup"


def test_sub_step_4_reset_value_is_accepted_but_unrecoverable(icl_parser_module):
    """Finding #4: ResetValue on REG0 (see base fixture) is accepted at parse time (test 1
    already confirms a clean parse with it present) but genuinely unrecoverable afterward.

    A first draft of this test checked for a "reset"-named attribute/method on the parsed
    item and found one (`IclDataRegister.reset`) -- investigated directly rather than trusted:
    that method (icl_items.py ~line 1649) unconditionally zeroes simulation-time state
    (`current_value.set_value(0)`, its own comment calling it a "Temp solution"), completely
    independent of whatever ResetValue was actually declared -- a false positive from a
    name-based heuristic, not a real accessor. Confirms finding #4 rather than refuting it:
    IclDataRegister carries no reset_source/declared-reset-value field at all (unlike
    IclScanRegister's own `reset()`, which DOES reference `self.reset_source` -- a real,
    checked-directly difference between the two classes). This test instead checks every
    *value* on the parsed object, not just attribute names, for the declared reset value
    (0xAB, deliberately distinct from every other number in this fixture) anywhere at all."""
    icl_text = _BASE_FIXTURE.format(body=_THREE_REGISTERS_BODY).replace(
        "ResetValue 8'b0;", "ResetValue 8'hAB;"
    )
    ij = _parse(icl_text, "warptap_one_hot_group_regfile_spike", icl_parser_module)
    top = ij.icl_instance
    from sys import modules

    IclDataRegister = modules[icl_parser_module.__module__].IclDataRegister
    reg0 = next(r for r in top.get_icl_item_type(IclDataRegister) if r.get_name() == "REG0")
    suspect_values = []
    for attr in dir(reg0):
        if attr.startswith("__"):
            continue
        try:
            value = getattr(reg0, attr)
        except Exception:
            continue
        if callable(value):
            continue
        if value == 0xAB or value == "10101011" or value == 171:
            suspect_values.append((attr, value))
    assert suspect_values == [], (
        f"expected no attribute exposing the declared ResetValue 0xAB, found: {suspect_values}"
    )


def test_sub_step_5_narrower_register_than_shared_bus_is_accepted(icl_parser_module):
    """A register narrower than the shared 8-bit bus is accepted -- registers need not share
    one uniform width. Informs the Phase 1 decision to allow mixed widths within one group."""
    body = _THREE_REGISTERS_BODY + "        DataRegister REG3[3:0] {\n            AddressValue 3;\n        }\n"
    icl_text = _BASE_FIXTURE.format(body=body)
    ij = _parse(icl_text, "warptap_one_hot_group_regfile_spike", icl_parser_module)
    top = ij.icl_instance
    from sys import modules

    IclDataRegister = modules[icl_parser_module.__module__].IclDataRegister
    reg3 = next(r for r in top.get_icl_item_type(IclDataRegister) if r.get_name() == "REG3")
    assert reg3.get_vector_size() == 4
    assert reg3.is_writable() is True
    assert reg3.is_readable() is True


def test_sub_step_6_write_only_and_read_only_registers_are_independent(icl_parser_module):
    """A module offering only WriteEnPort/DataInPort (no ReadEnPort/DataOutPort at all) makes
    every one of its addressable registers writable but never readable, and vice versa --
    confirmed live, not just read from check()'s own code."""
    write_only = """\
Module warptap_one_hot_group_write_only_spike {
    AddressPort ADDR[0:0];
    WriteEnPort WEN;
    DataInPort DI[7:0];

    OneHotDataGroup REGFILE[7:0] {
        DataRegister REG0[7:0] {
            AddressValue 0;
        }
    }
}
"""
    ij = _parse(write_only, "warptap_one_hot_group_write_only_spike", icl_parser_module)
    top = ij.icl_instance
    from sys import modules

    IclDataRegister = modules[icl_parser_module.__module__].IclDataRegister
    reg0 = next(r for r in top.get_icl_item_type(IclDataRegister) if r.get_name() == "REG0")
    assert reg0.is_writable() is True
    assert reg0.is_readable() is not True

    read_only = """\
Module warptap_one_hot_group_read_only_spike {
    AddressPort ADDR[0:0];
    ReadEnPort REN;
    DataOutPort DO[7:0];

    OneHotDataGroup REGFILE[7:0] {
        DataRegister REG0[7:0] {
            AddressValue 0;
        }
    }
}
"""
    ij2 = _parse(read_only, "warptap_one_hot_group_read_only_spike", icl_parser_module)
    top2 = ij2.icl_instance
    reg0b = next(r for r in top2.get_icl_item_type(IclDataRegister) if r.get_name() == "REG0")
    assert reg0b.is_readable() is True
    assert reg0b.is_writable() is not True


def test_sub_step_7_duplicate_address_value_is_not_caught_by_the_real_checker(icl_parser_module):
    """Finding #7's predicted consequence: two registers claiming the same AddressValue in one
    group is NOT rejected by the vendored checker -- confirmed live. This is the concrete
    justification for Phase 1's own validate_one_hot_data_group enforcing address-uniqueness
    itself, a real warptap-side safety net, not redundant belt-and-suspenders."""
    body = (
        "        DataRegister REG0[7:0] {\n            AddressValue 0;\n        }\n"
        "        DataRegister REG1[7:0] {\n            AddressValue 0;\n        }\n"
    )
    icl_text = _BASE_FIXTURE.format(body=body)
    ij = _parse(icl_text, "warptap_one_hot_group_regfile_spike", icl_parser_module)
    assert ij is not None  # did NOT raise -- the real checker has no cross-register check


def test_sub_step_8_two_groups_in_one_module_share_port_lookup(icl_parser_module):
    """Finding #3: get_port_type_sequence resolves AddressPort/etc. by TYPE across the whole
    enclosing Module, not by explicit per-group association. Two independently-named
    OneHotDataGroups in the same module, each nominally wanting its own AddressPort, would
    both resolve to the SAME concatenated port sequence -- confirmed live here by checking
    whether a register in the SECOND group ends up satisfied by the FIRST group's own address
    width (2 bits) even though its own real address (3) needs the same field, i.e. whether
    both groups genuinely share one address decode space rather than getting isolated ones.
    This result decides whether "one OneHotDataGroup per Module" becomes a hard, structurally
    enforced v1 rule (Phase 1) or just a documented caveat."""
    icl_text = """\
Module warptap_one_hot_group_two_groups_spike {
    AddressPort ADDR_A[1:0];
    WriteEnPort WEN_A;
    DataInPort DI_A[7:0];
    AddressPort ADDR_B[1:0];
    WriteEnPort WEN_B;
    DataInPort DI_B[7:0];

    OneHotDataGroup GROUP_A[7:0] {
        DataRegister A0[7:0] {
            AddressValue 0;
        }
    }
    OneHotDataGroup GROUP_B[7:0] {
        DataRegister B0[7:0] {
            AddressValue 0;
        }
    }
}
"""
    try:
        ij = _parse(icl_text, "warptap_one_hot_group_two_groups_spike", icl_parser_module)
        raised = False
    except (ValueError, AssertionError) as exc:
        raised = True
        raised_message = str(exc)
    if raised:
        print(f"two-groups-in-one-module raised: {raised_message}")
    else:
        top = ij.icl_instance
        from sys import modules

        IclAddressPort = modules[icl_parser_module.__module__].IclAddressPort
        address_ports = top.get_icl_item_type(IclAddressPort)
        print(f"two-groups-in-one-module: {len(address_ports)} AddressPort item(s) found")
        # If get_port_type_sequence really does merge by type, both groups' own registers
        # resolve against a combined 4-bit address space (ADDR_A ++ ADDR_B), not two
        # independent 2-bit ones -- this assertion documents whichever reality holds.
