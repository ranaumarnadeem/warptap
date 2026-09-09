"""Secondary validation for ICL import (implementation_plan.md §7 Stage 12): run
``import_icl()`` against the vendored ``icl_parser``'s own real fixture corpus
(``third_party/icl_parser/tests/test_icls/*.icl``) -- files this project didn't author, using
genuinely different ICL shapes than anything ``icl_emit.py`` produces.

**Every fixture in this corpus is expected to be REJECTED, with a specific, pinned reason --
not a loose pass/fail.** This is a real, honest finding, not a weak test: the whole corpus (a
parametrized generic-instrument library, bare ``ScanMux`` edge cases, an IR-decoded DR-mux TAP)
uses shapes genuinely different from warptap's own canonical SIB-network convention
(``icl_import.py``'s own module docstring explains why that's the deliberately narrow scope
Stage 12 was approved for). Each assertion below pins the *specific* reason -- confirms
``import_icl()`` correctly recognizes what ISN'T a warptap-shaped network, rather than either
silently misinterpreting foreign ICL or failing for a generic/wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from warptap.icl_import import IclImportError, import_icl


def _fixture(icl_parser_dir: Path, *parts: str) -> Path:
    return icl_parser_dir / "tests" / "test_icls" / Path(*parts)


def test_sreg_has_no_instances_at_all(icl_parser_dir, icl_parser_module):
    """SReg is a leaf register module -- no Instance statements of any kind, let alone a
    warptap_sib-typed one."""
    path = _fixture(icl_parser_dir, "Instruments.icl")
    with pytest.raises(
        IclImportError, match="no 'warptap_sib'- or 'warptap_scan_mux_'-typed instances found"
    ):
        import_icl([path], "SReg", icl_parser_module=icl_parser_module)


def test_wrapped_instr_uses_a_different_instrument_wiring_convention(icl_parser_dir, icl_parser_module):
    """WrappedInstr instantiates a real IEEE 1687-2014 Appendix E Instrument+SReg pair
    directly -- no SIB/select-mux pattern at all, a genuinely different (and, for a bare
    instrument with no chain-select gating, simpler) real ICL idiom than warptap's own."""
    path = _fixture(icl_parser_dir, "Instruments.icl")
    with pytest.raises(
        IclImportError, match="no 'warptap_sib'- or 'warptap_scan_mux_'-typed instances found"
    ):
        import_icl([path], "WrappedInstr", icl_parser_module=icl_parser_module)


def test_wrapped_scan_uses_a_different_instrument_wiring_convention(icl_parser_dir, icl_parser_module):
    path = _fixture(icl_parser_dir, "Instruments.icl")
    with pytest.raises(
        IclImportError, match="no 'warptap_sib'- or 'warptap_scan_mux_'-typed instances found"
    ):
        import_icl([path], "WrappedScan", icl_parser_module=icl_parser_module)


def test_sreg_to_dreg_has_a_real_bug_in_the_fixture_itself(icl_parser_dir, icl_parser_module):
    """Not import_icl()'s own doing: SRegToDReg's DataRegister clause references the
    lowercase parameter ``$size`` while the module only ever declares ``Parameter Size``
    (capital S) -- icl_parser's own parameter lookup is case-sensitive and raises before
    import_icl() ever gets a usable parsed instance to inspect. A real, pre-existing quirk in
    the vendored tool's own fixture file, confirmed by direct probing, not a network-shape
    question import_icl() has any say over -- propagates unwrapped, matching this project's
    "let a lower layer's own precisely-named exception through rather than re-wrapping it into
    something vaguer" convention (e.g. pdl_interpreter.iTarget's ICLAddressError)."""
    path = _fixture(icl_parser_dir, "Instruments.icl")
    with pytest.raises(ValueError, match="Reference to parameter size not found"):
        import_icl([path], "SRegToDReg", icl_parser_module=icl_parser_module)


def test_scan_mux_001_hits_the_documented_retargeting_gap(icl_parser_dir, icl_parser_module):
    """Bare ScanMux edge cases with no SIB pattern and no Instance statements at all -- happens
    to also be one of the network shapes that trips icl_parser's own real, already-documented
    (Stage 10) retargeting-graph AssertionError, surfaced here as the same named
    IclImportError the round-trip tests pin for a warptap-shaped network hitting the identical
    underlying issue."""
    path = _fixture(icl_parser_dir, "scan_mux_001.icl")
    with pytest.raises(IclImportError, match="retargeting-graph"):
        import_icl([path], "scan_mux_001", icl_parser_module=icl_parser_module)


def test_scan_mux_002_needs_instruments_icl_and_still_has_no_sib_pattern(icl_parser_dir, icl_parser_module):
    """scan_mux_002 instantiates SReg from Instruments.icl -- a real cross-file ICL reference,
    needing both files passed together -- but even once it resolves cleanly (this network does
    NOT trip the retargeting-graph issue, unlike scan_mux_001), it's still not a warptap-shaped
    SIB network: rejected for the same structural reason as the rest of this corpus."""
    paths = [
        _fixture(icl_parser_dir, "scan_mux_002.icl"),
        _fixture(icl_parser_dir, "Instruments.icl"),
    ]
    with pytest.raises(
        IclImportError, match="no 'warptap_sib'- or 'warptap_scan_mux_'-typed instances found"
    ):
        import_icl(paths, "scan_mux_002", icl_parser_module=icl_parser_module)


def test_icl_syntax_6_reset_value_fixture_has_no_sib_pattern(icl_parser_dir, icl_parser_module):
    path = _fixture(icl_parser_dir, "benchmarks_conv", "ICL", "test_icl_syntax_6.icl")
    with pytest.raises(
        IclImportError, match="no 'warptap_sib'- or 'warptap_scan_mux_'-typed instances found"
    ):
        import_icl([path], "test_icl_syntax_6", icl_parser_module=icl_parser_module)


def test_icl_syntax_7_ir_decoded_dr_mux_tap_has_no_sib_pattern(icl_parser_dir, icl_parser_module):
    """A classic IR-decoded DR-mux JTAG TAP (ScanMux DRmux SelectedBy IR{...}) -- the OTHER
    real way ICL describes a JTAG-accessible register file, structurally unrelated to the
    self-select-ScanMux SIB idiom warptap's own network uses. Also confirms this shape does
    NOT trip the retargeting-graph issue either -- a real, useful negative data point about
    which shapes hit that bug and which don't."""
    path = _fixture(icl_parser_dir, "benchmarks_conv", "ICL", "test_icl_syntax_7.icl")
    with pytest.raises(
        IclImportError, match="no 'warptap_sib'- or 'warptap_scan_mux_'-typed instances found"
    ):
        import_icl([path], "test_icl_syntax_7", icl_parser_module=icl_parser_module)
