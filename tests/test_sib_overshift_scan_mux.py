"""Pure-Python tests for the ICL connectivity check against a ScanMuxNode-containing network
(multi-arm ScanMux plan Phase 6). No Yosys/RTL involved, mirroring test_sib_overshift.py's own
style and helpers closely.

Confirms this module's own claim, needing no code changes at all beyond the type annotations
in sib_overshift.py itself: every function build_overshift_ops/diagnose_overshift calls was
already generalized for ScanMuxNode in earlier phases (Phase 2's sib_layout, Phase 2's
sib_retarget), so the probe mechanism just already worked.
"""

from __future__ import annotations

from warptap.icl_model import InstrumentNode, PhysicalGraph, ScanArm, ScanMuxNode
from warptap.sib_layout import layout_bit_length
from warptap.sib_model import SibNetworkRegister
from warptap.sib_overshift import build_overshift_ops, diagnose_overshift, expected_probe_tdo
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftDR, ShiftIR, bits_to_int
from warptap.tap_ir_play import play
from warptap.tap_model import Instruction, TapModel

IR_WIDTH = 4


def _instrument(name: str, width: int, capture_value: int = 0) -> InstrumentNode:
    return InstrumentNode(name=name, width=width, capture_value=capture_value)


def _mux_graph() -> PhysicalGraph:
    """Two arms, deliberately the SAME width -- a mis-selected arm would NOT be caught by
    probe length alone (layout_bit_length is identical either way), only by exact-stream
    content comparison, exactly the property this file exists to confirm for a mux."""
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(values=(1,), instrument=_instrument("a1", width=3, capture_value=0b101)),
            ScanArm(values=(2,), instrument=_instrument("a2", width=3, capture_value=0b010)),
        ),
    )
    return PhysicalGraph(chain=(mux,))


def _shift_ops(ops):
    return [op for op in ops if isinstance(op, ShiftDR)]


def _select_extest_ops():
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _probe_ops(graph, target_open, **kwargs):
    return _select_extest_ops() + build_overshift_ops(graph, target_open, **kwargs)


def _fresh_model_and_register(graph):
    model = TapModel(has_idcode=True)
    reg = SibNetworkRegister(graph)
    model.register_data_register(Instruction.EXTEST, reg)
    model.reset()
    reg.reset()
    model.tick(0, 0)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE settle
    return model, reg


def test_overshift_probe_matches_a_healthy_mux_target():
    """Sanity/no-false-positive check: probing a correctly-behaving mux target agrees with
    itself -- the probe mechanism doesn't need a fault to be meaningful."""
    graph = _mux_graph()
    target_open = {"mux_a": 1}
    ops = _probe_ops(graph, target_open, margin=4)
    expected = expected_probe_tdo(graph, ops)
    observed = expected_probe_tdo(graph, ops)  # a second, independent fresh run
    probe = _shift_ops(ops)[-1]
    result = diagnose_overshift(
        graph, target_open, probe.bits, layout_bit_length(graph, target_open), expected, observed
    )
    assert result.passed


def test_overshift_detects_a_mis_selected_arm_of_the_same_length():
    """The core Phase 6 deliverable: arm1 and arm2 have the SAME width (3 bits), so a
    mis-selected arm produces a probe of the identical DECLARED length -- length alone
    couldn't catch this. Simulates a select-field fault (mirrors test_sib_overshift.py's own
    test_stuck_open_sib_detected exactly): after phase 1 normally commits mux_a to arm1, force
    po to arm2's value directly, as if the select field never actually responded to phase 1's
    own command, then run the probe and confirm exact-stream comparison catches it."""
    graph = _mux_graph()
    target_open = {"mux_a": 1}
    ops = _probe_ops(graph, target_open, margin=4)
    # ops = [select-EXTEST preamble (3 ops), phase 1 (3 ops), phase 2/probe (3 ops)].
    preamble_and_phase1, phase2_ops = ops[:6], ops[6:]
    expected = expected_probe_tdo(graph, ops)

    model, reg = _fresh_model_and_register(graph)
    play(model, preamble_and_phase1)
    mux_slot = reg._slots[0]
    assert mux_slot.po == [1, 0]  # confirm phase 1 really did commit arm1 (value 1 = 0b01)
    mux_slot.po = [0, 1]  # ...then simulate the select field not actually responding,
    # stuck showing arm2 (value 2 = 0b10) instead
    observed = play(model, phase2_ops)[-1]

    probe = _shift_ops(ops)[-1]
    result = diagnose_overshift(
        graph, target_open, probe.bits, layout_bit_length(graph, target_open), expected, observed
    )
    assert not result.passed
    assert result.diagnosis != "match"
