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
