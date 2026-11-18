"""Pure-Python tests for the decompressor math ``faultflow_compression`` keeps for
``faultflow_continuous``: the GF(2) solve and the curated polynomial lookup. The one-shot
decompressor's retarget, which this module used to hold, went with faultflow's retirement of
that decompressor (warptap 0.0.5 is the last release that has it).
"""

from __future__ import annotations

import pytest

from warptap.faultflow_compression import (
    PRIMITIVE_POLYNOMIALS,
    FaultflowCompressionError,
    lookup_polynomial,
    solve_xor_broadcast,
)


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


def test_lookup_polynomial_returns_the_curated_entry():
    for width, poly in PRIMITIVE_POLYNOMIALS.items():
        assert lookup_polynomial(width) is poly


def test_lookup_polynomial_unsupported_width_raises_named_error():
    with pytest.raises(FaultflowCompressionError, match="width 12"):
        lookup_polynomial(12)


def test_the_one_shot_retarget_is_gone():
    import warptap.faultflow_compression as module

    for name in ("retarget_compressed_faultflow_patterns", "solve_pattern_seed",
                 "care_bit_rows", "polynomial_from_manifest"):
        assert not hasattr(module, name), name
