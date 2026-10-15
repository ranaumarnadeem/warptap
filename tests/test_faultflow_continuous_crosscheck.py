"""Cross-check: this project's own port of the continuous decompressor's model
(warptap.faultflow_continuous) against faultflow's real, live one -- the injector stages and
every scan-in row of the ring, for the ring widths, channel counts and phase shifters faultflow
actually builds. Skips (not fails) when no faultflow checkout (or one without the continuous
decompressor) is available, like every external-project cross-check in this suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from warptap.faultflow_compression import LfsrPolynomial
from warptap.faultflow_continuous import (
    continuous_scan_in_rows,
    injector_stages,
)


@pytest.fixture
def real_ring(faultflow_dir: Path):
    src_dir = str(faultflow_dir)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from faultflow.scan import ring_generator as real
        from faultflow.scan.compression import build_phase_shifter
    except ImportError as exc:
        pytest.skip(f"faultflow's continuous decompressor not importable: {exc}")
    if not hasattr(real, "continuous_scan_in_rows"):
        pytest.skip("this faultflow checkout predates the continuous decompressor")
    return real, build_phase_shifter


@pytest.mark.parametrize("channels", [1, 2, 3, 4, 8, 16])
def test_the_model_matches_faultflows_for_the_widths_and_chains_it_builds(
    real_ring, channels
):
    real, build_phase_shifter = real_ring
    width = real.default_ring_width(channels)
    real_poly = real.PRIMITIVE_POLYNOMIALS[width]
    poly = LfsrPolynomial(real_poly.width, frozenset(real_poly.taps))
    assert injector_stages(width, channels) == real.injector_stages(width, channels)
    for chains in (1, 3, 8, 20):
        taps = build_phase_shifter(width, chains)
        cycles = real.default_warmup(width, channels) + 10
        assert continuous_scan_in_rows(
            poly, taps, channels, cycles
        ) == real.continuous_scan_in_rows(real_poly, taps, channels, cycles)
