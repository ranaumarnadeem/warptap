"""Pure-Python tests for faultflow compression-aware pattern retargeting (Stage 24).
No real faultflow/RTL involved here -- structural/hand-derived assertions, mirroring
test_faultflow_retarget.py's established style for a from-scratch translation module.
"""

from __future__ import annotations

import pytest

from warptap.faultflow_compression import (
    FaultflowCompressionError,
    LfsrPolynomial,
    care_bit_rows,
    retarget_compressed_faultflow_patterns,
    solve_pattern_seed,
    solve_xor_broadcast,
)
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import PulsePin, ShiftDR

# A trivial 2-bit LFSR (state[0] <= fb; state[1] <= state[0] ^ fb, fb = state[1]) with a
# 2-chain phase shifter reading register bits 0 and 1 directly -- small enough to hand-derive
# every cycle's coefficient row and, separately, a concrete seed's real trajectory.
_POLY = LfsrPolynomial(2, frozenset({1}))
_PHASE_SHIFTER_TAPS = [[0], [1]]


def test_care_bit_rows_matches_hand_derived_trace():
    """rows[i] starts at 1<<i (state(0)==seed); cycle 0: chain0 mask=1 (bit0), chain1 mask=2
    (bit1). _step: fb=rows[1]; new[0]=fb; new[1]=rows[0]^fb (1 in taps). Traced by hand:
    cycle0 rows=[1,2] -> masks [1,2]; step -> rows=[2,3]
    cycle1 rows=[2,3] -> masks [2,3]; step -> rows=[3,1]
    cycle2 rows=[3,1] -> masks [3,1]
    """
    assert care_bit_rows(_POLY, _PHASE_SHIFTER_TAPS, 3) == [[1, 2], [2, 3], [3, 1]]


def test_solve_xor_broadcast_solvable_system():
    # Two independent rows, each isolating one bit directly (identity-like): bit0 must be 1,
    # bit1 must be 0 -> seed = 0b01 = 1.
    seed = solve_xor_broadcast([0b01, 0b10], [True, False], width=2)
    assert seed == 1


def test_solve_xor_broadcast_free_channels_default_to_zero():
    # Only bit0 constrained; bit1 is never referenced by any row -- must default to 0.
    seed = solve_xor_broadcast([0b01], [True], width=2)
    assert seed == 0b01


def test_solve_xor_broadcast_contradiction_returns_none():
    # Both rows reference the same single column (bit0) with conflicting required values.
    assert solve_xor_broadcast([0b1, 0b1], [True, False], width=1) is None


def test_solve_pattern_seed_matches_hand_derived_trajectory():
    """seed=1 (bit0=1, bit1=0) through _POLY/_PHASE_SHIFTER_TAPS produces, by direct
    parity-of-(mask & seed) computation against test_care_bit_rows's own hand-derived rows:
    chain0: [1,0,1] -> [True, False, True]; chain1: [0,1,1] -> [False, True, True]."""
    rows = care_bit_rows(_POLY, _PHASE_SHIFTER_TAPS, 3)
    load_seqs = {"0": [True, False, True], "1": [False, True, True]}
    assert solve_pattern_seed(load_seqs, rows, _POLY.width, pattern_index=0) == 1


def test_solve_pattern_seed_contradiction_raises_named_error():
    rows = care_bit_rows(_POLY, _PHASE_SHIFTER_TAPS, 3)
    # chain0 cycle0 (mask=1) requires bit0=1; chain1 cycle2 (also mask=1) requires bit0=0.
    load_seqs = {"0": [True, False, False], "1": [False, False, False]}
    with pytest.raises(FaultflowCompressionError, match="pattern 0"):
        solve_pattern_seed(load_seqs, rows, _POLY.width, pattern_index=0)


def test_solve_pattern_seed_ignores_dont_care_position_when_load_care_given():
    """Same load_seqs as test_solve_pattern_seed_contradiction_raises_named_error -- chain0
    cycle0 and chain1 cycle2 hit the same GF(2) column with conflicting required values,
    unsatisfiable when every position is a hard constraint. With load_care naming ONLY chain0
    cycle0 as a real care bit, the conflicting chain1 cycle2 value must be dropped entirely
    (not solved-against-as-a-constraint) rather than making the system unsatisfiable -- the
    exact scenario the old, load_care-less code could get wrong."""
    rows = care_bit_rows(_POLY, _PHASE_SHIFTER_TAPS, 3)
    load_seqs = {"0": [True, False, False], "1": [False, False, False]}
    seed = solve_pattern_seed(
        load_seqs, rows, _POLY.width, pattern_index=0, load_care={(0, 0)}
    )
    assert seed == 1  # bit0=1 (chain0 cycle0's only real constraint), bit1 free -> defaults 0


def test_solve_pattern_seed_load_care_none_still_uses_every_position():
    """Backward compatibility: load_care=None (also the default) must reproduce the original
    behavior exactly -- the same contradiction still raises, since every specified position
    remains a hard constraint when load_care isn't given."""
    rows = care_bit_rows(_POLY, _PHASE_SHIFTER_TAPS, 3)
    load_seqs = {"0": [True, False, False], "1": [False, False, False]}
    with pytest.raises(FaultflowCompressionError, match="pattern 0"):
        solve_pattern_seed(
            load_seqs, rows, _POLY.width, pattern_index=0, load_care=None
        )


def test_solve_pattern_seed_wrong_length_raises_named_error():
    rows = care_bit_rows(_POLY, _PHASE_SHIFTER_TAPS, 3)
    load_seqs = {"0": [True, False]}  # 2 bits, but max_chain_length (len(rows)) is 3
    with pytest.raises(FaultflowCompressionError, match="max_chain_length=3"):
        solve_pattern_seed(load_seqs, rows, _POLY.width, pattern_index=0)


def _graph_root():
    specs = [
        InstrumentSpec(
            "tdi_channel",
            width=2,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("tdi", 0), SignalBinding("tdi", 1)),
        ),
        InstrumentSpec(
            "scan_out_0",
            width=2,
            capture_value=0,
            signal_bits=(
                SignalBinding("scan_out_0", 0),
                SignalBinding("scan_out_0", 1),
            ),
        ),
    ]
    return build_sib_plan(specs, top_name="chip")


def test_retarget_compressed_missing_unload_chain_mapping_raises_named_error():
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {},
            "expected_unload": {"7": [True, False]},
            "capture_pi_values": {},
        }
    ]
    with pytest.raises(FaultflowCompressionError, match="7"):
        retarget_compressed_faultflow_patterns(
            patterns,
            {},
            "tdi_channel",
            graph,
            root,
            poly=_POLY,
            phase_shifter_taps=_PHASE_SHIFTER_TAPS,
            max_chain_length=3,
            clock_port="clk",
            scan_enable_port="scan_en",
        )


def test_retarget_compressed_load_phase_op_sequence():
    """Load phase must: (1) write the solved seed to the channel instrument, (2) idle-settle
    one clk edge with scan_en held LOW, (3) pulse clk max_chain_length times with scan_en held
    HIGH (cycle 0 of this pulse is the reseed edge) -- see module docstring for the real-RTL
    derivation. Then the capture pulse (scan_en held LOW, no capture_pi_values here), then the
    unload iApply."""
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {"0": [True, False, True], "1": [False, True, True]},
            "expected_unload": {"3": [True, False]},
            "capture_pi_values": {},
        }
    ]
    ops = retarget_compressed_faultflow_patterns(
        patterns,
        {3: "scan_out_0"},
        "tdi_channel",
        graph,
        root,
        poly=_POLY,
        phase_shifter_taps=_PHASE_SHIFTER_TAPS,
        max_chain_length=3,
        clock_port="clk",
        scan_enable_port="scan_en",
        clock_pulse_count=1,
    )
    pulse_indices = [i for i, op in enumerate(ops) if isinstance(op, PulsePin)]
    pulse_ops = [ops[i] for i in pulse_indices]
    assert len(pulse_ops) == 3
    assert pulse_ops[0] == PulsePin("clk", 1, hold_pins=(("scan_en", 0),))
    assert pulse_ops[1] == PulsePin("clk", 3, hold_pins=(("scan_en", 1),))
    assert pulse_ops[2] == PulsePin("clk", 1, hold_pins=(("scan_en", 0),))  # capture

    seed_write_index, natural_step_index, capture_index = pulse_indices
    assert seed_write_index < natural_step_index < capture_index
    # The seed write (a whole-instrument iApply) happened before the idle-settle pulse.
    assert any(isinstance(op, ShiftDR) for op in ops[:seed_write_index])
    # The unload read (a whole-instrument iApply) happens after the capture pulse.
    assert any(isinstance(op, ShiftDR) for op in ops[capture_index + 1 :])


def test_retarget_compressed_uses_load_care_to_avoid_a_dont_care_conflict():
    """A pattern whose load_seqs would be unsatisfiable if every position were a hard
    constraint (the exact fixture from test_solve_pattern_seed_contradiction_raises_named_error),
    but whose load_care names only the one real care-bit position -- confirms
    retarget_compressed_faultflow_patterns actually reads pattern["load_care"] and passes it
    through to solve_pattern_seed, not just that solve_pattern_seed itself can accept one."""
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {"0": [True, False, False], "1": [False, False, False]},
            "load_care": [[0, 0]],
            "expected_unload": {},
            "capture_pi_values": {},
        }
    ]
    # Would raise FaultflowCompressionError without load_care being read and threaded through --
    # see test_solve_pattern_seed_contradiction_raises_named_error for that same load_seqs
    # rejected outright with no load_care.
    retarget_compressed_faultflow_patterns(
        patterns,
        {},
        "tdi_channel",
        graph,
        root,
        poly=_POLY,
        phase_shifter_taps=_PHASE_SHIFTER_TAPS,
        max_chain_length=3,
        clock_port="clk",
        scan_enable_port="scan_en",
    )


def test_retarget_compressed_load_only_pattern_gets_no_capture_pulse():
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {"0": [True, False, True], "1": [False, True, True]},
            "expected_unload": {},
            "capture_pi_values": {},
        }
    ]
    ops = retarget_compressed_faultflow_patterns(
        patterns,
        {},
        "tdi_channel",
        graph,
        root,
        poly=_POLY,
        phase_shifter_taps=_PHASE_SHIFTER_TAPS,
        max_chain_length=3,
        clock_port="clk",
        scan_enable_port="scan_en",
    )
    pulse_ops = [op for op in ops if isinstance(op, PulsePin)]
    assert len(pulse_ops) == 2  # idle-settle + natural-step only, no capture pulse
