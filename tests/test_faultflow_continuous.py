"""Pure-Python tests for the continuous-flow decompressor's pattern retargeting (W1, built
on faultflow_compression.py's one-shot port).

The decompressor: a ring generator cleared on the scan enable's rise, fed ``num_channels``
channel bits into its injector stages on EVERY shift, a phase shifter reading the ring. A load
is ``warmup + max_chain_length`` shifts, so a pattern has ``channels x shifts`` free bits.
Every expectation below is derived from a numeric simulation written in this file (a few lines
of ring arithmetic), not from the module under test.
"""

from __future__ import annotations

import random

import pytest

from warptap.faultflow_compression import (
    PRIMITIVE_POLYNOMIALS,
    FaultflowCompressionError,
    LfsrPolynomial,
)
from warptap.faultflow_continuous import (
    ContinuousDecompressor,
    continuous_from_manifest,
    continuous_scan_in_rows,
    injector_stages,
    retarget_continuous_faultflow_patterns,
    retarget_decompressed_faultflow_patterns,
    solve_pattern_channels,
)
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import PulsePin, ShiftDR

POLY8 = PRIMITIVE_POLYNOMIALS[8]
TAPS = [[0, 3, 5], [1, 2, 7], [4], [0, 6]]


def _parity(value: int) -> int:
    return bin(value).count("1") & 1


def numeric_scan_ins(
    poly: LfsrPolynomial,
    taps: list[list[int]],
    injectors: list[int],
    words: list[list[int]],
) -> list[list[int]]:
    """The ring simulated bit by bit: cleared at shift 0; each shift the scan-ins read the
    phase shifter, then the ring steps (Galois, feedback state[width-1]) and each channel bit
    is XORed into its injector stage."""
    width = poly.width
    state = [0] * width
    out: list[list[int]] = []
    for word in words:
        out.append([sum(state[k] for k in tap) & 1 for tap in taps])
        fb = state[width - 1]
        state = [fb] + [
            state[i - 1] ^ (fb if i in poly.taps else 0) for i in range(1, width)
        ]
        for channel, stage in enumerate(injectors):
            state[stage] ^= word[channel]
    return out


def _decompressor(channels: int = 3, warmup: int = 2) -> ContinuousDecompressor:
    return ContinuousDecompressor(
        poly=POLY8,
        phase_shifter_taps=TAPS,
        num_channels=channels,
        warmup=warmup,
    )


def test_injector_stages_spread_the_channels_evenly():
    assert injector_stages(16, 8) == [0, 2, 4, 6, 8, 10, 12, 14]
    assert injector_stages(8, 3) == [0, 2, 5]
    assert injector_stages(8, 1) == [0]
    assert injector_stages(8, 8) == list(range(8))


@pytest.mark.parametrize("channels", [0, 9])
def test_injector_stages_refuse_a_channel_count_the_ring_cannot_take(channels):
    with pytest.raises(FaultflowCompressionError, match="channels"):
        injector_stages(8, channels)


def test_the_cleared_ring_gives_scan_ins_of_zero_at_shift_zero():
    rows = continuous_scan_in_rows(POLY8, TAPS, 3, 4)
    assert rows[0] == [0, 0, 0, 0]


def test_rows_match_the_numeric_ring_for_random_channel_words():
    rng = random.Random(1)
    channels, cycles = 3, 9
    rows = continuous_scan_in_rows(POLY8, TAPS, channels, cycles)
    for _ in range(5):
        words = [[rng.randint(0, 1) for _ in range(channels)] for _ in range(cycles)]
        flat = sum(
            word[c] << (t * channels + c)
            for t, word in enumerate(words)
            for c in range(channels)
        )
        expected = numeric_scan_ins(
            POLY8, TAPS, injector_stages(8, channels), words
        )
        got = [[_parity(mask & flat) for mask in row] for row in rows]
        assert got == expected


def test_one_channel_word_reaches_the_scan_ins_after_the_shift_that_applies_it():
    """Channel bit 0 at shift 2 only: scan-ins of shifts 0..2 are still 0, and some
    scan-in is 1 later (the injection reached the phase shifter's taps)."""
    words = [[0], [0], [1]] + [[0]] * 6
    out = numeric_scan_ins(POLY8, TAPS, injector_stages(8, 1), words)
    assert out[:3] == [[0, 0, 0, 0]] * 3
    assert any(any(row) for row in out[3:])


def _load_from_words(dec, words, max_chain_length, chains):
    """load_seqs of a load driven with `words` (one per shift): load cycle t is shift
    warmup + t, as a faultflow pattern indexes it."""
    scan_ins = numeric_scan_ins(
        dec.poly, dec.phase_shifter_taps, dec.injectors, words
    )
    return {
        str(chain): [
            bool(scan_ins[dec.warmup + t][chain]) for t in range(max_chain_length)
        ]
        for chain in chains
    }


def test_solving_a_pattern_returns_words_that_reload_it():
    dec = _decompressor(channels=3, warmup=2)
    rng = random.Random(7)
    max_chain_length = 4
    shifts = dec.warmup + max_chain_length
    words = [[rng.randint(0, 1) for _ in range(3)] for _ in range(shifts)]
    load_seqs = _load_from_words(dec, words, max_chain_length, chains=[0, 1])
    seqs = solve_pattern_channels(load_seqs, dec, max_chain_length, pattern_index=0)
    assert sorted(seqs) == [0, 1, 2]
    assert all(len(seq) == shifts for seq in seqs.values())
    solved = [[int(seqs[k][t]) for k in range(3)] for t in range(shifts)]
    assert _load_from_words(dec, solved, max_chain_length, chains=[0, 1]) == load_seqs


def test_an_unsatisfiable_load_names_the_pattern():
    """4 chains x 6 cycles of constraints over 1 channel x 7 shifts of freedom: a random
    load has contradictions, and the error names the pattern."""
    dec = _decompressor(channels=1, warmup=1)
    rng = random.Random(3)
    max_chain_length = 6
    load_seqs = {
        str(c): [bool(rng.randint(0, 1)) for _ in range(max_chain_length)]
        for c in range(4)
    }
    with pytest.raises(FaultflowCompressionError, match="pattern 5"):
        solve_pattern_channels(load_seqs, dec, max_chain_length, pattern_index=5)


def test_load_care_drops_the_positions_that_are_not_care():
    dec = _decompressor(channels=1, warmup=1)
    rng = random.Random(3)
    max_chain_length = 6
    load_seqs = {
        str(c): [bool(rng.randint(0, 1)) for _ in range(max_chain_length)]
        for c in range(4)
    }
    seqs = solve_pattern_channels(
        load_seqs, dec, max_chain_length, 0, load_care={(0, 5)}
    )
    solved = [[int(seqs[0][t])] for t in range(dec.warmup + max_chain_length)]
    out = numeric_scan_ins(dec.poly, dec.phase_shifter_taps, dec.injectors, solved)
    assert bool(out[dec.warmup + 5][0]) == load_seqs["0"][5]


def test_a_load_of_the_wrong_length_is_refused():
    dec = _decompressor()
    with pytest.raises(FaultflowCompressionError, match="max_chain_length=4"):
        solve_pattern_channels({"0": [True, False]}, dec, 4, pattern_index=0)


def test_the_launch_on_shift_head_bit_is_a_constraint_too():
    dec = _decompressor(channels=3, warmup=2)
    max_chain_length = 3
    shifts = dec.warmup + max_chain_length
    rng = random.Random(11)
    words = [[rng.randint(0, 1) for _ in range(3)] for _ in range(shifts + 1)]
    load_seqs = _load_from_words(dec, words, max_chain_length, chains=[0])
    head = numeric_scan_ins(dec.poly, dec.phase_shifter_taps, dec.injectors, words)[
        shifts
    ][1]
    seqs = solve_pattern_channels(
        load_seqs,
        dec,
        max_chain_length,
        0,
        launch_scan_in={1: bool(head)},
    )
    solved = [[int(seqs[k][t]) for k in range(3)] for t in range(shifts)] + [[0, 0, 0]]
    out = numeric_scan_ins(dec.poly, dec.phase_shifter_taps, dec.injectors, solved)
    assert out[shifts][1] == head


def _manifest(**over):
    entry = {
        "enabled": True,
        "architecture": "continuous",
        "num_channels": 3,
        "channel_port": "tdi",
        "polynomial": {"width": 8, "taps": sorted(POLY8.taps)},
        "phase_shifter_taps": TAPS,
        "warmup": 2,
        "injectors": [0, 2, 5],
    }
    entry.update(over)
    return entry


def test_the_decompressor_is_read_from_the_manifest():
    dec = continuous_from_manifest(_manifest())
    assert (dec.poly, dec.num_channels, dec.warmup) == (POLY8, 3, 2)
    assert dec.phase_shifter_taps == TAPS and dec.injectors == [0, 2, 5]


def test_injectors_the_manifest_states_must_be_the_ones_this_model_computes():
    with pytest.raises(FaultflowCompressionError, match="injectors"):
        continuous_from_manifest(_manifest(injectors=[0, 1, 2]))


@pytest.mark.parametrize("missing", ["polynomial", "phase_shifter_taps", "warmup"])
def test_a_manifest_missing_what_the_ring_needs_is_refused(missing):
    entry = _manifest()
    del entry[missing]
    with pytest.raises(FaultflowCompressionError, match=missing):
        continuous_from_manifest(entry)


def test_a_phase_shifter_tap_outside_the_ring_is_refused():
    with pytest.raises(FaultflowCompressionError, match="tap"):
        continuous_from_manifest(_manifest(phase_shifter_taps=[[0, 8]]))


def _graph_root(channels: int = 3):
    specs = [
        InstrumentSpec(
            "tdi_channel",
            width=channels,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=tuple(SignalBinding("tdi", k) for k in range(channels)),
        ),
        InstrumentSpec(
            "scan_out_0",
            width=2,
            capture_value=0,
            signal_bits=(
                SignalBinding("scan_out_0", 0),
                SignalBinding("scan_out_0", 1),
            ),
        ),
    ]
    return build_sib_plan(specs, top_name="chip")


def _pattern(dec, max_chain_length, seed=2):
    rng = random.Random(seed)
    shifts = dec.warmup + max_chain_length
    words = [[rng.randint(0, 1) for _ in range(dec.num_channels)] for _ in range(shifts)]
    return {
        "load_seqs": _load_from_words(dec, words, max_chain_length, chains=[0, 1]),
        "expected_unload": {"3": [True, False]},
        "capture_pi_values": {},
    }


def test_the_retarget_writes_the_channels_before_every_shift_pulse():
    """One scan-enable-low pulse (the next edge's rise clears the ring), then for each of
    the warmup + max_chain_length shifts a channel write and one pulse with scan enable
    high; then the capture pulse and the unload read."""
    dec = _decompressor(channels=3, warmup=2)
    graph, root = _graph_root(3)
    max_chain_length = 3
    ops = retarget_continuous_faultflow_patterns(
        [_pattern(dec, max_chain_length)],
        {3: "scan_out_0"},
        "tdi_channel",
        graph,
        root,
        decompressor=dec,
        max_chain_length=max_chain_length,
        clock_port="clk",
        scan_enable_port="scan_en",
    )
    shifts = dec.warmup + max_chain_length
    pulses = [(i, op) for i, op in enumerate(ops) if isinstance(op, PulsePin)]
    assert [op for _, op in pulses] == (
        [PulsePin("clk", 1, hold_pins=(("scan_en", 0),))]
        + [PulsePin("clk", 1, hold_pins=(("scan_en", 1),))] * shifts
        + [PulsePin("clk", 1, hold_pins=(("scan_en", 0),))]
    )
    positions = [i for i, _ in pulses]
    # A channel write (a whole-instrument scan) lies before each shift pulse.
    for before, at in zip(positions[:-2], positions[1:-1]):
        assert any(isinstance(op, ShiftDR) for op in ops[before + 1 : at])
    # The unload read comes after the capture pulse.
    assert any(isinstance(op, ShiftDR) for op in ops[positions[-1] + 1 :])


def test_the_retarget_refuses_an_unmapped_unload_chain():
    dec = _decompressor()
    graph, root = _graph_root(3)
    with pytest.raises(FaultflowCompressionError, match="7"):
        retarget_continuous_faultflow_patterns(
            [
                {
                    "load_seqs": {},
                    "expected_unload": {"7": [True]},
                    "capture_pi_values": {},
                }
            ],
            {},
            "tdi_channel",
            graph,
            root,
            decompressor=dec,
            max_chain_length=3,
            clock_port="clk",
            scan_enable_port="scan_en",
        )


def test_a_load_only_pattern_gets_no_capture_pulse():
    dec = _decompressor()
    graph, root = _graph_root(3)
    ops = retarget_continuous_faultflow_patterns(
        [{**_pattern(dec, 3), "expected_unload": {}}],
        {},
        "tdi_channel",
        graph,
        root,
        decompressor=dec,
        max_chain_length=3,
        clock_port="clk",
        scan_enable_port="scan_en",
    )
    assert len([op for op in ops if isinstance(op, PulsePin)]) == 1 + dec.warmup + 3


def test_the_architecture_in_the_manifest_picks_the_retarget():
    dec = _decompressor()
    graph, root = _graph_root(3)
    ops = retarget_decompressed_faultflow_patterns(
        [_pattern(dec, 3)],
        {3: "scan_out_0"},
        "tdi_channel",
        graph,
        root,
        compression=_manifest(),
        max_chain_length=3,
        clock_port="clk",
        scan_enable_port="scan_en",
    )
    assert len([op for op in ops if isinstance(op, PulsePin)]) == 1 + 5 + 1


def test_a_manifest_without_an_architecture_is_the_one_shot_decompressor():
    graph, root = _graph_root(2)
    one_shot = {
        "enabled": True,
        "num_channels": 8,
        "polynomial": {"width": 2, "taps": [1]},
        "phase_shifter_taps": [[0], [1]],
    }
    ops = retarget_decompressed_faultflow_patterns(
        [
            {
                "load_seqs": {"0": [True, False, True], "1": [False, True, True]},
                "expected_unload": {},
                "capture_pi_values": {},
            }
        ],
        {},
        "tdi_channel",
        graph,
        root,
        compression=one_shot,
        max_chain_length=3,
        clock_port="clk",
        scan_enable_port="scan_en",
    )
    # The one-shot flow: one idle-settle pulse, then max_chain_length shift edges at once.
    assert [op for op in ops if isinstance(op, PulsePin)] == [
        PulsePin("clk", 1, hold_pins=(("scan_en", 0),)),
        PulsePin("clk", 3, hold_pins=(("scan_en", 1),)),
    ]
