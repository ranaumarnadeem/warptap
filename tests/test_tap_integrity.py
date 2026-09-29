"""Pure-Python tests for warptap.tap_integrity: the program's structure, the TDO it expects
of the TAP's own registers, live-instrument masking, JSON round-tripping and check_integrity.
Real RTL runs the same programs in test_tap_integrity_cross_sim.py."""

from __future__ import annotations

import json

import pytest

from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SignalBinding,
)
from warptap.sib_overshift import build_overshift_ops, default_sentinel_pattern, expected_probe_tdo
from warptap.sib_plan import HierarchySpec, InstrumentSpec, build_sib_plan
from warptap.tap_integrity import (
    TMS_RESET_LEAD_IN,
    TRST_LEAD_IN,
    IntegrityProgram,
    TapConfig,
    TapIntegrityError,
    build_integrity_program,
    check_integrity,
    select_instruction,
)
from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import bits_from_int
from warptap.tap_model import CAPTURE_IR_PATTERN, IDCODE_VALUE, OPCODE_EXTEST

MARGIN = 4

_FLAT = [
    InstrumentSpec(
        "ctrl", width=2, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("ctrl_in", 0), SignalBinding("ctrl_in", 1)),
    ),
    InstrumentSpec(
        "status", width=1, capture_value=0,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("status_out", 0),),
    ),
    InstrumentSpec("stub", width=3, capture_value=0b101),
]


def _program(specs=_FLAT, **kwargs) -> IntegrityProgram:
    graph, root = build_sib_plan(specs)
    kwargs.setdefault("margin", MARGIN)
    return build_integrity_program(graph, root, **kwargs)


def _shifted(program: IntegrityProgram, test: str) -> list[int]:
    return [c.tdo for c in program.cycles if c.test == test and c.shift]


def _sentinel(bits: int) -> list[int]:
    return bits_from_int(default_sentinel_pattern(bits), bits)


_TAP_PATHS = [
    "dr_paths", "ir_paths", "ir_capture_update", "select_ir_reset",
    "idcode_hold", "bypass_hold", "ir_hold", "idcode_capture", "bypass_capture",
]


def test_tests_run_in_order():
    names = [t.name for t in _program().tests]
    assert names[:3] == ["reset_instruction", "instruction_register", "bypass"]
    assert names[3:15] == [f"opcode_{op:04b}" for op in range(16) if op not in (0, 1, 2, 15)]
    assert names[15:] == [
        "idcode", *_TAP_PATHS, "network_closed",
        "open_sib_ctrl", "open_sib_status", "open_sib_stub", "open_all",
        "sample_preload", "network_hold",
        "write_readback_ctrl", "tms_reset",
    ]


def test_tap_paths_and_network_hold_can_be_left_out():
    names = [t.name for t in _program(tap_paths=False, network_hold=False).tests]
    assert names[15:] == [
        "idcode", "network_closed",
        "open_sib_ctrl", "open_sib_status", "open_sib_stub", "open_all",
        "write_readback_ctrl", "tms_reset",
    ]


def test_every_tap_state_transition_is_taken():
    """All 16 states, each left with TMS=0 and with TMS=1."""
    state, taken = TapState.TEST_LOGIC_RESET, set()
    for c in _program().cycles:
        if not c.trst_n:
            state = TapState.TEST_LOGIC_RESET
            continue
        taken.add((state, c.tms))
        state = next_state(state, c.tms)
    assert taken == {(s, tms) for s in TapState for tms in (0, 1)}


def test_without_idcode_the_tap_paths_use_bypass():
    names = [t.name for t in _program(tap=TapConfig(has_idcode=False)).tests]
    assert {"dr_paths", "bypass_hold", "ir_hold", "bypass_capture"} <= set(names)
    assert not {"idcode_hold", "idcode_capture"} & set(names)


def test_program_starts_with_trst_and_resets_by_tms_at_the_end():
    program = _program()
    lead = [(c.tms, c.tdi, c.trst_n) for c in program.cycles[: len(TRST_LEAD_IN)]]
    assert lead == list(TRST_LEAD_IN)
    tms_reset = [c for c in program.cycles if c.test == "tms_reset"]
    assert [(c.tms, c.tdi, c.trst_n) for c in tms_reset[: len(TMS_RESET_LEAD_IN)]] == list(
        TMS_RESET_LEAD_IN
    )
    assert all(c.trst_n for c in program.cycles[len(TRST_LEAD_IN) :])


@pytest.mark.parametrize("test", ["reset_instruction", "idcode", "tms_reset"])
def test_idcode_scans_read_the_value_then_the_sentinel(test):
    """32 bits of IDCODE, then the sentinel fed first: the register is 32 bits long. (The
    explicit load also shifts the IR first.)"""
    bits = _shifted(_program(), test)[-(32 + MARGIN) :]
    assert bits == bits_from_int(IDCODE_VALUE, 32) + _sentinel(32 + MARGIN)[:MARGIN]


def test_instruction_register_captures_its_pattern_then_shows_its_length():
    bits = _shifted(_program(), "instruction_register")
    assert bits == bits_from_int(CAPTURE_IR_PATTERN, 4) + _sentinel(MARGIN)
    assert bits[:2] == [1, 0]  # IEEE 1149.1: the capture pattern's two LSBs are 01


@pytest.mark.parametrize("test", ["bypass", "opcode_0011", "opcode_1110"])
def test_bypass_and_unimplemented_opcodes_delay_by_one(test):
    bits = _shifted(_program(), test)
    # An unimplemented opcode's test also shifts the IR (the capture pattern) first.
    assert bits[-(1 + MARGIN) :] == [0] + _sentinel(1 + MARGIN)[:MARGIN]


def test_without_idcode_the_reset_instruction_is_bypass():
    program = _program(tap=TapConfig(has_idcode=False))
    names = [t.name for t in program.tests]
    assert "idcode" not in names
    assert "opcode_0001" in names  # the IDCODE opcode is unimplemented now
    assert _shifted(program, "reset_instruction") == [0] + _sentinel(1 + MARGIN)[:MARGIN]


def test_opcodes_follow_the_ir_width():
    names = [t.name for t in _program(tap=TapConfig(ir_width=5)).tests]
    assert sum(n.startswith("opcode_") for n in names) == 32 - 4
    assert "opcode_00011" in names
    assert not any(n.startswith("opcode_") for n in [t.name for t in _program(
        exhaustive_opcodes=False).tests])


def test_a_live_instrument_is_masked_where_it_reaches_tdo():
    """status's captured bit reaches TDO only through its open SIB: its own probe and
    open_all's, the close-all scans right after them (opening the next target), and
    network_hold's capture, which closes every SIB after it."""
    program = _program()
    probe = [c for c in program.cycles if c.test == "open_sib_status" and c.shift]
    assert sum(not c.care for c in probe) == 1
    masked_tests = {c.test for c in program.cycles if c.shift and not c.care}
    assert masked_tests == {"open_sib_status", "open_sib_stub", "open_all", "network_hold"}
    masked_tests = {
        c.test for c in _program(network_hold=False).cycles if c.shift and not c.care
    }
    assert masked_tests == {
        "open_sib_status", "open_sib_stub", "open_all", "write_readback_ctrl"
    }


def test_live_values_and_live_override_the_mask():
    pinned = _program(live_values={"status": 1})
    assert all(c.care for c in pinned.cycles if c.shift)
    unmasked = _program(live=[])
    assert all(c.care for c in unmasked.cycles if c.shift)
    # capture_value (0) then decides: the pinned 1 shows where the unpinned 0 did.
    diff = [
        (a.tdo, b.tdo) for a, b in zip(pinned.cycles, unmasked.cycles) if a.tdo != b.tdo
    ]
    assert diff and all(pair == (1, 0) for pair in diff)


def test_network_probe_matches_the_overshift_oracle():
    """The closed-network test is sib_overshift's probe: its last scan's TDO is what
    expected_probe_tdo predicts for EXTEST plus that probe."""
    graph, root = build_sib_plan(_FLAT)
    program = build_integrity_program(graph, root, margin=MARGIN, live=[])
    ops = select_instruction(OPCODE_EXTEST) + build_overshift_ops(
        graph, frozenset(), margin=MARGIN
    )
    probe_bits = MARGIN + len(graph.chain)
    bits = _shifted(program, "network_closed")[-probe_bits:]
    assert sum(b << k for k, b in enumerate(bits)) == expected_probe_tdo(graph, ops)


def test_write_readback_reads_each_value_back():
    """P = 0b01, then ~P = 0b10, then 0, each written then read back over ctrl's two bits
    (build_integrity_program checks every read against the network model)."""
    program = _program()
    test = next(t for t in program.tests if t.name == "write_readback_ctrl")
    reads = [op for op in test.ops if getattr(op, "mask", None)]

    def masked(op) -> list[int]:
        return [(op.tdo >> k) & 1 for k in range(op.bits) if (op.mask >> k) & 1]

    assert [bin(op.mask).count("1") for op in reads] == [2, 2, 2]
    values = [masked(op) for op in reads]
    assert sorted(values[:2]) == [[0, 1], [1, 0]] and values[2] == [0, 0]


def _alternating(width: int) -> list[int]:
    return [1 - k % 2 for k in range(width)]  # 1, 0, 1, 0, ...: bit 0 set


def test_registers_are_held_in_pause_at_0_and_at_1():
    """Each hold test's register comes out of Pause with what it went in with: an
    alternating pattern P, then ~P, each pushed out by the next (after the IR's own
    capture pattern, which the instruction load shifts out first)."""
    program = _program()
    capture_ir = bits_from_int(CAPTURE_IR_PATTERN, 4)
    p32 = _alternating(32)
    bits = _shifted(program, "idcode_hold")
    assert bits == capture_ir + bits_from_int(IDCODE_VALUE, 32) + p32 + [
        1 - b for b in p32
    ] + _sentinel(32 + MARGIN)[:MARGIN]
    # BYPASS: captured 0, then the 1 fed before the first pause, the 0 before the second.
    assert _shifted(program, "bypass_hold") == capture_ir + [0, 1, 0, 0, 1]
    p4 = _alternating(4)
    assert _shifted(program, "ir_hold") == capture_ir + p4 + [1 - b for b in p4] + [1] * MARGIN


def test_captures_overwrite_a_register_holding_the_complement():
    program = _program()
    idcode = bits_from_int(IDCODE_VALUE, 32)
    bits = _shifted(program, "idcode_capture")[4:]  # after the IR load
    # The first scan feeds ~IDCODE; the second captures IDCODE over it.
    assert bits == idcode + idcode + _sentinel(32 + MARGIN)[:MARGIN]
    bits = _shifted(program, "bypass_capture")[4:]
    assert bits == [0, 1] + [0] + _sentinel(1 + MARGIN)[:MARGIN]


def test_sample_preload_shows_the_network_tail_without_moving_it():
    """open_all left the last SIB (stub's) open, so its bit, 1, is on TDO for the whole
    scan; the network isn't selected, so network_hold then captures it unchanged."""
    program = _program()
    bits = _shifted(program, "sample_preload")[4:]
    assert bits == [1] * (3 + MARGIN)


def test_network_hold_pauses_the_whole_open_network():
    """Captured content, then P, then ~P, one full network length each. (write_readback
    builds after it only because its reads, checked against the model, find every SIB
    closed.)"""
    program = _program()
    bits = _shifted(program, "network_hold")[4:]
    length = len(bits) // 3
    assert length == 3 + 2 + 1 + 3  # every SIB bit, ctrl, status, stub
    p = _alternating(length)
    assert bits[length:] == p + [1 - b for b in p]


def test_program_is_deterministic_and_round_trips_through_json():
    first, second = _program(), _program()
    assert first.cycles == second.cycles
    data = json.loads(json.dumps(first.to_json()))
    assert data["format"] == "warptap-tck-program"
    loaded = IntegrityProgram.from_json(data)
    assert loaded.cycles == first.cycles
    assert loaded.tap == first.tap
    assert [t.name for t in loaded.tests] == [t.name for t in first.tests]
    # TRST (3), Run-Test/Idle -> Shift-DR (3), 32 + MARGIN shifts, back to Idle (2).
    assert (data["tests"][0]["start"], data["tests"][0]["stop"]) == (0, 3 + 3 + 36 + 2)


def test_check_integrity_passes_its_own_expectation_and_names_a_mismatch():
    program = _program()
    observed = [c.tdo for c in program.cycles]
    assert check_integrity(program, observed).passed

    index = next(i for i, c in enumerate(program.cycles) if c.test == "bypass" and c.care)
    observed[index] ^= 1
    result = check_integrity(program, observed)
    assert not result.passed
    assert [(f.test, f.cycle) for f in result.failures] == [("bypass", index)]


def test_check_integrity_ignores_dont_care_bits_and_rejects_unknowns():
    program = _program()
    observed: list = [c.tdo for c in program.cycles]
    masked = next(i for i, c in enumerate(program.cycles) if c.shift and not c.care)
    observed[masked] ^= 1
    assert check_integrity(program, observed).passed

    care = next(i for i, c in enumerate(program.cycles) if c.care)
    observed[care] = "x"
    failure = check_integrity(program, observed).failures[0]
    assert (failure.cycle, failure.observed) == (care, -1)
    with pytest.raises(TapIntegrityError, match="observed"):
        check_integrity(program, observed[:-1])


def test_nested_and_mux_networks_get_a_probe_per_sib_and_arm():
    nested, nested_root = build_sib_plan(
        [
            HierarchySpec("bank", children=[InstrumentSpec("deep", width=2, capture_value=1)]),
            InstrumentSpec("top", width=1, capture_value=0),
        ]
    )
    names = [t.name for t in build_integrity_program(nested, nested_root).tests]
    assert {"open_sib_bank", "open_sib_deep", "open_sib_top", "open_all"} <= set(names)

    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(values=(1,), instrument=InstrumentNode("a", 1, 1)),
            ScanArm(values=(2,), instrument=InstrumentNode("b", 2, 2)),
        ),
    )
    graph = PhysicalGraph(chain=(mux,))
    root = ModuleInstance("top", children=(ModuleInstance("a"), ModuleInstance("b")))
    names = [t.name for t in build_integrity_program(graph, root).tests]
    assert {"open_mux_a_1", "open_mux_a_2"} <= set(names)
    assert "open_all" not in names  # no SIB outside the mux


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"margin": 0}, "margin"),
        ({"live": ["nope"]}, "unknown"),
        ({"live": ["ctrl"]}, "WRITE"),
        ({"live_values": {"stub": 1}}, "aren't live"),
        ({"live_values": {"status": 2}}, "does not fit"),
    ],
)
def test_bad_options_are_refused(kwargs, match):
    with pytest.raises(TapIntegrityError, match=match):
        _program(**kwargs)


def test_select_instruction_checks_the_opcode_width():
    assert len(select_instruction(0b1111)) == 3
    with pytest.raises(ValueError):
        select_instruction(0b10000)
