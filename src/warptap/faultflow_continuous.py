"""faultflow's continuous-flow scan decompressor: its model and pattern retargeting (W1, next
to ``faultflow_compression.py``'s one-shot port).

The decompressor (``faultflow/scan/ring_generator.py`` and ``compression.py`` in faultflow;
ported here as understanding, not as an import): a Galois ring generator of ``poly.width``
stages, **cleared on the scan enable's rise** (the first shift of a load, after a clock with
scan enable low) and fed ``num_channels`` channel bits **on every shift** -- channel ``c`` is
XORed into ring stage ``c * width // num_channels`` -- and a phase shifter driving each chain's
scan-in from the ring. Shift ``t``'s scan-ins read the ring as shift ``t - 1`` left it (shift
0's: the cleared ring, 0). A load is ``warmup + max_chain_length`` shifts, so a pattern has
``num_channels x shifts`` free bits instead of one seed's: the first ``warmup`` shifts only
fill the ring (their scan-ins fall off the chains' far ends), and a pattern's ``load_seqs[chain][t]``
is the scan-in of shift ``warmup + t``.

A free bit is named by its shift and channel: bit ``t * num_channels + c`` is channel ``c`` of
the word applied at shift ``t``. :func:`continuous_scan_in_rows` gives every scan-in as a GF(2)
mask over them; :func:`solve_pattern_channels` solves a pattern's load over all of them at
once, strictly -- a don't-care is a ``load_care`` the pattern names, nothing else -- and
returns each channel's bit for each shift. :func:`retarget_continuous_faultflow_patterns` drives
them through the JTAG network: one clock pulse with scan enable low, then for each shift a
write of that shift's channel word and one pulse with scan enable high, then the capture and
the unload as in the one-shot flow.

``manifest["compression"]`` describes the decompressor: ``architecture`` (``"continuous"``;
absent or ``"one_shot"`` is the older decompressor, which
:func:`retarget_decompressed_faultflow_patterns` hands to ``faultflow_compression``),
``num_channels``, ``polynomial`` (``{"width", "taps"}``), ``phase_shifter_taps``, ``warmup``
and, as a check, ``injectors``.

Not retargeted here: a transition (launch) pattern, as in the one-shot flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from warptap.faultflow_compression import (
    FaultflowCompressionError,
    LfsrPolynomial,
    _step,
    _xor_reduce,
    polynomial_from_manifest,
    retarget_compressed_faultflow_patterns,
    solve_xor_broadcast,
)
from warptap.faultflow_retarget import (
    _instrument_width,
    _sequence_to_payload,
    _strip_unload_padding,
)
from warptap.icl_model import ModuleInstance, PhysicalGraph
from warptap.pdl_interpreter import PDLInterpreter
from warptap.tap_ir import PulsePin


def injector_stages(width: int, num_channels: int) -> List[int]:
    """The ring stage each channel bit XORs into on every shift: channel ``c`` into stage
    ``c * width // num_channels``, spread evenly -- ported from
    ``faultflow/scan/ring_generator.py::injector_stages``."""
    if not 1 <= num_channels <= width:
        raise FaultflowCompressionError(
            f"{num_channels} channels into a {width}-stage ring: need 1 to {width} channels"
        )
    return [(c * width) // num_channels for c in range(num_channels)]


@dataclass(frozen=True)
class ContinuousDecompressor:
    """What a pattern's load is made of: the ring (``poly``), the phase shifter (one tap
    list of ring stages per chain), the channel count and the warm-up shifts."""

    poly: LfsrPolynomial
    phase_shifter_taps: List[List[int]]
    num_channels: int
    warmup: int

    def __post_init__(self) -> None:
        injector_stages(self.poly.width, self.num_channels)
        if self.warmup < 1:
            raise FaultflowCompressionError(
                f"warmup is {self.warmup}: the clearing shift loads 0, so at least 1"
            )
        if any(
            not 0 <= tap < self.poly.width
            for taps in self.phase_shifter_taps
            for tap in taps
        ):
            raise FaultflowCompressionError(
                f"a phase-shifter tap is outside the {self.poly.width}-stage ring"
            )

    @property
    def injectors(self) -> List[int]:
        return injector_stages(self.poly.width, self.num_channels)

    def shifts(self, max_chain_length: int) -> int:
        """How many shifts a load takes: the warm-up and the longest chain's."""
        return self.warmup + max_chain_length


def continuous_scan_in_rows(
    poly: LfsrPolynomial,
    phase_shifter_taps: List[List[int]],
    num_channels: int,
    cycles: int,
) -> List[List[int]]:
    """``rows[t][chain]``: chain's scan-in at shift ``t``, for ``t`` in ``range(cycles)``,
    as a GF(2) bitmask over the channel bits (bit ``t * num_channels + c``: channel ``c`` of
    the word applied at shift ``t``) -- ported from
    ``faultflow/scan/ring_generator.py::continuous_scan_in_rows``."""
    injectors = injector_stages(poly.width, num_channels)
    state = [0] * poly.width
    rows: List[List[int]] = []
    for t in range(cycles):
        rows.append([_xor_reduce(state[k] for k in taps) for taps in phase_shifter_taps])
        state = _step(state, poly)
        for c, stage in enumerate(injectors):
            state[stage] ^= 1 << (t * num_channels + c)
    return rows


def continuous_from_manifest(compression: dict) -> ContinuousDecompressor:
    """The decompressor ``manifest["compression"]`` describes (see this module's docstring);
    a missing field, or ``injectors`` that are not the ones this model computes, is
    refused."""
    for key in ("polynomial", "phase_shifter_taps", "warmup", "num_channels"):
        if compression.get(key) is None:
            raise FaultflowCompressionError(
                f"the continuous decompressor's manifest entry has no {key}"
            )
    decompressor = ContinuousDecompressor(
        poly=polynomial_from_manifest(compression),
        phase_shifter_taps=[list(taps) for taps in compression["phase_shifter_taps"]],
        num_channels=int(compression["num_channels"]),
        warmup=int(compression["warmup"]),
    )
    stated = compression.get("injectors")
    if stated is not None and list(stated) != decompressor.injectors:
        raise FaultflowCompressionError(
            f"the manifest's injectors {list(stated)} are not the stages "
            f"{decompressor.injectors} this model injects the channels into"
        )
    return decompressor


def solve_pattern_channels(
    load_seqs: dict,
    decompressor: ContinuousDecompressor,
    max_chain_length: int,
    pattern_index: int,
    load_care: "Optional[set[tuple[int, int]]]" = None,
    launch_scan_in: "Optional[dict[int, bool]]" = None,
) -> dict:
    """One joint GF(2) system over every free bit of the load (``num_channels x shifts``),
    solved once: ``{channel: [bit for each shift]}`` driving the chains to ``load_seqs``.
    ``load_seqs[chain][t]`` is shift ``warmup + t``'s scan-in; ``load_care``, when not ``None``,
    keeps only the ``(chain, t)`` positions it names; ``launch_scan_in`` ({chain: bit}, a
    launch-on-shift pattern) constrains the scan-in of the shift after the load.
    Unconstrained bits are 0."""
    shifts = decompressor.shifts(max_chain_length)
    extra = 1 if launch_scan_in else 0
    rows = continuous_scan_in_rows(
        decompressor.poly,
        decompressor.phase_shifter_taps,
        decompressor.num_channels,
        shifts + extra,
    )
    solver_rows: List[int] = []
    rhs: List[bool] = []
    for chain_key, bits in load_seqs.items():
        chain_id = int(chain_key)
        if len(bits) != max_chain_length:
            raise FaultflowCompressionError(
                f"pattern {pattern_index}: chain {chain_id}'s load_seqs has {len(bits)} "
                f"bits, expected exactly max_chain_length={max_chain_length}"
            )
        for t, value in enumerate(bits):
            if load_care is not None and (chain_id, t) not in load_care:
                continue
            solver_rows.append(rows[decompressor.warmup + t][chain_id])
            rhs.append(bool(value))
    for chain_id, bit in (launch_scan_in or {}).items():
        solver_rows.append(rows[shifts][int(chain_id)])
        rhs.append(bool(bit))
    width = decompressor.num_channels * shifts
    solved = solve_xor_broadcast(solver_rows, rhs, width)
    if solved is None:
        raise FaultflowCompressionError(
            f"pattern {pattern_index}: no channel words jointly give the specified load "
            f"positions through this continuous decompressor ({decompressor.num_channels} "
            f"channels x {shifts} shifts of freedom)"
        )
    return {
        c: [
            bool((solved >> (t * decompressor.num_channels + c)) & 1)
            for t in range(shifts)
        ]
        for c in range(decompressor.num_channels)
    }


def retarget_continuous_faultflow_patterns(
    patterns: List[dict],
    chain_to_instrument: dict,
    compression_channel_instrument: str,
    graph: PhysicalGraph,
    root: ModuleInstance,
    *,
    decompressor: ContinuousDecompressor,
    max_chain_length: int,
    clock_port: str,
    scan_enable_port: str,
    clock_pulse_count: int = 1,
) -> list:
    """Retarget every pattern through a continuous-decompressor composed netlist's JTAG
    network: solve the pattern's channel words (:func:`solve_pattern_channels`), pulse the
    clock once with scan enable low (so the next edge's scan-enable rise clears the ring),
    then for each shift write that shift's channel word to ``compression_channel_instrument``
    (a ``num_channels``-bit WRITE instrument on the composed netlist's channel port) and pulse
    the clock once with scan enable high. The unload side is
    :mod:`warptap.faultflow_compression`'s: capture pulse (scan enable low, the pattern's
    ``capture_pi_values``), then a read of each raw ``scan_out_N`` chain."""
    pdl = PDLInterpreter(graph, root)
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
            channel_seqs = solve_pattern_channels(
                load_seqs, decompressor, max_chain_length, pattern_index, load_care
            )
            pdl.program.append(
                PulsePin(clock_port, 1, hold_pins=((scan_enable_port, 0),))
            )
            for t in range(decompressor.shifts(max_chain_length)):
                word = sum(
                    int(channel_seqs[c][t]) << c for c in range(decompressor.num_channels)
                )
                pdl.iTarget(compression_channel_instrument)
                pdl.iWrite(word)
                pdl.iApply()
                pdl.program.append(
                    PulsePin(clock_port, 1, hold_pins=((scan_enable_port, 1),))
                )

        if expected_unload:
            hold_pins = ((scan_enable_port, 0),) + tuple(
                (name, int(bool(value))) for name, value in capture_pi_values.items()
            )
            pdl.program.append(
                PulsePin(clock_port, clock_pulse_count, hold_pins=hold_pins)
            )

        for chain_id in sorted({int(k) for k in expected_unload}):
            if chain_id not in chain_to_instrument:
                raise FaultflowCompressionError(
                    f"faultflow chain {chain_id} has no entry in chain_to_instrument -- "
                    "every unload-side chain a pattern references must be mapped (known: "
                    f"{sorted(chain_to_instrument)})"
                )
            instrument_name = chain_to_instrument[chain_id]
            width = _instrument_width(graph, instrument_name)
            seq = _strip_unload_padding(
                [bool(b) for b in expected_unload[str(chain_id)]], width
            )
            pdl.iTarget(instrument_name)
            pdl.iRead(_sequence_to_payload(seq))
            pdl.iApply()

    return pdl.program


def retarget_decompressed_faultflow_patterns(
    patterns: List[dict],
    chain_to_instrument: dict,
    compression_channel_instrument: str,
    graph: PhysicalGraph,
    root: ModuleInstance,
    *,
    compression: dict,
    max_chain_length: int,
    clock_port: str,
    scan_enable_port: str,
    clock_pulse_count: int = 1,
) -> list:
    """Retarget through whichever decompressor ``manifest["compression"]`` says is on the
    chip: ``architecture == "continuous"`` the continuous one, anything else (absent: the
    older one) :func:`~warptap.faultflow_compression.retarget_compressed_faultflow_patterns`."""
    architecture = compression.get("architecture", "one_shot")
    if architecture == "continuous":
        return retarget_continuous_faultflow_patterns(
            patterns,
            chain_to_instrument,
            compression_channel_instrument,
            graph,
            root,
            decompressor=continuous_from_manifest(compression),
            max_chain_length=max_chain_length,
            clock_port=clock_port,
            scan_enable_port=scan_enable_port,
            clock_pulse_count=clock_pulse_count,
        )
    if architecture != "one_shot":
        raise FaultflowCompressionError(
            f"unknown decompressor architecture {architecture!r}"
        )
    return retarget_compressed_faultflow_patterns(
        patterns,
        chain_to_instrument,
        compression_channel_instrument,
        graph,
        root,
        poly=polynomial_from_manifest(compression),
        phase_shifter_taps=[list(t) for t in compression["phase_shifter_taps"]],
        max_chain_length=max_chain_length,
        clock_port=clock_port,
        scan_enable_port=scan_enable_port,
        clock_pulse_count=clock_pulse_count,
    )


__all__ = [
    "ContinuousDecompressor",
    "continuous_from_manifest",
    "continuous_scan_in_rows",
    "injector_stages",
    "retarget_continuous_faultflow_patterns",
    "retarget_decompressed_faultflow_patterns",
    "solve_pattern_channels",
]
