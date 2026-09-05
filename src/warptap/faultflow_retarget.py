"""faultflow pattern retargeting (implementation_plan.md §7 Stage 14, built on Stage 13's
``PulsePin``/STIL emission). Consumes faultflow's real ``--export-patterns`` JSON (a flat array
of ``{"load_seqs": {chain_id: [bool,...]}, "capture_pi_values": {port: bool},
"expected_unload": {chain_id: [bool,...]}}`` records, ``faultflow/scan/pattern_export.py``'s
own ``scan_pattern_to_dict``) and remaps each pattern through warptap's own SIB/TAP network
model, driving a real :class:`~warptap.pdl_interpreter.PDLInterpreter` -- never faultflow's own
SoC chain-offset table, and never faultflow's own retargeting algorithm (this project's
established "port understanding, not code" boundary with that project).

**A faultflow chain id carries no inherent name** (a bare int assigned during block-level scan
stitching) -- ``chain_to_instrument`` is a REQUIRED caller input, since no shared naming
convention exists between the two tools. The separate, structurally different
``faultflow_soc_access_v1`` manifest (a flat, always-present SoC chain addressed by offset, not
a TAP-instruction-selected network) is not consumed here.

**Bit order -- independently re-derived and verified twice this session** (once by hand with a
non-palindromic worked example, once by an independent review given the same primitives): both
tools define physical "position 0" as nearest-SI identically, but faultflow's own exported
sequence is index-reversed relative to position (``faultflow/retarget/transform.py``'s own
``s[k] == V[L-1-k]``), while warptap's own ``payload_value`` bit ``k`` IS position ``k``
directly (:mod:`warptap.sib_layout`'s ``compose_bits``). This means
``payload_bit[k] == sequence[width-1-k]`` -- the exported bit list must be REVERSED before
packing into an int via :func:`warptap.tap_ir.bits_to_int`. Not a new convention: ``tap_ir.py``'s
own module docstring already states the identical rule for composing ``sib_layout``'s
position-ordered bit lists into a chronological int.

**Padding, confirmed directly against the real source** (``faultflow/scan/protocol.py``'s
``serialize_vector``): when a pattern's chains differ in length, ``load_seqs`` is padded at the
FRONT and ``expected_unload`` at the BACK to a shared ``max_chain_length`` -- neither the
padding amount nor ``max_chain_length`` itself is present in the exported JSON, so padding is
stripped here using each chain's REAL width (from ``chain_to_instrument`` -> ``graph`` ->
``InstrumentNode.width``): the last ``width`` elements of ``load_seqs``, the first ``width``
elements of ``expected_unload``.

**One ``PulsePin`` per pattern, not per chain** -- a real correctness requirement, not a
simplification: faultflow's ``ScanPattern`` represents ONE fault-detection attempt where every
referenced chain is loaded, a SINGLE functional capture edge is applied (with
``capture_pi_values`` held throughout), and every chain is then unloaded/compared against that
SAME capture. Interleaving load/pulse/unload per chain would compare a chain's response before
other chains in the same pattern were even loaded, or before any capture happened at all --
this module loads every chain first, pulses once, then unloads every chain.

``clock_port``/``clock_pulse_count`` are REQUIRED caller inputs: faultflow's own schema carries
no clock/timing field at all (confirmed directly against ``transform.py``/``emit.py`` -- zero
references to any clock concept in the real SoC-pattern-emission code). Making the caller
supply a real period for that pin is standard STIL authoring, not a burden unique to this
module -- see :mod:`warptap.tap_ir_stil`'s own ``pulse_periods`` requirement.
"""

from __future__ import annotations

from typing import Any, Dict, List, Union

from warptap.errors import WarptapError
from warptap.icl_model import ModuleInstance, PhysicalGraph
from warptap.pdl_interpreter import PDLInterpreter
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest, PulsePin]


class FaultflowRetargetError(WarptapError):
    """Raised when a pattern references a faultflow chain id absent from
    ``chain_to_instrument``, or that mapping names an instrument not present in ``graph``."""


def _instrument_width(graph: PhysicalGraph, instrument_name: str) -> int:
    for node in graph.chain:
        if node.instrument is not None and node.instrument.name == instrument_name:
            return node.instrument.width
    raise FaultflowRetargetError(f"no instrument named {instrument_name!r} in this network's graph")


def _strip_load_padding(seq: List[bool], width: int) -> List[bool]:
    """``load_seqs`` is front-padded to a shared ``max_chain_length`` -- the real content is
    always the LAST ``width`` elements (see module docstring)."""
    if len(seq) < width:
        raise FaultflowRetargetError(
            f"load_seqs entry has {len(seq)} bits, shorter than the target instrument's own "
            f"width {width} -- cannot be this chain's real content plus padding"
        )
    return seq[len(seq) - width :]


def _strip_unload_padding(seq: List[bool], width: int) -> List[bool]:
    """``expected_unload`` is back-padded -- the real content is the FIRST ``width``
    elements (see module docstring)."""
    if len(seq) < width:
        raise FaultflowRetargetError(
            f"expected_unload entry has {len(seq)} bits, shorter than the target instrument's "
            f"own width {width} -- cannot be this chain's real content plus padding"
        )
    return seq[:width]


def _sequence_to_payload(seq: List[bool]) -> int:
    """faultflow's own ``sequence[k] == V[width-1-k]`` (position); warptap's own
    ``payload_value`` bit ``k`` IS position ``k`` directly -- so
    ``payload_bit[k] == sequence[width-1-k]``, i.e. the REVERSED sequence packs directly into
    an int via LSB-first ``bits_to_int`` semantics (see module docstring for the full
    derivation)."""
    bits = [1 if b else 0 for b in reversed(seq)]
    return sum(bit << i for i, bit in enumerate(bits))


def retarget_faultflow_patterns(
    patterns: List[Dict[str, Any]],
    chain_to_instrument: Dict[int, str],
    graph: PhysicalGraph,
    root: ModuleInstance,
    *,
    clock_port: str,
    clock_pulse_count: int,
) -> List[_IrOp]:
    """Retarget every pattern in ``patterns`` (the parsed ``--export-patterns`` JSON array)
    through a real :class:`~warptap.pdl_interpreter.PDLInterpreter` built against
    ``graph``/``root``, returning the accumulated :mod:`warptap.tap_ir` ops -- feed straight
    into :func:`warptap.tap_ir_stil.to_stil` (the only backend that can render the
    :class:`~warptap.tap_ir.PulsePin` ops this function inserts).

    Raises :class:`FaultflowRetargetError` naming the offending chain id if
    ``chain_to_instrument`` doesn't cover every chain a pattern references, or names an
    instrument absent from ``graph``."""
    pdl = PDLInterpreter(graph, root)
    for pattern in patterns:
        load_seqs: Dict[str, list] = pattern.get("load_seqs", {})
        expected_unload: Dict[str, list] = pattern.get("expected_unload", {})
        capture_pi_values: Dict[str, bool] = pattern.get("capture_pi_values", {})

        chain_ids = sorted({int(k) for k in load_seqs} | {int(k) for k in expected_unload})
        widths: Dict[int, int] = {}
        for chain_id in chain_ids:
            if chain_id not in chain_to_instrument:
                raise FaultflowRetargetError(
                    f"faultflow chain {chain_id} has no entry in chain_to_instrument -- "
                    f"every chain a pattern references must be mapped (known: "
                    f"{sorted(chain_to_instrument)})"
                )
            widths[chain_id] = _instrument_width(graph, chain_to_instrument[chain_id])

        # Phase 1: load every referenced chain's stimulus.
        for chain_id in chain_ids:
            key = str(chain_id)
            if key not in load_seqs:
                continue
            seq = _strip_load_padding([bool(b) for b in load_seqs[key]], widths[chain_id])
            pdl.iTarget(chain_to_instrument[chain_id])
            pdl.iWrite(_sequence_to_payload(seq))
            pdl.iApply()

        # Phase 2: one real functional-clock capture for this whole pattern, holding every
        # requested PI value throughout -- see module docstring for why this is per-pattern,
        # not per-chain.
        if expected_unload:
            hold_pins = tuple((name, int(bool(value))) for name, value in capture_pi_values.items())
            pdl.program.append(PulsePin(clock_port, clock_pulse_count, hold_pins=hold_pins))

        # Phase 3: unload/compare every referenced chain's response.
        for chain_id in chain_ids:
            key = str(chain_id)
            if key not in expected_unload:
                continue
            seq = _strip_unload_padding([bool(b) for b in expected_unload[key]], widths[chain_id])
            pdl.iTarget(chain_to_instrument[chain_id])
            pdl.iRead(_sequence_to_payload(seq))
            pdl.iApply()

    return pdl.program
