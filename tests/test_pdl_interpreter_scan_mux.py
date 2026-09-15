"""PDLInterpreter.iApply() driving a real ScanMuxNode graph (multi-arm ScanMux plan Phase 4).
Python-model-only (via tap_ir_play.play() + SibNetworkRegister, matching
test_pdl_interpreter_cross_sim.py's own ``run_on_python`` half and its own EXTEST-select-
preamble/one-combined-ops-list conventions exactly) -- no real RTL cross-sim yet, since
RTL-inserting a ScanMuxNode is sib_insert.py's own Phase 5 job, not done at this point in the
plan. This file instead confirms iApply's OWN ops-emission/orchestration is correct for a mux
target by driving the PUBLIC PDLInterpreter API (iTarget/iWrite/iRead/iApply), not by
hand-building a compose_bits sequence the way tests/test_sib_model_scan_mux.py's own tests do
-- the two files' properties cross-check each other from different layers.
"""

from __future__ import annotations

from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
)
from warptap.pdl_interpreter import PDLInterpreter
from warptap.pdl_verify import check_reads
from warptap.sib_model import SibNetworkRegister
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR, bits_to_int
from warptap.tap_ir_play import play
from warptap.tap_model import Instruction, TapModel

IR_WIDTH = 4
OPCODE_EXTEST = 0


def _select_extest_ops() -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(IR_WIDTH, tdi=bits_to_int([0] * IR_WIDTH)),  # OPCODE_EXTEST = 0
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _instrument(
    name: str, width: int, *, capture_value: int = 0,
    direction: InstrumentDirection = InstrumentDirection.READ,
) -> InstrumentNode:
    return InstrumentNode(name=name, width=width, capture_value=capture_value, direction=direction)


def _write_mux_graph_and_root() -> tuple[PhysicalGraph, ModuleInstance]:
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(values=(1,), instrument=_instrument("arm1", 3, direction=InstrumentDirection.WRITE)),
            ScanArm(values=(2,), instrument=_instrument("arm2", 2, direction=InstrumentDirection.WRITE)),
        ),
    )
    graph = PhysicalGraph(chain=(mux,))
    root = ModuleInstance("top", children=(ModuleInstance("arm1"), ModuleInstance("arm2")))
    return graph, root


def run_on_python(graph, ir_ops) -> tuple[list[int], SibNetworkRegister]:
    model = TapModel(has_idcode=True)
    reg = SibNetworkRegister(graph)
    model.register_data_register(Instruction.EXTEST, reg)
    model.reset()
    reg.reset()
    model.tick(0, 0)  # TEST_LOGIC_RESET -> RUN_TEST_IDLE settle
    return play(model, ir_ops), reg


def test_iapply_writes_and_reads_back_through_a_mux_arm():
    graph, root = _write_mux_graph_and_root()
    pdl = PDLInterpreter(graph, root)

    pdl.iTarget("arm1")
    pdl.iWrite(0b101)
    write_ops = pdl.iApply()

    pdl.iTarget("arm1")
    pdl.iRead(0b101)
    read_ops = pdl.iApply()

    ir_ops = _select_extest_ops() + write_ops + read_ops
    observed, _reg = run_on_python(graph, ir_ops)
    # Retargeting shift-length optimization plan: the second iApply re-targets the exact same
    # already-open arm1 -- stage_open_sequence's own currently_open reuse now emits ZERO phase-1
    # rounds for it (nothing new to open), not the previous 1, so this iApply is phase2 only.
    # 1 (IR-select) + 2 (first iApply: phase1 1 round + phase2) + 1 (second iApply: phase2
    # only) = 4, not the pre-fix 5.
    assert len(observed) == 1 + 2 + 1
    # An earlier version of this test only verified the SHIFT SHAPE, not the read VALUE -- its
    # own comment claimed "play() itself raises on a tdo/mask mismatch," which is false (tap_
    # ir_play.play()'s own docstring says explicitly it never compares against op.tdo/op.mask
    # -- that's pdl_verify.check_reads()'s job). Adding a real check_reads() call surfaced a
    # real, separate bug (mux-arm-readback bug fix plan): PDLInterpreter._target_layout's own
    # comparison-value construction assumed a matched ScanMuxNode arm's content sits wherever
    # a uniform position-order-then-reversed layout would place it -- wrong, confirmed against
    # real RTL (tests/test_scan_mux_write_arm_readback_cross_sim.py): the matched arm's own
    # content actually surfaces on the round's own *first* `width` chronological cycles,
    # MSB-first, since rtl/scan_mux_cell.v's own `so` is a combinational passthrough of the
    # matched arm's `so` for as long as it stays matched. Now fixed
    # (_mux_arm_read_chronological_bits) -- this assertion is the fast, RTL-free regression
    # test for it.
    results = check_reads(ir_ops, observed)
    assert len(results) == 1
    assert results[0].passed


def test_iapply_switches_arms_directly_across_separate_calls():
    """The genuinely new correctness question this plan's Phase 0 spike, Phase 2's sib_layout,
    and Phase 3's sib_model oracle all separately confirmed: switching a mux from one arm to a
    DIFFERENT one, via two independent iApply calls to two different instruments gated by the
    SAME mux, must commit correctly -- arm1's own value must survive being switched away from
    and back to, exactly mirroring test_sequential_iapply_to_different_targets_matches_on_
    real_rtl's own property for plain sibling SIBs, but now for two arms of ONE mux."""
    graph, root = _write_mux_graph_and_root()
    pdl = PDLInterpreter(graph, root)

    pdl.iTarget("arm1")
    pdl.iWrite(0b110)
    first_ops = pdl.iApply()  # bypass -> arm1, deliver 0b110

    pdl.iTarget("arm2")
    pdl.iWrite(0b01)
    second_ops = pdl.iApply()  # arm1 -> arm2 (its own round) then deliver 0b01 (a 2nd round)
    assert pdl._currently_open == {"mux_a": 2}

    pdl.iTarget("arm1")
    pdl.iRead(0b110)
    third_ops = pdl.iApply()  # arm2 -> arm1 (its own round) then read back (a 2nd round)
    assert pdl._currently_open == {"mux_a": 1}

    ir_ops = _select_extest_ops() + first_ops + second_ops + third_ops
    observed, _reg = run_on_python(graph, ir_ops)
    # Each iApply is phase1 (1 round -- a top-level mux target, switch or not, has no
    # ancestors) + phase2 = 2 ShiftDRs; +1 IR-select; 3 iApply calls total.
    assert len(observed) == 1 + 2 + 2 + 2
