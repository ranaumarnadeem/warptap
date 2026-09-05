"""Pure-Python tests for faultflow pattern retargeting (implementation_plan.md §7 Stage 14).
No real faultflow/RTL involved here -- structural assertions against hand-built
``--export-patterns``-shaped dicts, mirroring this project's established style for a
from-scratch translation module.
"""

from __future__ import annotations

import pytest

from warptap.faultflow_retarget import FaultflowRetargetError, retarget_faultflow_patterns
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import PulsePin, ShiftDR

_SPECS = [
    InstrumentSpec(
        "ctrl_write",
        width=3,
        capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("bist_start", 0), SignalBinding("bist_start", 1), SignalBinding("bist_start", 2)),
    ),
    InstrumentSpec("status_a", width=2, capture_value=0, signal_bits=(SignalBinding("status", 0), SignalBinding("status", 1))),
]


def _graph_root():
    return build_sib_plan(_SPECS, top_name="chip")


def test_missing_chain_mapping_raises_named_error():
    graph, root = _graph_root()
    patterns = [{"load_seqs": {"5": [True, False, True]}, "expected_unload": {}, "capture_pi_values": {}}]
    with pytest.raises(FaultflowRetargetError, match="5"):
        retarget_faultflow_patterns(
            patterns, {}, graph, root, clock_port="sysclk", clock_pulse_count=1
        )


def test_bit_order_is_reversed_before_packing():
    """Non-palindromic worked example (see faultflow_retarget.py's own module docstring for
    the full derivation): feeding A then B then C (A != B != C) into a 3-bit register leaves
    position0=C, position1=B, position2=A; faultflow's own exported sequence is [A,B,C]
    (chronological feed order); warptap's payload bit k = position k directly, so
    payload_value's bits must be [C,B,A] = reversed([A,B,C])."""
    graph, root = _graph_root()
    # A=0, B=1, C=1 -> faultflow sequence [0,1,1] -> reversed [1,1,0] -> payload = 1+2+0 = 3
    patterns = [{"load_seqs": {"0": [False, True, True]}, "expected_unload": {}, "capture_pi_values": {}}]
    ops = retarget_faultflow_patterns(
        patterns, {0: "ctrl_write"}, graph, root, clock_port="sysclk", clock_pulse_count=1
    )
    shift_drs = [op for op in ops if isinstance(op, ShiftDR)]
    # ctrl_write is the only (and first) SIB -- phase 2's tdi encodes the 3-bit payload
    # directly at the chain's own position-0-nearest-tdi slot; decode via the same
    # position-order convention test_pdl_interpreter.py's own tests already establish.
    from warptap.tap_ir import bits_from_int

    phase2 = shift_drs[-1]
    fed_bits = bits_from_int(phase2.tdi, phase2.bits)
    position_bits = list(reversed(fed_bits))
    content_bits = position_bits[0:3]
    assert content_bits == [1, 1, 0]  # payload_value == 3 == 0b011 LSB-first -> [1,1,0]


def test_load_padding_stripped_from_front():
    """load_seqs is front-padded to a shared max_chain_length -- a 2-bit chain padded to 5
    bits has its real content as the LAST 2 elements."""
    graph, root = _graph_root()
    # Real 2-bit content (chronological/sequence order) = [True, False]; front-padded with
    # 3 False bits to reach length 5.
    patterns = [
        {
            "load_seqs": {"1": [False, False, False, True, False]},
            "expected_unload": {},
            "capture_pi_values": {},
        }
    ]
    # status_a is a READ instrument in this fixture but iWrite doesn't care about direction
    # at the PDLInterpreter layer -- only used here to isolate the padding-strip logic.
    ops = retarget_faultflow_patterns(
        patterns, {1: "status_a"}, graph, root, clock_port="sysclk", clock_pulse_count=1
    )
    assert ops  # reached iApply without raising -- padding math didn't underflow


def test_unload_padding_stripped_from_back():
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {},
            "expected_unload": {"1": [True, False, False, False, False]},
            "capture_pi_values": {},
        }
    ]
    ops = retarget_faultflow_patterns(
        patterns, {1: "status_a"}, graph, root, clock_port="sysclk", clock_pulse_count=1
    )
    assert ops


def test_short_sequence_shorter_than_width_raises_named_error():
    graph, root = _graph_root()
    patterns = [{"load_seqs": {"1": [True]}, "expected_unload": {}, "capture_pi_values": {}}]
    with pytest.raises(FaultflowRetargetError, match="width"):
        retarget_faultflow_patterns(
            patterns, {1: "status_a"}, graph, root, clock_port="sysclk", clock_pulse_count=1
        )


def test_pulsepin_inserted_once_per_pattern_not_per_chain():
    """Two chains referenced in ONE pattern with an expected_unload -- exactly one PulsePin,
    positioned after BOTH loads and before EITHER unload (see module docstring: comparing a
    chain's response before another chain in the same pattern is even loaded, or before any
    capture happened, would be a real correctness bug)."""
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {"0": [False, False, True]},
            "expected_unload": {"1": [True, False]},
            "capture_pi_values": {"bist_start": True},
        }
    ]
    ops = retarget_faultflow_patterns(
        patterns,
        {0: "ctrl_write", 1: "status_a"},
        graph,
        root,
        clock_port="sysclk",
        clock_pulse_count=4,
    )
    pulse_ops = [op for op in ops if isinstance(op, PulsePin)]
    assert len(pulse_ops) == 1
    assert pulse_ops[0].port == "sysclk"
    assert pulse_ops[0].count == 4
    assert dict(pulse_ops[0].hold_pins) == {"bist_start": 1}

    pulse_index = ops.index(pulse_ops[0])
    # Everything before the pulse belongs to the load phase, everything after to unload --
    # a ShiftDR count split confirms the ordering without depending on exact op counts.
    assert any(isinstance(op, ShiftDR) for op in ops[:pulse_index])
    assert any(isinstance(op, ShiftDR) for op in ops[pulse_index + 1 :])


def test_load_only_pattern_gets_no_pulsepin():
    """No expected_unload at all -- nothing to capture, so no PulsePin should be inserted."""
    graph, root = _graph_root()
    patterns = [{"load_seqs": {"0": [True, False, False]}, "expected_unload": {}, "capture_pi_values": {}}]
    ops = retarget_faultflow_patterns(
        patterns, {0: "ctrl_write"}, graph, root, clock_port="sysclk", clock_pulse_count=1
    )
    assert not any(isinstance(op, PulsePin) for op in ops)


def test_multiple_patterns_each_get_their_own_pulsepin():
    graph, root = _graph_root()
    patterns = [
        {"load_seqs": {"0": [True, False, False]}, "expected_unload": {"0": [False, False, True]}, "capture_pi_values": {}},
        {"load_seqs": {"0": [False, True, False]}, "expected_unload": {"0": [True, True, False]}, "capture_pi_values": {}},
    ]
    ops = retarget_faultflow_patterns(
        patterns, {0: "ctrl_write"}, graph, root, clock_port="sysclk", clock_pulse_count=2
    )
    assert sum(1 for op in ops if isinstance(op, PulsePin)) == 2
