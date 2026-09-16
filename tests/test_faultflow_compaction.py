"""Pure-Python tests for faultflow compaction-aware pattern retargeting (Stage 24).
No real faultflow/RTL involved here -- structural/hand-derived assertions, mirroring
test_faultflow_retarget.py's established style for a from-scratch translation module.
"""

from __future__ import annotations

import pytest

from warptap.faultflow_compaction import (
    FaultflowCompactionError,
    _expected_channel_value,
    retarget_compacted_faultflow_patterns,
)
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import PulsePin, ShiftDR

# 2 compactor outputs folding 3 internal chains: output0 = chain0^chain1, output1 = chain1^chain2.
_FANOUT = [[0, 1], [1, 2]]


def test_expected_channel_value_folds_via_fanout():
    # cycle 0: out0 = True^False=True, out1 = False^True=True -> 0b11 = 3
    expected_unload = {"0": [True], "1": [False], "2": [True]}
    assert _expected_channel_value(expected_unload, _FANOUT, cycle=0) == 0b11


def test_expected_channel_value_missing_chain_raises_named_error():
    expected_unload = {
        "0": [True],
        "1": [False],
    }  # chain 2 missing, but fanout[1] needs it
    with pytest.raises(FaultflowCompactionError, match="chain 2"):
        _expected_channel_value(expected_unload, _FANOUT, cycle=0)


def _graph_root():
    specs = [
        InstrumentSpec(
            "ctrl_write",
            width=3,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(
                SignalBinding("bist_start", 0),
                SignalBinding("bist_start", 1),
                SignalBinding("bist_start", 2),
            ),
        ),
        InstrumentSpec(
            "tdo_channel",
            width=2,
            capture_value=0,
            signal_bits=(SignalBinding("tdo", 0), SignalBinding("tdo", 1)),
        ),
    ]
    return build_sib_plan(specs, top_name="chip")


def test_retarget_compacted_missing_load_chain_mapping_raises_named_error():
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {"5": [True, False, True]},
            "expected_unload": {},
            "capture_pi_values": {},
        }
    ]
    with pytest.raises(FaultflowCompactionError, match="5"):
        retarget_compacted_faultflow_patterns(
            patterns,
            {},
            "tdo_channel",
            graph,
            root,
            fanout=_FANOUT,
            clock_port="clk",
            scan_enable_port="scan_en",
        )


def test_retarget_compacted_unload_op_sequence():
    """2 unload cycles -> 2 iApply reads on tdo_channel, exactly 1 shift-clock pulse (scan_en
    held HIGH) between them (N-1 pulses for N samples -- no pulse needed after the last
    sample). Plus the capture pulse (scan_en NOT forced, matching the untouched
    faultflow_retarget.py precedent) before the first sample."""
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {"0": [True, False, False]},
            "expected_unload": {
                "0": [True, False],
                "1": [False, True],
                "2": [True, True],
            },
            "capture_pi_values": {},
        }
    ]
    ops = retarget_compacted_faultflow_patterns(
        patterns,
        {0: "ctrl_write"},
        "tdo_channel",
        graph,
        root,
        fanout=_FANOUT,
        clock_port="clk",
        scan_enable_port="scan_en",
        clock_pulse_count=1,
    )
    pulse_ops = [op for op in ops if isinstance(op, PulsePin)]
    assert len(pulse_ops) == 2
    assert pulse_ops[0] == PulsePin("clk", 1, hold_pins=())  # capture
    assert pulse_ops[1] == PulsePin(
        "clk", 1, hold_pins=(("scan_en", 1),)
    )  # inter-cycle step

    capture_index = ops.index(pulse_ops[0])
    step_index = ops.index(pulse_ops[1])
    assert capture_index < step_index
    # The load write happened before the capture pulse.
    assert any(isinstance(op, ShiftDR) for op in ops[:capture_index])
    # At least one read ShiftDR before AND after the inter-cycle step.
    assert any(isinstance(op, ShiftDR) for op in ops[capture_index + 1 : step_index])
    assert any(isinstance(op, ShiftDR) for op in ops[step_index + 1 :])


def test_retarget_compacted_load_only_pattern_gets_no_pulses():
    graph, root = _graph_root()
    patterns = [
        {
            "load_seqs": {"0": [True, False, False]},
            "expected_unload": {},
            "capture_pi_values": {},
        }
    ]
    ops = retarget_compacted_faultflow_patterns(
        patterns,
        {0: "ctrl_write"},
        "tdo_channel",
        graph,
        root,
        fanout=_FANOUT,
        clock_port="clk",
        scan_enable_port="scan_en",
    )
    assert not any(isinstance(op, PulsePin) for op in ops)
