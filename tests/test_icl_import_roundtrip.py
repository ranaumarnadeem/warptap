"""Round-trip test for ICL import (implementation_plan.md §7 Stage 12): build a network via
``build_sib_plan``, emit it with ``to_icl()`` (Stage 10), parse it back with ``import_icl()``,
and confirm the recovered ``PhysicalGraph``/``ModuleInstance`` match the original -- exercising
Stage 10's emitter and Stage 12's importer end to end, against each other, with no new fixture
needed.

**Only the single-SIB single-WRITE-instrument network round-trips cleanly, and this is
Stage 10's own already-documented limitation, not a new one Stage 12 introduces.** ``Ijtag()``
does the real structural parse AND the real retargeting-graph build (``IclRegisterModel``) in
one inseparable constructor call; the retargeting-graph build itself hits a real, unresolved
internal ``AssertionError`` for most network shapes (confirmed in Stage 10's own validation).
Stage 10's structural-only tests could tolerate that (they only needed the file to *parse*, not
the returned object), but ``import_icl()`` genuinely needs ``Ijtag()``'s returned
``.icl_instance`` to do anything at all -- so for those same network shapes,
``import_icl()`` cannot avoid surfacing the underlying failure, now as a named
``IclImportError`` rather than a bare third-party traceback. Both outcomes are tested below:
the one shape that fully round-trips, and the documented failure for a realistic multi-
instrument network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from warptap.icl_emit import to_icl
from warptap.icl_import import IclImportError, import_icl
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan


def _write_icl(icl_text: str, tmpdir: Path) -> Path:
    path = Path(tmpdir) / "warptap.icl"
    path.write_text(icl_text, encoding="utf-8")
    return path


def test_single_sib_write_only_network_round_trips_cleanly(icl_parser_module):
    specs = [
        InstrumentSpec(
            "ctrl_write",
            width=1,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("bist_start"),),
        )
    ]
    graph, root = build_sib_plan(specs, top_name="chip")
    icl_text = to_icl(graph, root, include_access_link=False)

    with tempfile.TemporaryDirectory(prefix="warptap-icl-import-") as tmpdir:
        path = _write_icl(icl_text, Path(tmpdir))
        imported_graph, imported_root = import_icl([path], "chip", icl_parser_module=icl_parser_module)

    assert imported_root.name == root.name == "chip"
    assert [c.name for c in imported_root.children] == [c.name for c in root.children]

    assert len(imported_graph.chain) == len(graph.chain) == 1
    orig_node, imp_node = graph.chain[0], imported_graph.chain[0]
    assert imp_node.sib_name == orig_node.sib_name
    assert imp_node.nested == ()
    assert imp_node.instrument.name == orig_node.instrument.name == "ctrl_write"
    assert imp_node.instrument.width == orig_node.instrument.width == 1
    assert imp_node.instrument.direction == orig_node.instrument.direction == InstrumentDirection.WRITE

    # The documented, permanent round-trip limitation: signal_bits/capture_value are NOT
    # recoverable from ICL text (see icl_import.py's own module docstring) -- confirm they
    # come back as the honest empty/zero default, not a fabricated guess.
    assert imp_node.instrument.signal_bits == ()
    assert imp_node.instrument.capture_value == 0
    # And confirm the ORIGINAL really did have real signal_bits, so this is a genuine
    # information loss being demonstrated, not a vacuous check against an already-empty field.
    assert orig_node.instrument.signal_bits == (SignalBinding("bist_start"),)


def test_multi_instrument_network_round_trip_hits_documented_retargeting_gap(icl_parser_module):
    """NOT a bug -- the exact same vendored-tool retargeting-graph AssertionError Stage 10's
    own validation tests already tolerate (test_icl_emit_iclparser_validation.py), surfaced
    here as a named IclImportError instead, because import_icl() has no way to get a usable
    result out of a failed Ijtag() construction the way a structural-only check could."""
    specs = [
        InstrumentSpec("sensor_a", width=3, capture_value=0b101),
        InstrumentSpec(
            "ctrl_write",
            width=1,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("bist_start"),),
        ),
    ]
    graph, root = build_sib_plan(specs, top_name="chip")
    icl_text = to_icl(graph, root, include_access_link=False)

    with tempfile.TemporaryDirectory(prefix="warptap-icl-import-") as tmpdir:
        path = _write_icl(icl_text, Path(tmpdir))
        with pytest.raises(IclImportError, match="retargeting-graph"):
            import_icl([path], "chip", icl_parser_module=icl_parser_module)


def test_access_link_network_raises_named_error(icl_parser_module):
    """A round trip emitted WITH the (default-on) AccessLink block hits icl_parser's own
    confirmed "Not supported" gap (Stage 10) -- import_icl() must name this specifically, not
    let a bare ValueError from third-party code surface unexplained."""
    specs = [
        InstrumentSpec(
            "ctrl_write",
            width=1,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("bist_start"),),
        )
    ]
    graph, root = build_sib_plan(specs, top_name="chip")
    icl_text = to_icl(graph, root, include_access_link=True)

    with tempfile.TemporaryDirectory(prefix="warptap-icl-import-") as tmpdir:
        path = _write_icl(icl_text, Path(tmpdir))
        with pytest.raises(IclImportError, match="AccessLink"):
            import_icl([path], "chip", icl_parser_module=icl_parser_module)
