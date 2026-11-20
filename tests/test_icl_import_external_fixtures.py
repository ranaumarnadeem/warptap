"""Secondary validation for ICL import (implementation_plan.md §7 Stage 12): run
``import_icl()`` against the vendored ``icl_parser``'s own real fixture corpus
(``third_party/icl_parser/tests/test_icls/*.icl``) -- files this project didn't author, using
genuinely different ICL shapes than anything ``icl_emit.py`` produces.

The importer recognizes SIBs and ScanMuxes by their structure, not by warptap's module names,
but **no fixture in this corpus holds a network warptap's model can represent**, so each is
expected to be rejected, for the specific construct that rules it out: a bare instrument
register with no SIB or ScanMux around it, a ScanMux selected by part of another register or by
an IR decode, or ICL the parser itself rejects. Each assertion pins that reason (see
``icl_import.REJECTION_REASONS``), so a regression shows as a different reason, not as a loose
pass/fail. The ITC'16 benchmark networks are checked the same way in
``test_icl_import_itc16.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from warptap.icl_import import (
    FOREIGN_SELECT,
    INVALID_ICL,
    NON_REGISTER_SELECT,
    REGISTER_OUTSIDE_SLOT,
    IclImportError,
    import_icl,
)


def _fixture(icl_parser_dir: Path, *parts: str) -> Path:
    return icl_parser_dir / "tests" / "test_icls" / Path(*parts)


@pytest.mark.parametrize(
    "top, register",
    [("SReg", "SR"), ("WrappedInstr", "reg8.SR"), ("WrappedScan", "reg.SR")],
)
def test_a_bare_instrument_is_a_register_outside_any_slot(icl_parser_dir, icl_parser_module, top, register):
    """SReg is a leaf register module; WrappedInstr and WrappedScan wrap one (IEEE 1687-2014
    Appendix E's Instrument+SReg pair). With no SIB or ScanMux around the register, warptap's
    model has nowhere to put it: a chain holds only SIBs and ScanMuxes."""
    path = _fixture(icl_parser_dir, "Instruments.icl")
    with pytest.raises(IclImportError, match=f"^{REGISTER_OUTSIDE_SLOT}: '{register}' sits"):
        import_icl([path], top, icl_parser_module=icl_parser_module)


def test_sreg_to_dreg_has_a_real_bug_in_the_fixture_itself(icl_parser_dir, icl_parser_module):
    """SRegToDReg's DataRegister clause references the lowercase parameter ``$size`` while the
    module only declares ``Parameter Size`` (capital S), and icl_parser's parameter lookup is
    case-sensitive. The parser's own error is passed on as invalid ICL."""
    path = _fixture(icl_parser_dir, "Instruments.icl")
    with pytest.raises(IclImportError, match=f"^{INVALID_ICL}: Reference to parameter size not found"):
        import_icl([path], "SRegToDReg", icl_parser_module=icl_parser_module)


def test_scan_mux_001_fails_the_parsers_own_check(icl_parser_dir, icl_parser_module):
    """Bare ScanMux edge cases with ports the parser's own top-module check doesn't account
    for: its assert fails before warptap sees the network, and the error says where."""
    path = _fixture(icl_parser_dir, "scan_mux_001.icl")
    with pytest.raises(IclImportError, match=f"^{INVALID_ICL}: the vendored parser's own consistency check \\(an assert at icl_items.py:"):
        import_icl([path], "scan_mux_001", icl_parser_module=icl_parser_module)


def test_scan_mux_002_selects_by_part_of_another_register(icl_parser_dir, icl_parser_module):
    """scan_mux_002 instantiates SReg from Instruments.icl (a cross-file reference, so both
    files go in together); its mux_3 is selected by part of that register, not by a select
    register of its own."""
    paths = [
        _fixture(icl_parser_dir, "scan_mux_002.icl"),
        _fixture(icl_parser_dir, "Instruments.icl"),
    ]
    with pytest.raises(
        IclImportError, match=f"^{FOREIGN_SELECT}: 'mux_3' is selected by part of scan register 'reg8.SR'"
    ):
        import_icl(paths, "scan_mux_002", icl_parser_module=icl_parser_module)


def test_icl_syntax_6_reset_value_fixture_is_a_register_outside_any_slot(icl_parser_dir, icl_parser_module):
    path = _fixture(icl_parser_dir, "benchmarks_conv", "ICL", "test_icl_syntax_6.icl")
    with pytest.raises(IclImportError, match=f"^{REGISTER_OUTSIDE_SLOT}: 'SR_0' sits"):
        import_icl([path], "test_icl_syntax_6", icl_parser_module=icl_parser_module)


def test_icl_syntax_7_ir_decoded_dr_mux_tap_selects_through_the_ir(icl_parser_dir, icl_parser_module):
    """A classic IR-decoded DR-mux JTAG TAP (``ScanMux DRmux SelectedBy IR{...}``): its IR mux
    is selected by the TAP's IR-select, which warptap's model, a network behind a TAP rather
    than the TAP itself, has no place for."""
    path = _fixture(icl_parser_dir, "benchmarks_conv", "ICL", "test_icl_syntax_7.icl")
    with pytest.raises(
        IclImportError, match=f"^{NON_REGISTER_SELECT}: 'IRMux' is selected by IclToIrSelectPort"
    ):
        import_icl([path], "test_icl_syntax_7", icl_parser_module=icl_parser_module)
