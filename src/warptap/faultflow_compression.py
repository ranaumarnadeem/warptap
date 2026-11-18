"""The scan decompressor math warptap ports from faultflow: the Galois-form LFSR ring generator's
polynomial and next-state function, and a GF(2) Gauss-Jordan solve.
:mod:`warptap.faultflow_continuous` builds faultflow's continuous-flow decompressor from them.

**Port understanding, not code** (this project's own established boundary with faultflow,
``tapestry_handoff.md``): the math below is a native, hand-written reimplementation, cited from
faultflow's real source at every step, never an import of or call into faultflow itself.

- ``LfsrPolynomial``/``PRIMITIVE_POLYNOMIALS``/``lookup_polynomial``/``_step`` --
  ``faultflow/scan/ring_generator.py``. faultflow writes the campaign's real polynomial into
  ``manifest["compression"]["polynomial"]``, which :func:`warptap.faultflow_continuous.
  continuous_from_manifest` reads; ``test_faultflow_compression_polynomial_table.py`` holds the
  curated table to faultflow's own whenever a faultflow checkout is available.
- The GF(2) Gauss-Jordan solve -- ``src/core/scan/compression.cpp``'s ``solve_xor_broadcast``
  (a pure algorithm over plain ``vector<bool>`` rows, reimplemented in pure Python as
  :func:`solve_xor_broadcast`).

**The one-shot decompressor's retarget is gone.** This module used to retarget faultflow's
one-shot decompressor (one seed per pattern, reseeded on the scan enable's rise:
``retarget_compressed_faultflow_patterns``, ``solve_pattern_seed``, ``care_bit_rows``,
``polynomial_from_manifest``). faultflow retired that decompressor (commit ``c665de9``,
October 2026), so no faultflow since writes a manifest for it, and warptap 0.0.5 is the last
release that retargets one. ``FaultflowCompressionError`` stays here, where faultflow's tests
import it from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from warptap.errors import WarptapError


class FaultflowCompressionError(WarptapError):
    """Raised by the decompressor model and its retargeting (:mod:`warptap.faultflow_continuous`):
    a manifest it can't read, a polynomial width with no curated tap set, a pattern whose load
    has no solution through the decompressor, or a chain missing from ``chain_to_instrument``."""


@dataclass(frozen=True)
class LfsrPolynomial:
    """A Galois-form (internal-XOR) LFSR feedback structure -- ported verbatim (structure and
    field names) from ``faultflow/scan/ring_generator.py::LfsrPolynomial``. ``taps`` are stage
    indices in ``{1, ..., width-1}`` that get an extra feedback XOR gate on their input (stage 0
    always receives the raw feedback bit with no XOR -- see :func:`_step`). The feedback bit is
    always ``state[width-1]``."""

    width: int
    taps: frozenset[int]


# Curated maximal-length LFSR feedback polynomials -- ported verbatim from
# faultflow/scan/ring_generator.py::PRIMITIVE_POLYNOMIALS (decades-old, public-domain,
# never-patented tap sets; see that module's own docstring for the literature citations).
# faultflow still uses this table for its ring; test_faultflow_compression_polynomial_table.py
# asserts this copy matches it at every width, when a faultflow checkout is present.
PRIMITIVE_POLYNOMIALS: dict[int, LfsrPolynomial] = {
    8: LfsrPolynomial(8, frozenset({4, 5, 6})),
    16: LfsrPolynomial(16, frozenset({4, 13, 15})),
    32: LfsrPolynomial(32, frozenset({1, 2, 22})),
    64: LfsrPolynomial(64, frozenset({60, 61, 63})),
}


def lookup_polynomial(width: int) -> LfsrPolynomial:
    """Return the curated maximal-length polynomial for ``width`` -- ported from
    ``faultflow/scan/ring_generator.py::lookup_polynomial``. Scope deliberately bounded to the
    tabulated widths above, exactly matching faultflow's own scope."""
    if width not in PRIMITIVE_POLYNOMIALS:
        raise FaultflowCompressionError(
            f"no curated primitive-polynomial tap set for width {width}; "
            f"supported widths: {sorted(PRIMITIVE_POLYNOMIALS)}"
        )
    return PRIMITIVE_POLYNOMIALS[width]


def _step(rows: list[int], poly: LfsrPolynomial) -> list[int]:
    """Advance one shift cycle -- ported verbatim from
    ``faultflow/scan/ring_generator.py::_step``. Works identically whether ``rows[i]`` holds a
    raw 0/1 bit or a GF(2) coefficient bitmask over seed bits, since the XOR recurrence is
    linear."""
    fb = rows[poly.width - 1]
    new = [0] * poly.width
    new[0] = fb
    for i in range(1, poly.width):
        new[i] = rows[i - 1] ^ (fb if i in poly.taps else 0)
    return new


def _xor_reduce(values: Iterable[int]) -> int:
    acc = 0
    for v in values:
        acc ^= v
    return acc


def solve_xor_broadcast(rows: List[int], rhs: List[bool], width: int) -> "int | None":
    """Pure-Python Gauss-Jordan elimination over GF(2), ported from
    ``src/core/scan/compression.cpp::solve_xor_broadcast`` (that function's own ``vector<bool>``
    row/column shape, reimplemented here with each row packed as a single Python int: bits
    ``0..width-1`` are the row's own K coefficient columns, bit ``width`` is its RHS). Returns a
    satisfying ``width``-bit seed (free/unpivoted channels default to 0, matching the C++
    original) or ``None`` if the system is contradictory."""
    augmented = [
        (row & ((1 << width) - 1)) | ((1 << width) if r else 0)
        for row, r in zip(rows, rhs)
    ]
    pivot_col_for_row = [-1] * len(augmented)
    pivot_row = 0
    for col in range(width):
        if pivot_row >= len(augmented):
            break
        sel = pivot_row
        while sel < len(augmented) and not (augmented[sel] >> col) & 1:
            sel += 1
        if sel == len(augmented):
            continue
        augmented[pivot_row], augmented[sel] = augmented[sel], augmented[pivot_row]
        for r in range(len(augmented)):
            if r != pivot_row and (augmented[r] >> col) & 1:
                augmented[r] ^= augmented[pivot_row]
        pivot_col_for_row[pivot_row] = col
        pivot_row += 1

    for r in range(pivot_row, len(augmented)):
        coeffs = augmented[r] & ((1 << width) - 1)
        rhs_bit = (augmented[r] >> width) & 1
        if coeffs == 0 and rhs_bit:
            return None

    seed = 0
    for r in range(pivot_row):
        col = pivot_col_for_row[r]
        rhs_bit = (augmented[r] >> width) & 1
        if rhs_bit:
            seed |= 1 << col
    return seed


__all__ = [
    "FaultflowCompressionError",
    "LfsrPolynomial",
    "PRIMITIVE_POLYNOMIALS",
    "lookup_polynomial",
    "solve_xor_broadcast",
]
