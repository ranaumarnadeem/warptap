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
from warptap.sib_plan import HierarchySpec, InstrumentSpec

_CTRL_WRITE = InstrumentSpec(
    "ctrl_write", width=1, capture_value=0,
    direction=InstrumentDirection.WRITE,
    signal_bits=(SignalBinding("ctrl_in", 0),),
)
_STATUS_READ = InstrumentSpec(
    "status_read", width=1, capture_value=1,
    direction=InstrumentDirection.READ,
    signal_bits=(SignalBinding("status_out", 0),),
)

_SPECS = [_CTRL_WRITE, _STATUS_READ]


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


def test_insert_test_access_accepts_a_hierarchy_spec(fixtures_dir, yosys_command):
    """``specs`` was widened from ``List[InstrumentSpec]`` to ``List[TestAccessSpec]``
    (``Union[InstrumentSpec, HierarchySpec]``) -- this function's own body already just
    forwarded ``specs`` straight to ``build_sib_plan`` unchanged, which has accepted
    ``HierarchySpec`` since Stage 4's own nested-network work, so the widening was a type/
    docstring correction, not a behavior change. This test is the proof: nothing before it
    exercised ``insert_test_access`` (as opposed to ``build_sib_plan``/``insert_sib_network``
    directly) with a ``HierarchySpec`` in its specs, so the correctness of this exact call
    path had never actually been checked. Nests ``status_read`` one level under a
    ``HierarchySpec`` named ``bank_a``, matching docs/guide/nested-sib-networks.md's own
    example shape."""
    nested_specs = [
        HierarchySpec("bank_a", children=[_STATUS_READ]),
        _CTRL_WRITE,
    ]

    inserted_verilog, graph, root = insert_test_access(
        [fixtures_dir / "real_signal.v"], "real_signal", nested_specs, yosys_command=yosys_command,
    )

    assert "module real_signal" in inserted_verilog
    # Physical network: one top-level hierarchy SIB (bank_a) plus one top-level leaf SIB
    # (ctrl_write); status_read is nested inside bank_a's own chain, not a top-level sibling.
    assert len(graph.chain) == 2
    hierarchy_node = next(n for n in graph.chain if n.sib_name == "sib_bank_a")
    assert hierarchy_node.instrument is None
    assert len(hierarchy_node.nested) == 1
    assert hierarchy_node.nested[0].instrument.name == "status_read"
    # Addressing stays flat regardless of nesting depth (nested-sib-networks.md's own
    # documented convention) -- both leaves are still direct children of root.
    assert {c.name for c in root.children} == {"ctrl_write", "status_read"}

    # The returned (graph, root) must actually drive a PDLInterpreter through the nested
    # leaf specifically -- proving the depth is real, not just structurally present.
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("status_read")
    ops = pdl.iApply()
    assert ops  # a real op sequence was produced for the nested target
