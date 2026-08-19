"""ICL connectivity check via "over-shifting" (implementation_plan.md §7 Stage 6). Technique
attributed to Dr. Martin Keim (Mentor Graphics/Siemens EDA), "Automated Debugging of IJTAG
Networks," Nordic Test Forum, Nov 2017 -- a vendor conference-talk deck (not the IEEE 1687
standard text or a peer-reviewed paper), the only source found using this exact technique by
name: flush a bit pattern longer than the currently-configured scan-path length through TDI,
then check what comes back at TDO against what the ICL-declared register widths predict.

**Exceeding the historical technique -- and why "margin matters" differently as a result**:
Keim's diagnosis is necessarily arrival-timing-only (black-box silicon): watch for a
recognizable sentinel pattern to reappear, on schedule, at TDO. warptap already owns a full
behavioral oracle (:class:`~warptap.tap_model.TapModel` +
:class:`~warptap.sib_model.SibNetworkRegister`), so this module does exact bit-for-bit stream
comparison instead of arrival-timing observation. This is strictly more sensitive -- it also
catches content-only defects (unchanged length, wrong bit content) that pure timing
observation would miss -- but it changes what `margin` actually guarantees, confirmed by
direct experiment (see ``tests/test_sib_overshift.py``'s own margin-sweep tests), not assumed
from Keim's own narrower technique: with `margin=0` there is no sentinel content fed at all
(the probe is just the self-restoring tail), yet MOST real structural defects still get caught
immediately anyway, because exact comparison only needs one mismatched bit anywhere in the
observed window -- a genuine additional strength over arrival-timing-only observation, not a
weakness. What `margin=0` does **not** provide is a *reliable guarantee*: whether a specific
defect is caught at `margin=0` depends on whether its content happens to coincidentally echo
back something that still matches the reference's own prediction for that cycle (a real,
demonstrated case: a too-narrow instrument's shorter live layout can wrap and echo back
exactly what a correctly-sized reference would have shown, slipping through at `margin=0` and
only becoming visible with `margin >= 1`). Larger margin generally increases detection
reliability but there is no simple formula (e.g. `margin >= |delta|`) that guarantees catching
every possible defect -- this mirrors Keim's own admitted limits, just via a different
mechanism than his. It still inherits the technique's one honestly-admitted blind spot (Keim's
slide 21): two same-length, same-content candidate paths are indistinguishable by exact
comparison either.

v1 implements the *primitive* (one probe + comparison), not Keim's hierarchical ring-by-ring
*search* algorithm -- that machinery exists to localize a fault among many nested candidate
SIBs in an unknown network; warptap's v1 network is flat (no nested SIB-gating-SIB trees,
Stage 4's own scoping decision), so there is exactly one candidate SIB per instrument, always
known in advance. Reuses `sib_layout`/`tap_ir`/`tap_ir_play` exactly as `pdl_interpreter.py`
does -- no new IR op type, no new lowering.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Union

from warptap.icl_model import PhysicalGraph
from warptap.sib_layout import compose_bits, layout_bit_length
from warptap.sib_model import SibNetworkRegister
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftDR, bits_from_int, bits_to_int
from warptap.tap_ir_play import play
from warptap.tap_model import Instruction, TapModel


def default_sentinel_pattern(bits: int) -> int:
    """Keim's own periodic 0011-repeating flush pattern (NTF17 slide 14), generalized to
    ``bits`` long, chronological/LSB-first (``tap_ir.py``'s own int convention: bit ``i`` is
    the value shifted on cycle ``i + 1``). Periodicity is deliberate, kept even though exact
    comparison is the real pass/fail oracle here: a stuck-at fault collapses a periodic
    pattern into a constant run, a cheap, human-legible signature distinct from "diverges
    somewhere\"."""
    fed_bits = [(i >> 1) & 1 for i in range(bits)]  # 0,0,1,1,0,0,1,1,...
    return bits_to_int(fed_bits)


def build_overshift_ops(
    graph: PhysicalGraph,
    target_open: frozenset[str],
    *,
    currently_open: frozenset[str] = frozenset(),
    margin: int,
    sentinel: Optional[int] = None,
) -> list[Union[ShiftDR, GotoState]]:
    """Phase 1 (retarget ``currently_open`` -> ``target_open``): identical construction to
    ``PDLInterpreter.iApply``'s own phase 1 -- duplicated, not imported (this check has none
    of ``iApply``'s pending-write/read state to entangle with; two occurrences is the same
    "rule of three" case ``sib_layout.py``'s own docstring already cites for not sharing
    prematurely).

    Phase 2 (the probe): ``margin`` free-choice sentinel bits, fed first (chronologically),
    followed by a self-restoring ``N_model``-bit tail built via ``compose_bits(graph,
    target_open, target_open, target_sib=None, payload_value=0)`` -- since the tail is fed
    *last*, it's what's physically resident when this shift's Update-DR commits ("last bit
    fed lands nearest TDI," ``tap_ir.py``'s own established convention), so the probe leaves
    the network in the exact ``target_open`` state it found it in.

    Like ``PDLInterpreter.iApply``, this never selects a TAP instruction -- the caller is
    responsible for having already selected an instruction whose data register is the SIB
    network (EXTEST, matching every other stage's convention) before driving these ops,
    exactly as ``tests/test_pdl_interpreter_cross_sim.py``'s own ``_select_extest_ops()``
    preamble already does.

    ``margin=0`` is accepted (it's mechanically just a probe with no sentinel bits at all,
    i.e. the self-restoring tail alone) but provides no *reliable* detection guarantee (see
    module docstring's empirically-grounded explanation) -- kept legal rather than rejected
    specifically so a caller/test can construct one to demonstrate that unreliability
    directly, rather than only being able to assert it in prose. Raises :class:`ValueError`
    for a negative ``margin``, which is never meaningful.
    """
    if margin < 0:
        raise ValueError(f"margin must be >= 0, got {margin}")
    if sentinel is None:
        sentinel = default_sentinel_pattern(margin)

    ops: list[Union[ShiftDR, GotoState]] = []

    len1 = layout_bit_length(graph, currently_open)
    bits1 = compose_bits(graph, currently_open, target_open, target_sib=None, payload_value=0)
    ops.append(GotoState(TapState.SHIFT_DR))
    ops.append(ShiftDR(len1, tdi=bits_to_int(list(reversed(bits1)))))
    ops.append(GotoState(TapState.RUN_TEST_IDLE))

    tail_bits = compose_bits(graph, target_open, target_open, target_sib=None, payload_value=0)
    n_model = len(tail_bits)
    sentinel_bits = bits_from_int(sentinel, margin)
    fed_bits = sentinel_bits + list(reversed(tail_bits))
    ops.append(GotoState(TapState.SHIFT_DR))
    ops.append(ShiftDR(margin + n_model, tdi=bits_to_int(fed_bits)))
    ops.append(GotoState(TapState.RUN_TEST_IDLE))

    return ops


def expected_probe_tdo(graph: PhysicalGraph, ops: list) -> int:
    """Reference oracle: drive ``ops`` (which must already include any TAP-instruction-
    selection preamble the caller needs, and must end with the over-shift probe as its own
    last shift op -- exactly what ``build_overshift_ops`` alone, or a preamble followed by
    it, produces) against a FRESH ``TapModel`` + ``SibNetworkRegister(graph)`` pair -- fresh
    every call, never the subject's own register, so a corrupted live subject can't
    accidentally "predict" its own corruption. Returns the last entry of ``play()``'s
    returned list -- the probe's own observed value, regardless of what preceded it."""
    model = TapModel(has_idcode=True)
    reg = SibNetworkRegister(graph)
    model.register_data_register(Instruction.EXTEST, reg)
    model.reset()
    reg.reset()
    model.tick(0, 0)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE settle
    observed = play(model, ops)
    return observed[-1]


class OverShiftResult(NamedTuple):
    passed: bool
    probe_bits: int
    layout_bits: int
    expected_tdo: int
    observed_tdo: int
    diagnosis: str


def diagnose_overshift(
    graph: PhysicalGraph,
    target_open: frozenset[str],
    probe_bits: int,
    layout_bits: int,
    expected_tdo: int,
    observed_tdo: int,
) -> OverShiftResult:
    """Pure int/bit comparison, no model access. ``passed`` (exact equality) is the only
    real pass/fail oracle here -- stronger than Keim's timing-only comparison. ``diagnosis``
    is best-effort human-legible UX only (constant-output => stuck-at signature;
    first-divergence position within the observed TDO stream) -- never required to localize
    the fault to a specific slot, matching the source's own admitted "same-length paths
    indistinguishable" limit. Deliberately does not attempt to map a TDO-stream divergence
    position back to a chain position/slot name: TDO's chronological order and the fed
    TDI's chronological order are not the same sequence (TDO reflects whatever was already
    resident before each shift, not what's being fed in that same cycle), so any such
    mapping needs real per-cycle simulation to derive correctly, not arithmetic on the two
    aggregate ints alone -- exactly what ``graph``/``target_open`` are accepted for (a
    future, more careful localization pass could use them), but this v1 diagnosis does not
    attempt it."""
    del graph, target_open  # accepted for a future, more careful localization pass; unused today
    passed = expected_tdo == observed_tdo
    if passed:
        diagnosis = "match"
    else:
        observed_bits = bits_from_int(observed_tdo, probe_bits)
        if len(set(observed_bits)) <= 1:
            diagnosis = "constant output at TDO -- stuck-at signature within the probed path"
        else:
            expected_bits = bits_from_int(expected_tdo, probe_bits)
            agree = 0
            for e, o in zip(expected_bits, observed_bits):
                if e != o:
                    break
                agree += 1
            diagnosis = (
                f"observed TDO diverges from expected after {agree} matching bit(s) of "
                f"{probe_bits} (declared length {layout_bits}) -- consistent with a length "
                "or content mismatch; cannot localize further from this probe alone"
            )
    return OverShiftResult(
        passed=passed,
        probe_bits=probe_bits,
        layout_bits=layout_bits,
        expected_tdo=expected_tdo,
        observed_tdo=observed_tdo,
        diagnosis=diagnosis,
    )
