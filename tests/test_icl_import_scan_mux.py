"""Round-trip tests for ICL import's multi-arm ScanMux support (multi-arm ScanMux plan Phase
7): build a PhysicalGraph containing a ScanMuxNode by hand (build_sib_plan is SibNode-only, see
icl_model.py's own module docstring), emit it with to_icl() (Phase 7's own icl_emit.py side),
parse it back with import_icl(), and confirm the recovered graph matches the original --
mirroring test_icl_import_roundtrip.py's own style and conventions closely.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_emit import to_icl
from warptap.icl_import import import_icl
from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SibNode,
    SignalBinding,
)


def _write_icl(icl_text: str, tmpdir: Path) -> Path:
    path = Path(tmpdir) / "warptap.icl"
    path.write_text(icl_text, encoding="utf-8")
    return path


def _instrument(name, width=1, capture_value=0, direction=InstrumentDirection.READ, signal_bits=()):
    return InstrumentNode(
        name=name, width=width, capture_value=capture_value, direction=direction,
        signal_bits=signal_bits,
    )


def _round_trip(graph, root_name, icl_parser_module):
    root = ModuleInstance(name=root_name)
    icl_text = to_icl(graph, root, include_access_link=False)
    with tempfile.TemporaryDirectory(prefix="warptap-icl-import-") as tmpdir:
        path = _write_icl(icl_text, Path(tmpdir))
        return import_icl([path], root_name, icl_parser_module=icl_parser_module)


def test_top_level_two_arm_mux_round_trips_cleanly(icl_parser_module):
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(values=(1,), instrument=_instrument("a1", width=3, capture_value=0b101)),
            ScanArm(
                values=(2,),
                instrument=_instrument(
                    "a2", direction=InstrumentDirection.WRITE,
                    signal_bits=(SignalBinding("bist_start"),),
                ),
            ),
        ),
    )
    graph = PhysicalGraph(chain=(mux,))
    imported_graph, imported_root = _round_trip(graph, "chip", icl_parser_module)

    assert {c.name for c in imported_root.children} == {"a1", "a2"}
    assert len(imported_graph.chain) == 1
    imp_mux = imported_graph.chain[0]
    assert isinstance(imp_mux, ScanMuxNode)
    assert imp_mux.mux_name == "mux_a"
    assert imp_mux.select_width == 2
    assert len(imp_mux.arms) == 2

    orig_by_value = {arm.values[0]: arm for arm in mux.arms}
    imp_by_value = {arm.values[0]: arm for arm in imp_mux.arms}
    assert set(imp_by_value) == set(orig_by_value) == {1, 2}
    for value, orig_arm in orig_by_value.items():
        imp_arm = imp_by_value[value]
        assert imp_arm.values == (value,)
        assert imp_arm.nested == ()
        assert imp_arm.instrument.name == orig_arm.instrument.name
        assert imp_arm.instrument.width == orig_arm.instrument.width
        assert imp_arm.instrument.direction == orig_arm.instrument.direction
        # Same documented, permanent round-trip limitation as the SibNode case.
        assert imp_arm.instrument.signal_bits == ()
        assert imp_arm.instrument.capture_value == 0


def test_three_arm_mux_round_trips_cleanly(icl_parser_module):
    """N > 2 specifically -- confirms the fromArm0/fromArm1/... enumeration loop (and the
    per-arm host{k} interfaces it reads back through) generalizes past 2 arms."""
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(values=(0,), instrument=_instrument("a0")),
            ScanArm(values=(1,), instrument=_instrument("a1", width=2)),
            ScanArm(values=(2,), instrument=_instrument("a2", width=3)),
        ),
    )
    graph = PhysicalGraph(chain=(mux,))
    imported_graph, _root = _round_trip(graph, "chip", icl_parser_module)

    imp_mux = imported_graph.chain[0]
    assert imp_mux.select_width == 2
    assert len(imp_mux.arms) == 3
    imp_by_value = {arm.values[0]: arm.instrument.width for arm in imp_mux.arms}
    assert imp_by_value == {0: 1, 1: 2, 2: 3}


def test_mux_gated_by_hierarchy_sib_round_trips_cleanly(icl_parser_module):
    """A hierarchy SIB gating a ScanMuxNode instead of a leaf instrument -- exercises
    _build_node's own widened _is_slot_instance check (a fromSO target that's mux-typed, not
    just SIB-typed)."""
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(
            ScanArm(values=(0,), instrument=_instrument("a0")),
            ScanArm(values=(1,), instrument=_instrument("a1")),
        ),
    )
    outer = SibNode(sib_name="gate_a", instrument=None, nested=(mux,))
    graph = PhysicalGraph(chain=(outer,))
    imported_graph, imported_root = _round_trip(graph, "chip", icl_parser_module)

    assert {c.name for c in imported_root.children} == {"a0", "a1"}
    assert len(imported_graph.chain) == 1
    imp_outer = imported_graph.chain[0]
    assert isinstance(imp_outer, SibNode)
    assert imp_outer.sib_name == "gate_a"
    assert imp_outer.instrument is None
    assert len(imp_outer.nested) == 1
    imp_mux = imp_outer.nested[0]
    assert isinstance(imp_mux, ScanMuxNode)
    assert imp_mux.mux_name == "mux_a"
    assert {arm.values[0] for arm in imp_mux.arms} == {0, 1}


def test_mux_arm_gating_nested_hierarchy_sib_round_trips_cleanly(icl_parser_module):
    """The other nesting direction: one of a mux's own arms gates a nested hierarchy SIB
    rather than a leaf instrument -- exercises _build_scan_mux_node's own arm-nested branch,
    the mux-side counterpart to the test above."""
    inner = SibNode(sib_name="sib_inner", instrument=_instrument("deep", width=2))
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(
            ScanArm(values=(0,), nested=(inner,)),
            ScanArm(values=(1,), instrument=_instrument("a1")),
        ),
    )
    graph = PhysicalGraph(chain=(mux,))
    imported_graph, imported_root = _round_trip(graph, "chip", icl_parser_module)

    assert {c.name for c in imported_root.children} == {"deep", "a1"}
    imp_mux = imported_graph.chain[0]
    assert isinstance(imp_mux, ScanMuxNode)
    imp_by_value = {arm.values[0]: arm for arm in imp_mux.arms}
    assert imp_by_value[1].instrument.name == "a1"
    nested_arm = imp_by_value[0]
    assert nested_arm.instrument is None
    assert len(nested_arm.nested) == 1
    imp_inner = nested_arm.nested[0]
    assert isinstance(imp_inner, SibNode)
    assert imp_inner.sib_name == "sib_inner"
    assert imp_inner.instrument.name == "deep"
    assert imp_inner.instrument.width == 2
