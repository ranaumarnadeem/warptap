"""Performance regression guard for ICL import (implementation_plan.md §7 Stage 12). Not a
correctness test -- test_icl_import_roundtrip.py already covers that -- this exists purely to
catch a silent reintroduction of the real scaling bottleneck found and fixed this session:
icl_parser's own Ijtag(...) constructor used to always build a full Z3 SMT-solver-based
retargeting-vector model (IclRegisterModel) that import_icl() never uses at all, making any
network with a handful of width>1 instruments impractically slow (measured: a 5-instrument,
width-4 network exceeded 5 minutes). Fixed via icl_parser's own additive
build_register_model=False parameter, now wired into import_icl().

The network below (20 instruments, width 8 -- 160 total content bits, comfortably past the
width>1 shapes that used to be impractical) completed in ~1-2s with the fix in place during
this session's own measurements, against a >300s timeout for a much smaller shape beforehand.
The bound chosen here (30s) is deliberately generous -- this is a tripwire for a real
regression (a bad merge reintroducing the unconditional retargeting-graph build, or a future
icl_parser version dropping the parameter), not a tight performance benchmark that would be
flaky on a slower CI machine.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from warptap.icl_emit import to_icl
from warptap.icl_import import import_icl
from warptap.sib_plan import InstrumentSpec, build_sib_plan

_TIME_BUDGET_SECONDS = 30

_SPECS = [
    InstrumentSpec(f"inst_{i}", width=8, capture_value=i % 256) for i in range(20)
]


def test_moderately_wide_multi_instrument_network_imports_quickly(icl_parser_module):
    graph, root = build_sib_plan(_SPECS, top_name="chip")
    icl_text = to_icl(graph, root, include_access_link=False)

    with tempfile.TemporaryDirectory(prefix="warptap-icl-import-perf-") as tmpdir:
        path = Path(tmpdir) / "warptap.icl"
        path.write_text(icl_text, encoding="utf-8")

        t0 = time.time()
        imported_graph, _root = import_icl([path], "chip", icl_parser_module=icl_parser_module)
        elapsed = time.time() - t0

    assert len(imported_graph.chain) == len(_SPECS)
    assert elapsed < _TIME_BUDGET_SECONDS, (
        f"import_icl() took {elapsed:.1f}s for a 20-instrument, width-8 network -- expected "
        f"well under {_TIME_BUDGET_SECONDS}s. This almost certainly means the retargeting-"
        "graph build (IclRegisterModel) is running again during import -- see this file's own "
        "module docstring and icl_import.py's for the fix this guards."
    )


def _walk_depth(n: int, directory: Path, icl_parser_module) -> int:
    """The deepest the Python stack gets while ``import_icl`` walks a flat network of ``n``
    top-level SIBs, counted from the end of the parse."""
    graph, root = build_sib_plan(
        [InstrumentSpec(f"leaf_{i}", width=1, capture_value=0) for i in range(n)],
        top_name="chip",
    )
    path = directory / f"flat_{n}.icl"
    path.write_text(to_icl(graph, root, include_access_link=False), encoding="utf-8")
    depth = [0, 0]  # now, deepest

    def count(frame, event, arg):
        if event == "call":
            depth[0] += 1
            depth[1] = max(depth[1], depth[0])
        elif event == "return":
            depth[0] -= 1

    def parse_then_count(top, files, **kwargs):
        ijtag = icl_parser_module(top, files, **kwargs)
        sys.setprofile(count)
        return ijtag

    parse_then_count.__module__ = icl_parser_module.__module__  # import_icl finds the parser's classes by it
    try:
        imported, _ = import_icl([path], "chip", icl_parser_module=parse_then_count)
    finally:
        sys.setprofile(None)
    assert [node.sib_name for node in imported.chain] == [node.sib_name for node in graph.chain]
    return depth[1]


def test_a_long_chain_is_walked_without_a_nested_call_per_element(tmp_path, icl_parser_module):
    """The walk's deepest Python stack is the same for 60 top-level SIBs as for 20. Walked with
    one nested call per element, it went 4 frames deeper per SIB, and Python 3.10 crashes at
    about 20000 frames: a few thousand top-level SIBs."""
    shallow = _walk_depth(20, tmp_path, icl_parser_module)
    deep = _walk_depth(60, tmp_path, icl_parser_module)
    assert deep <= shallow + 2, f"20 SIBs: {shallow} frames deep, 60 SIBs: {deep}"
