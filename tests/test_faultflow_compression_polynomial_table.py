"""Cross-check: this project's own ported PRIMITIVE_POLYNOMIALS copy
(warptap.faultflow_compression) against faultflow's real, live table -- guards against the
drift risk warptap.faultflow_compression's own module docstring used to flag for EVERY manifest
(a hand-ported copy had no other mechanism keeping it in sync with faultflow's real source).
Skips (not fails) when no faultflow checkout is available, mirroring every other external-project
cross-check in this suite (autombist_generator, icl_parser_module, etc.) -- this is still a real
gap for a manifest captured before faultflow started serializing the polynomial directly (see
below), which is the only case that still uses this curated table.

Also covers the manifest-driven path (``polynomial_from_manifest``) that closed that drift risk
for a current manifest, with a hand-built manifest dict -- real CI coverage that does not depend
on a faultflow checkout being present."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from warptap.faultflow_compression import (
    PRIMITIVE_POLYNOMIALS as WARPTAP_POLYS,
    FaultflowCompressionError,
    LfsrPolynomial,
    lookup_polynomial,
    polynomial_from_manifest,
)


@pytest.fixture
def real_faultflow_polynomials(faultflow_dir: Path):
    src_dir = str(faultflow_dir)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from faultflow.scan.ring_generator import PRIMITIVE_POLYNOMIALS as REAL_POLYS
    except ImportError as exc:
        pytest.skip(
            f"faultflow.scan.ring_generator not importable from {src_dir}: {exc}"
        )
    return REAL_POLYS


def test_ported_polynomial_table_matches_faultflows_real_table(
    real_faultflow_polynomials,
):
    assert set(WARPTAP_POLYS) == set(real_faultflow_polynomials)
    for width, warptap_poly in WARPTAP_POLYS.items():
        real_poly = real_faultflow_polynomials[width]
        assert warptap_poly.width == real_poly.width
        assert warptap_poly.taps == real_poly.taps


def test_polynomial_from_manifest_uses_the_serialized_field_directly():
    compression = {
        "num_channels": 8,
        "polynomial": {"width": 8, "taps": [2, 3, 4]},
    }
    assert polynomial_from_manifest(compression) == LfsrPolynomial(
        8, frozenset({2, 3, 4})
    )


def test_polynomial_from_manifest_ignores_num_channels_when_polynomial_present():
    # A hand-built manifest whose "polynomial" field deliberately disagrees with what
    # lookup_polynomial(num_channels) would return, to prove the real field wins outright
    # rather than merely supplementing the curated-table lookup.
    compression = {
        "num_channels": 8,
        "polynomial": {"width": 8, "taps": [0, 1, 7]},
    }
    assert polynomial_from_manifest(compression) != lookup_polynomial(8)
    assert polynomial_from_manifest(compression) == LfsrPolynomial(
        8, frozenset({0, 1, 7})
    )


def test_polynomial_from_manifest_falls_back_for_a_manifest_without_the_field():
    compression = {"num_channels": 16}
    assert polynomial_from_manifest(compression) == lookup_polynomial(16)


def test_polynomial_from_manifest_fallback_still_raises_for_unsupported_width():
    compression = {"num_channels": 12}
    with pytest.raises(FaultflowCompressionError, match="width 12"):
        polynomial_from_manifest(compression)
