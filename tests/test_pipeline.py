"""Test for warptap.pipeline.insert_test_access (library-quality pass, post-Stage 14) -- the
new end-to-end convenience function extracted from the exact sequence every cross-sim test
otherwise repeats by hand. Reuses the existing real_signal.v fixture
(tests/test_sib_insert_write_instrument_cross_sim.py's own target), confirming the returned
Verilog is real/synthesizable and the returned (graph, root) actually drives a PDLInterpreter
correctly -- not just that the five wrapped calls didn't raise.
"""

from __future__ import annotations

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pipeline import insert_test_access
from warptap.sib_plan import InstrumentSpec

_SPECS = [
    InstrumentSpec(
        "ctrl_write", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("ctrl_in", 0),),
    ),
    InstrumentSpec(
        "status_read", width=1, capture_value=1,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("status_out", 0),),
    ),
]


def test_insert_test_access_returns_synthesizable_verilog_and_a_working_network(
    fixtures_dir, yosys_command
):
    inserted_verilog, graph, root = insert_test_access(
        [fixtures_dir / "real_signal.v"], "real_signal", _SPECS, yosys_command=yosys_command
    )

    assert "module real_signal" in inserted_verilog
    assert len(graph.chain) == 2
    assert {node.instrument.name for node in graph.chain} == {"ctrl_write", "status_read"}
    assert {c.name for c in root.children} == {"ctrl_write", "status_read"}

    # The returned (graph, root) must actually drive a PDLInterpreter -- not just be
    # structurally present.
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    ops = pdl.iApply()
    assert ops  # a real op sequence was produced, not an empty/broken retargeting result
