"""PDL emission on the real mem_subsystem_mbist scenario (implementation_plan.md §7 Stage 11):
renders real PDL text for the exact scenario Stage 9's own `pdl_verify.py` tier-2 test
cross-simulates (write `self_repair_start=1`, settle, read `self_repair_busy` expecting 1)
against the real 8-instrument network -- but PDL emission, like `PDLInterpreter` itself,
operates purely on the abstract `PhysicalGraph`/`ModuleInstance` model, so this needs neither
the real openMBIST checkout nor Yosys/Icarus. Self-consistency-validated only, matching
`pdl_emit.py`'s own permanent, honest scope (no independent PDL parser exists anywhere to
validate against) -- reconfirmed here across a real, deeper (8-instrument) chain, not just the
2-instrument case `test_pdl_emit_integration.py` already proves the underlying mechanism with.
"""

from __future__ import annotations

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.pdl_emit import to_pdl
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_layout import compose_bits
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sib_retarget import open_path_to
from warptap.tap_ir import bits_from_int, bits_to_int

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


def test_real_mem_subsystem_mbist_scenario_renders_and_matches_actual_shifted_payload():
    graph, root = build_sib_plan(_SPECS, top_name="mem_subsystem_mbist")
    pdl = PDLInterpreter(graph, root)

    pdl.iTarget("self_repair_start")
    pdl.iWrite(1)
    write_ops = pdl.iApply()
    pdl.iRunLoop(10)
    pdl.iTarget("self_repair_busy")
    pdl.iRead(1)
    read_ops = pdl.iApply()
    pdl.iTarget("self_repair_start")
    pdl.iWrite(0)
    pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert pdl_text.splitlines() == [
        "iTarget self_repair_start;",
        "iWrite self_repair_start 0x1;",
        "iApply;",
        "iRunLoop 10 -tck;",
        "iTarget self_repair_busy;",
        "iRead self_repair_busy 0x1;",
        "iApply;",
        "iTarget self_repair_start;",
        "iWrite self_repair_start 0x0;",
        "iApply;",
    ]

    # Self-consistency (pdl_emit.py's own documented ceiling -- no independent PDL parser
    # exists to validate against): independently recompute, via the SAME sib_layout primitives
    # PDLInterpreter itself uses but called fresh here, what phase 2's tdi/tdo SHOULD be for
    # this real 8-deep chain, and confirm the rendered iWrite/iRead values agree with the
    # ACTUAL bits that were shifted -- not just that to_pdl() echoed back its own inputs.
    write_target = next(iter(open_path_to(graph, "self_repair_start"))).name
    expected_write_bits = compose_bits(
        graph, frozenset({write_target}), frozenset({write_target}),
        target_sib=write_target, payload_value=1,
    )
    assert write_ops[4].tdi == bits_to_int(list(reversed(expected_write_bits)))

    read_target = next(iter(open_path_to(graph, "self_repair_busy"))).name
    expected_read_bits = compose_bits(
        graph, frozenset({read_target}), frozenset({read_target}),
        target_sib=read_target, payload_value=1,
    )
    assert read_ops[4].tdo == bits_to_int(list(reversed(expected_read_bits)))
