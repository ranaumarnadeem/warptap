"""ICL import round-trip on the real mem_subsystem_mbist network shape (implementation_plan.md
§7 Stage 12). Empirically corrects a previously-documented over-broad claim (Stage 10/12's own
"only a single-SIB single-WRITE-instrument network is confirmed to build cleanly"): this real
8-instrument, mixed READ/WRITE network round-trips through `icl_parser`'s full retargeting-
graph construction with ZERO exceptions -- the ONLY reason a network was ever seen to trip that
tool's own internal `AssertionError` is a width>1 instrument specifically (confirmed by direct
probing this session: a single width=3 READ-only instrument alone reproduces it, while networks
of any size and instrument-count built entirely from width=1 instruments -- this one included --
round-trip cleanly regardless of direction mix). `test_icl_import_roundtrip.py`'s own
"multi-instrument" test name/docstring predates this finding and is corrected alongside this
file to attribute the failure to its real cause (a width=3 instrument), not instrument count.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_emit import to_icl
from warptap.icl_import import import_icl
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan

_SPECS = [
    InstrumentSpec(
        "test_mode", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("test_mode"),),
    ),
    InstrumentSpec(
        "bist_start", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("bist_start"),),
    ),
    InstrumentSpec(
        "bist_done", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("bist_done"),),
    ),
    InstrumentSpec(
        "bist_fail", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("bist_fail"),),
    ),
    InstrumentSpec(
        "self_repair_start", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("self_repair_start"),),
    ),
    InstrumentSpec(
        "self_repair_done", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("self_repair_done"),),
    ),
    InstrumentSpec(
        "self_repair_fail", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("self_repair_fail"),),
    ),
    InstrumentSpec(
        "self_repair_busy", width=1, capture_value=0,
        direction=InstrumentDirection.READ, signal_bits=(SignalBinding("self_repair_busy"),),
    ),
]


def test_real_mem_subsystem_mbist_network_round_trips_fully_clean(icl_parser_module):
    """Every instrument in this real design is exactly 1 bit wide -- the condition now known
    to matter -- so this round-trips through BOTH icl_parser's structural check AND its full
    retargeting-graph build with zero exceptions, not just the single-instrument case."""
    graph, root = build_sib_plan(_SPECS, top_name="mem_subsystem_mbist")
    icl_text = to_icl(graph, root, include_access_link=False)

    with tempfile.TemporaryDirectory(prefix="warptap-mem-subsystem-mbist-import-") as tmpdir:
        path = Path(tmpdir) / "warptap.icl"
        path.write_text(icl_text, encoding="utf-8")
        imported_graph, imported_root = import_icl(
            [path], "mem_subsystem_mbist", icl_parser_module=icl_parser_module
        )  # must NOT raise at all

    assert imported_root.name == root.name == "mem_subsystem_mbist"
    assert [c.name for c in imported_root.children] == [c.name for c in root.children]
    assert len(imported_graph.chain) == len(graph.chain) == 8
    for orig_node, imp_node in zip(graph.chain, imported_graph.chain):
        assert imp_node.sib_name == orig_node.sib_name
        assert imp_node.instrument.name == orig_node.instrument.name
        assert imp_node.instrument.width == orig_node.instrument.width == 1
        assert imp_node.instrument.direction == orig_node.instrument.direction
