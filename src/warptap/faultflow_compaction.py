"""faultflow scan-*compaction*-aware pattern retargeting (Stage 24, built on Stage 14's
``faultflow_retarget.py``). Closes a gap that module's own ``chain_to_instrument`` model can't
express: when a faultflow design has ``[compaction]`` enabled
(``faultflow/scan/compaction.py``), its raw ``scan_out_N`` chain ports become INTERNAL wires on
the composed netlist -- the real external unload-side pins are a K-bit-wide ``tdo`` channel bus,
a purely combinational, registerless XOR-tree fold of every internal chain's CURRENT output bit.
``chain_to_instrument``'s naive 1:1 raw-chain-to-instrument map has nothing to bind to on this
side of a compacted design.

**Port understanding, not code** (see :mod:`warptap.faultflow_compression`'s own docstring for
the full statement of this project's established boundary with faultflow -- applies identically
here).

No GF(2) algorithm needs porting for this module at all, unlike the compression side: compaction
is registerless (confirmed, ``faultflow/scan/compaction.py``'s own module docstring: "no
register, no clock, no sequential accumulation"), so predicting the compacted output at any
unload cycle is a pure, independent-per-cycle forward XOR fold, and the fold MAP itself
(``fanout[o]`` = internal chain indices XORed into output bit ``o``) is already directly
serialized as real data in ``manifest["compaction"]["fanout"]`` (confirmed,
``faultflow/runner/runner.py``) -- read it verbatim, nothing to reconstruct or re-derive.

**Unload-side physical protocol**: because compaction has no register, ``tdo``'s live value at
any instant equals the XOR fold of whatever the composed netlist's real internal chain-output
nets currently hold -- it must be SAMPLED every unload cycle, not read once at the end (unlike
:mod:`warptap.faultflow_retarget`'s per-chain unload, which reads a whole chain's content in one
``iApply`` via its own instrument abstraction). Per cycle ``t``: predict the expected K-bit
``tdo`` value from ``expected_unload``'s own content at cycle ``t`` folded through ``fanout``,
sample it (``iTarget``/``iRead``/``iApply`` against the K-bit READ instrument bound to the
composed netlist's ``tdo`` port), then physically clock the design's own scan shift register
forward one position (``PulsePin(clock_port, 1, hold_pins=((scan_enable_port, 1),))``) before the
next cycle's sample -- ``scan_enable_port`` must be held HIGH for this pulse (unlike the
capture-phase pulse) so the plain scan-stitched chains actually shift instead of staying in
functional/capture mode.

Confirmed directly against ``faultflow/scan/detection_pipeline.py``'s own
``_compaction_diff_observable`` (the ATPG-side consumer of this exact fold): both
``expected_unload[chain_id]`` and the fold itself are indexed DIRECTLY by cycle
(``range(max_chain_length)``), no front/back padding-to-width stripping needed or applicable
here -- unlike :mod:`warptap.faultflow_retarget`'s raw-chain case, which does need it.
"""

from __future__ import annotations

from typing import List, Union

from warptap.errors import WarptapError
from warptap.faultflow_retarget import (
    _instrument_width,
    _sequence_to_payload,
    _strip_load_padding,
)
from warptap.icl_model import ModuleInstance, PhysicalGraph
from warptap.pdl_interpreter import PDLInterpreter
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest, PulsePin]


class FaultflowCompactionError(WarptapError):
    """Raised for a faultflow chain id missing from ``chain_to_instrument`` (load side, mirrors
    :class:`~warptap.faultflow_retarget.FaultflowRetargetError`), or an ``expected_unload`` chain
    index in ``fanout`` that a given pattern's own data doesn't cover."""


def _expected_channel_value(
    expected_unload: dict, fanout: List[List[int]], cycle: int
) -> int:
    """The K-bit expected ``tdo`` payload at unload cycle ``cycle``: bit ``o`` is the XOR fold,
    over ``fanout[o]``'s chain indices, of each chain's ``expected_unload`` bit at this same
    cycle -- the exact fold ``faultflow/scan/detection_pipeline.py``'s own
    ``_compaction_diff_observable`` performs, applied here to a prediction instead of a real
    diff. Raises :class:`FaultflowCompactionError` naming the missing chain if ``fanout``
    references one ``expected_unload`` doesn't cover."""
    value = 0
    for o, chains in enumerate(fanout):
        bit = 0
        for chain_id in chains:
            key = str(chain_id)
            if key not in expected_unload:
                raise FaultflowCompactionError(
                    f"compactor output {o} folds chain {chain_id}, but this pattern's "
                    f"expected_unload has no entry for it (known: {sorted(expected_unload)})"
                )
            bit ^= int(bool(expected_unload[key][cycle]))
        value |= bit << o
    return value


def retarget_compacted_faultflow_patterns(
    patterns: List[dict],
    chain_to_instrument: dict,
    compaction_channel_instrument: str,
    graph: PhysicalGraph,
    root: ModuleInstance,
    *,
    fanout: List[List[int]],
    clock_port: str,
    scan_enable_port: str,
    clock_pulse_count: int = 1,
) -> List[_IrOp]:
    """Retarget every pattern in ``patterns`` through a compaction-enabled composed netlist's
    real JTAG network: the LOAD side is untouched, reusing
    :mod:`warptap.faultflow_retarget`'s own per-chain ``chain_to_instrument`` mechanism
    verbatim (compaction never touches ``scan_in_N``, those stay individually addressable on a
    compaction-only composed netlist); the UNLOAD side is entirely replaced -- per-cycle
    sampling of ``compaction_channel_instrument`` (a K-bit READ instrument bound to the composed
    netlist's own ``tdo`` channel port) against a fold of ``expected_unload`` through
    ``fanout``, interleaved with physical shift-clock pulses (see module docstring).

    ``fanout`` should come straight from ``manifest["compaction"]["fanout"]`` (already real
    data, nothing to reconstruct). ``clock_port``/``scan_enable_port`` must match the design's
    real scan clock/enable ports (the same ones ``manifest["compression"]`` would name for a
    compression-enabled design, or the plain design's own scan ports if compaction is the only
    transform active).

    Raises :class:`FaultflowCompactionError` naming the offending chain id (load side, mirroring
    :class:`~warptap.faultflow_retarget.FaultflowRetargetError`) or an unfolded chain
    (unload side, see :func:`_expected_channel_value`)."""
    pdl = PDLInterpreter(graph, root)

    for pattern in patterns:
        load_seqs: dict = pattern.get("load_seqs", {})
        expected_unload: dict = pattern.get("expected_unload", {})
        capture_pi_values: dict = pattern.get("capture_pi_values", {})

        for chain_key, bits in load_seqs.items():
            chain_id = int(chain_key)
            if chain_id not in chain_to_instrument:
                raise FaultflowCompactionError(
                    f"faultflow chain {chain_id} has no entry in chain_to_instrument -- "
                    "every load-side chain a pattern references must be mapped (known: "
                    f"{sorted(chain_to_instrument)})"
                )
            instrument_name = chain_to_instrument[chain_id]
            width = _instrument_width(graph, instrument_name)
            seq = _strip_load_padding([bool(b) for b in bits], width)
            pdl.iTarget(instrument_name)
            pdl.iWrite(_sequence_to_payload(seq))
            pdl.iApply()

        if expected_unload:
            hold_pins = tuple(
                (name, int(bool(value))) for name, value in capture_pi_values.items()
            )
            pdl.program.append(
                PulsePin(clock_port, clock_pulse_count, hold_pins=hold_pins)
            )

        max_chain_length = max((len(v) for v in expected_unload.values()), default=0)
        for cycle in range(max_chain_length):
            expected_value = _expected_channel_value(expected_unload, fanout, cycle)
            pdl.iTarget(compaction_channel_instrument)
            pdl.iRead(expected_value)
            pdl.iApply()
            if cycle < max_chain_length - 1:
                pdl.program.append(
                    PulsePin(clock_port, 1, hold_pins=((scan_enable_port, 1),))
                )

    return pdl.program


__all__ = [
    "FaultflowCompactionError",
    "retarget_compacted_faultflow_patterns",
]
