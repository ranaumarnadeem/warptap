"""Cross-check: this project's own ported PRIMITIVE_POLYNOMIALS copy
(warptap.faultflow_compression) against faultflow's real, live table -- guards against the
drift risk warptap.faultflow_compression's own module docstring flags (a hand-ported copy has
no other mechanism keeping it in sync with faultflow's real source). Skips (not fails) when no
faultflow checkout is available, mirroring every other external-project cross-check in this
suite (autombist_generator, icl_parser_module, etc.)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from warptap.faultflow_compression import PRIMITIVE_POLYNOMIALS as WARPTAP_POLYS


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
