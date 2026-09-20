"""faultflow scan-*compression*-aware pattern retargeting (Stage 24, built on Stage 14's
``faultflow_retarget.py``). Closes a gap that module's own ``chain_to_instrument`` model can't
express: when a faultflow design has ``[compression]`` enabled
(``faultflow/scan/compression.py``), its raw ``scan_in_N`` chain ports become INTERNAL wires on
the composed netlist -- the real external load-side pins are a K-bit-wide ``tdi`` channel bus
feeding a Galois-form LFSR ring generator, expanded to each chain via a static XOR phase
shifter. ``chain_to_instrument``'s naive 1:1 raw-chain-to-instrument map has nothing to bind to
on this side of a compressed design.

**Port understanding, not code** (this project's own established boundary with faultflow,
``tapestry_handoff.md``, already applied by Stage 14 for bit-order/padding rules): the GF(2)
math below is a native, hand-written reimplementation, cited from faultflow's real source at
every step, never an import of or call into faultflow itself.

Ported, verbatim in structure, from real faultflow source (re-verified directly against the
checkout at ``C:\\Users\\Potato\\Desktop\\faultflow`` this session, not from memory):

- ``PRIMITIVE_POLYNOMIALS``/``LfsrPolynomial``/``lookup_polynomial``/``_step``/``care_bit_rows``
  -- ``faultflow/scan/ring_generator.py``. As of faultflow commit ``26e1f96`` (branch
  ``compress``, ``faultflow/runner/runner.py``'s ``scan_compress()``),
  ``manifest["compression"]["polynomial"]`` now serializes the campaign's real polynomial
  directly as ``{"width": int, "taps": [int, ...]}`` (straight from the real
  ``CompressionMap.polynomial``, an ``LfsrPolynomial``) -- see :func:`polynomial_from_manifest`,
  the preferred way to obtain ``poly``, for a manifest that carries this field. The curated
  ``PRIMITIVE_POLYNOMIALS`` table and ``lookup_polynomial`` remain only as a FALLBACK, for a
  manifest captured before this field existed (no ``"polynomial"`` key present); a dedicated
  test (``test_faultflow_compression_polynomial_table.py``) still cross-checks that curated copy
  against faultflow's own real table whenever a faultflow checkout is available, and separately
  covers the manifest-driven path with a hand-built manifest so that coverage doesn't depend on
  a checkout being present.
- The GF(2) Gauss-Jordan solve -- ``src/core/scan/compression.cpp``'s ``solve_xor_broadcast``
  (a pure algorithm over plain ``vector<bool>`` rows, no faultflow/C++ dependency of any kind;
  confirmed reimplementable in pure Python, exactly what :func:`solve_xor_broadcast` here does).

**Load-side physical timing** -- re-derived directly from the real RTL
``faultflow.scan.compression.ring_generator_wrapper_verilog`` emits (re-verified this session,
not assumed):
``reseed = scan_enable_port & ~prev_scan_en``; ``prev_scan_en`` is updated UNCONDITIONALLY every
``clock_port`` edge (``prev_scan_en <= scan_enable_port;``, not gated by any ``if``);
``lfsr_reg`` only updates on an edge where ``scan_enable_port`` is high. So: one clock edge with
``scan_enable_port`` held LOW (idle-settle, guarantees ``prev_scan_en=0`` regardless of prior
state) followed by ``max_chain_length`` edges with ``scan_enable_port`` held HIGH reproduces
exactly one reseed (the first of those edges) followed by ``max_chain_length - 1`` edges of
natural LFSR feedback stepping -- matching ``care_bit_rows``'s own ``state(0) == seed``
convention precisely.

**A limitation of** :func:`solve_pattern_seed`, **closed for a manifest that supplies**
``load_care``: as of faultflow commit ``9b670df`` (branch ``compress``,
``faultflow/scan/protocol.py``/``faultflow/scan/detection_pipeline.py``/
``faultflow/scan/pattern_export.py``), an exported pattern dict may carry
``"load_care": [[chain_id, cycle], ...] | None``. Present (a list), for a SAT-ATPG-accepted,
compression-enabled candidate, it names exactly the ``(chain_id, cycle)`` positions
``detection_pipeline.py``'s ``_check_compression_satisfiable`` (via
``faultflow/scan/care_bits.py``'s ``extract_scan_care_bits``) proved necessary for the fault(s)
that pattern detects -- every other specified ``load_seqs`` position is a genuine don't-care.
:func:`solve_pattern_seed` takes this as its own ``load_care`` argument and, when given, builds
the joint GF(2) system using ONLY those positions, so a don't-care position's arbitrary fill
value can no longer cause a false "unsatisfiable" result.

``load_care`` is ``null``/absent -- and :func:`solve_pattern_seed` falls back to its original,
more conservative behavior of treating EVERY specified ``load_seqs`` position as a hard
constraint -- for a manifest exported by an older faultflow version that predates this field, a
random-fill pattern (not SAT-targeted, so ``extract_scan_care_bits`` never ran for it), or a
non-compression campaign. Confirmed directly against ``care_bits.py``: faultflow's own
extraction is a flip/re-simulate don't-care search requiring a live fault-detection oracle this
module has no access to (and shouldn't attempt to rebuild -- that's real ATPG machinery, out of
scope for a pattern-translation layer), so this fallback remains a conservative,
ALWAYS-CORRECT-WHEN-IT-SUCCEEDS approach (any seed it finds exactly reproduces the pattern), but
it can, in principle, report a pattern unsatisfiable that ATPG's own reduced care-bit solve
would have accepted, if a don't-care position's arbitrary filled value happens not to lie on the
same LFSR trajectory as the seed that satisfies the real care bits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Union

from warptap.errors import WarptapError
from warptap.faultflow_retarget import (
    _instrument_width,
    _sequence_to_payload,
    _strip_unload_padding,
)
from warptap.icl_model import ModuleInstance, PhysicalGraph
from warptap.pdl_interpreter import PDLInterpreter
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest, PulsePin]


class FaultflowCompressionError(WarptapError):
    """Raised for a faultflow chain id missing from ``chain_to_instrument`` (unload side,
    mirrors :class:`~warptap.faultflow_retarget.FaultflowRetargetError`), a ``load_seqs`` entry
    whose length doesn't match ``max_chain_length`` (a compression-enabled campaign's own
    protocol invariant -- see this module's docstring), or a pattern whose specified load
    content has no jointly-satisfying K-bit seed through this decompressor."""


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
# never-patented tap sets; see that module's own docstring for the literature citations). Used
# only as a FALLBACK now, via lookup_polynomial, for a manifest captured before faultflow started
# serializing the real polynomial into manifest["compression"]["polynomial"] -- see
# polynomial_from_manifest and this module's own docstring. A dedicated cross-check test
# (test_faultflow_compression_polynomial_table.py) asserts this copy matches faultflow's own real
# table at every available width, when a faultflow checkout is present.
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


def polynomial_from_manifest(compression: dict) -> LfsrPolynomial:
    """Build the campaign's real :class:`LfsrPolynomial` from ``manifest["compression"]`` --
    the preferred way to obtain ``poly``, ahead of :func:`lookup_polynomial`. As of faultflow
    commit ``26e1f96`` (branch ``compress``, ``faultflow/runner/runner.py``'s
    ``scan_compress()``), ``compression["polynomial"]`` carries ``{"width": int, "taps": [int,
    ...]}`` straight from the real ``CompressionMap.polynomial`` (see this module's own
    docstring) -- when present, that's used directly, with no curated-table lookup or drift risk
    of any kind. Falls back to :func:`lookup_polynomial` on ``compression["num_channels"]`` for a
    manifest captured before faultflow started serializing this field (no ``"polynomial"`` key
    present)."""
    polynomial = compression.get("polynomial")
    if polynomial is None:
        return lookup_polynomial(compression["num_channels"])
    return LfsrPolynomial(polynomial["width"], frozenset(polynomial["taps"]))


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


def care_bit_rows(
    poly: LfsrPolynomial, phase_shifter_taps: List[List[int]], max_chain_length: int
) -> List[List[int]]:
    """``out[t][c]`` = GF(2) coefficient bitmask (over the K seed/channel bits) for scan chain
    ``c``'s ``scan_in`` value at shift cycle ``t`` -- ported verbatim from
    ``faultflow/scan/ring_generator.py::care_bit_rows``. Static/content-independent: depends
    only on ``(poly, phase_shifter_taps, max_chain_length)``, all available from
    ``manifest["compression"]`` plus this module's own ported polynomial table."""
    rows = [1 << i for i in range(poly.width)]
    out: List[List[int]] = []
    for _ in range(max_chain_length):
        out.append([_xor_reduce(rows[k] for k in taps) for taps in phase_shifter_taps])
        rows = _step(rows, poly)
    return out


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


def solve_pattern_seed(
    load_seqs: dict,
    rows: List[List[int]],
    width: int,
    pattern_index: int,
    load_care: "set[tuple[int, int]] | None" = None,
) -> int:
    """Build ONE joint GF(2) system and solve once for a single ``width``-bit seed --
    compression's load side is a joint, all-or-nothing constraint (one shared LFSR state feeds
    every chain from one seed; confirmed via ``detection_pipeline.py``'s own
    ``_check_compression_satisfiable`` docstring), never a per-chain-independent transform.

    ``load_care``, when not ``None``, restricts the system to exactly the ``(chain_id, cycle)``
    positions it names -- faultflow's own ATPG-proved care-bit subset (``ScanPattern.load_care``,
    see this module's own docstring); every other specified ``load_seqs`` position is then a
    genuine don't-care, left out of the solve entirely rather than treated as a hard constraint.
    When ``None`` (a manifest exported before ``load_care`` existed, or a random-fill pattern),
    every specified position is used, this function's original, more conservative behavior.

    Confirmed directly against ``faultflow/scan/care_bits.py::extract_scan_care_bits``: a
    compression-enabled campaign's ``load_seqs[chain_id]`` is indexed DIRECTLY by cycle
    (``range(max_chain_length)``, no front-padding-to-instrument-width stripping needed or
    applicable here, unlike :mod:`warptap.faultflow_retarget`'s raw-chain case) -- so
    ``rows[cycle][chain_id]`` (this module's own :func:`care_bit_rows` output) lines up 1:1 with
    ``load_seqs[str(chain_id)][cycle]`` with no offset math, and equally with ``load_care``'s own
    ``(chain_id, cycle)`` pairs."""
    solver_rows: List[int] = []
    rhs: List[bool] = []
    for chain_key, bits in load_seqs.items():
        chain_id = int(chain_key)
        if len(bits) != len(rows):
            raise FaultflowCompressionError(
                f"pattern {pattern_index}: chain {chain_id}'s load_seqs has {len(bits)} "
                f"bits, expected exactly max_chain_length={len(rows)} for a "
                "compression-enabled campaign"
            )
        for cycle, value in enumerate(bits):
            if load_care is not None and (chain_id, cycle) not in load_care:
                continue
            solver_rows.append(rows[cycle][chain_id])
            rhs.append(bool(value))
    seed = solve_xor_broadcast(solver_rows, rhs, width)
    if seed is None:
        if load_care is not None:
            raise FaultflowCompressionError(
                f"pattern {pattern_index}: no {width}-bit seed jointly satisfies its "
                "extracted load_care positions through this compression decompressor -- "
                "faultflow's own ATPG already proved this exact care-bit subset satisfiable, "
                "so this points at a data or polynomial/phase-shifter mismatch, not the "
                "don't-care-position limitation"
            )
        raise FaultflowCompressionError(
            f"pattern {pattern_index}: no {width}-bit seed jointly satisfies every "
            "specified load_seqs position through this compression decompressor -- see "
            "this module's own docstring on the don't-care-position limitation"
        )
    return seed


def retarget_compressed_faultflow_patterns(
    patterns: List[dict],
    chain_to_instrument: dict,
    compression_channel_instrument: str,
    graph: PhysicalGraph,
    root: ModuleInstance,
    *,
    poly: LfsrPolynomial,
    phase_shifter_taps: List[List[int]],
    max_chain_length: int,
    clock_port: str,
    scan_enable_port: str,
    clock_pulse_count: int = 1,
) -> List[_IrOp]:
    """Retarget every pattern in ``patterns`` through a compression-enabled composed netlist's
    real JTAG network: the LOAD side is entirely replaced (solve a joint seed, write it to
    ``compression_channel_instrument`` -- a K-bit WRITE instrument bound to the composed
    netlist's own ``tdi`` channel port -- then physically clock the reseed + natural LFSR
    stepping, per this module's own docstring); the UNLOAD side is untouched, reusing
    :mod:`warptap.faultflow_retarget`'s own per-chain ``chain_to_instrument`` mechanism
    verbatim, since compression never touches ``scan_out_N`` (those stay individually,
    raw-chain addressable on a compression-only composed netlist).

    ``poly``/``phase_shifter_taps``/``max_chain_length`` come from
    ``manifest["compression"]`` (``phase_shifter_taps`` directly; ``poly`` via this module's own
    :func:`polynomial_from_manifest`, which reads the real
    ``manifest["compression"]["polynomial"]`` field faultflow now serializes -- or falls back to
    :func:`lookup_polynomial` on ``manifest["compression"]["num_channels"]`` for a manifest
    captured before that field existed; see this module's docstring).
    ``clock_port``/``scan_enable_port`` must match ``manifest["compression"]["clock_port"]``/
    ``["scan_enable_port"]``.

    Raises :class:`FaultflowCompressionError` naming the offending chain id (unload side,
    mirroring :class:`~warptap.faultflow_retarget.FaultflowRetargetError`) or pattern index
    (load side, see :func:`solve_pattern_seed`)."""
    pdl = PDLInterpreter(graph, root)
    rows = care_bit_rows(poly, phase_shifter_taps, max_chain_length)

    for pattern_index, pattern in enumerate(patterns):
        load_seqs: dict = pattern.get("load_seqs", {})
        expected_unload: dict = pattern.get("expected_unload", {})
        capture_pi_values: dict = pattern.get("capture_pi_values", {})
        load_care_raw = pattern.get("load_care")
        load_care = (
            {(int(chain), int(cycle)) for chain, cycle in load_care_raw}
            if load_care_raw is not None
            else None
        )

        if load_seqs:
            seed = solve_pattern_seed(
                load_seqs, rows, poly.width, pattern_index, load_care
            )
            pdl.iTarget(compression_channel_instrument)
            pdl.iWrite(seed)
            pdl.iApply()
            # Idle-settle: guarantees prev_scan_en=0 at the next edge regardless of whatever
            # this PDLInterpreter's own prior pattern left the design's scan_enable_port
            # signal implying -- see module docstring for why this one edge suffices.
            pdl.program.append(
                PulsePin(clock_port, 1, hold_pins=((scan_enable_port, 0),))
            )
            # Cycle 0 of this pulse is the reseed edge; the remaining max_chain_length - 1
            # edges free-run the LFSR -- matches care_bit_rows's own state(0) == seed
            # convention exactly.
            pdl.program.append(
                PulsePin(
                    clock_port, max_chain_length, hold_pins=((scan_enable_port, 1),)
                )
            )

        if expected_unload:
            hold_pins = ((scan_enable_port, 0),) + tuple(
                (name, int(bool(value))) for name, value in capture_pi_values.items()
            )
            pdl.program.append(
                PulsePin(clock_port, clock_pulse_count, hold_pins=hold_pins)
            )

        for chain_id in sorted({int(k) for k in expected_unload}):
            key = str(chain_id)
            if chain_id not in chain_to_instrument:
                raise FaultflowCompressionError(
                    f"faultflow chain {chain_id} has no entry in chain_to_instrument -- "
                    "every unload-side chain a pattern references must be mapped (known: "
                    f"{sorted(chain_to_instrument)})"
                )
            instrument_name = chain_to_instrument[chain_id]
            width = _instrument_width(graph, instrument_name)
            seq = _strip_unload_padding([bool(b) for b in expected_unload[key]], width)
            pdl.iTarget(instrument_name)
            pdl.iRead(_sequence_to_payload(seq))
            pdl.iApply()

    return pdl.program


__all__ = [
    "FaultflowCompressionError",
    "LfsrPolynomial",
    "PRIMITIVE_POLYNOMIALS",
    "lookup_polynomial",
    "polynomial_from_manifest",
    "care_bit_rows",
    "solve_xor_broadcast",
    "solve_pattern_seed",
    "retarget_compressed_faultflow_patterns",
]
