"""warptap.wsp_integrity: the integrity program of an IEEE 1500 wrapper serial port -- WIR,
WBY and boundary-register tests, each cycle carrying what the model expects at WSO, at
the wrapped outputs and at the wrapper scan-outs.

Every expectation is the model's own (``Wsp1500Sim``), run twice with the core's outputs at 0
and at 1: where the runs disagree the bit is no-care."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from warptap.wsp1500_model import (
    DEFAULT_INSTRUCTIONS,
    Instruction,
    WbrCell,
    Wrapper1500,
    WspChainDesign,
)
from warptap.wsp_integrity import (
    FORMAT,
    VERSION,
    WspIntegrityError,
    WspIntegrityProgram,
    build_wsp_integrity_program,
    check_wsp_integrity,
    drive_ports,
    observation_points,
    replay_wsp_program,
)

BLOCK = Wrapper1500(
    name="blk",
    chains=(
        (WbrCell("a", "input"), WbrCell("b", "input")),
        (WbrCell("y", "output"),),
    ),
)
SOC = WspChainDesign(top="soc", wrappers=(BLOCK, BLOCK))


def _shifts(program: WspIntegrityProgram, test: str, *, wir: bool = False) -> list[int]:
    """The step numbers of ``test`` that shift a data register (ShiftWR high, the WIR
    deselected) -- or, with ``wir``, the WIR."""
    return [
        n
        for n, step in enumerate(program.steps)
        if step.test == test and step.cycle.shiftwr and bool(step.cycle.selectwir) == wir
    ]


def _column(program: WspIntegrityProgram, name: str, steps: list[int]) -> list[object]:
    k = [p.name for p in program.observe].index(name)
    return [program.steps[n].expect[k] for n in steps]


def test_the_tests_of_a_block_in_program_order() -> None:
    program = build_wsp_integrity_program(BLOCK)
    names = [name for name, _, _ in program.tests]
    assert names == [
        "reset",
        "wir_length",
        *[f"code_{code:03b}" for code in range(8)],
        "held_update",
        "capture_update",
    ]
    # The tests tile the program in order, with no gaps.
    assert program.tests[0][1] == 0 and program.tests[-1][2] == len(program)
    assert all(a[2] == b[1] for a, b in zip(program.tests, program.tests[1:]))
    assert [program.test_of(n) for n in (0, len(program) - 1)] == [
        "reset",
        "capture_update",
    ]


def test_without_exhaustive_opcodes_only_the_instructions_own_codes_are_tested() -> None:
    program = build_wsp_integrity_program(BLOCK, exhaustive_opcodes=False)
    codes = [n for n, _, _ in program.tests if n.startswith("code_")]
    assert codes == [f"code_{i.code:03b}" for i in sorted(DEFAULT_INSTRUCTIONS, key=lambda i: i.code)]


def test_a_chain_tests_one_wrapper_at_a_time_with_the_others_in_bypass() -> None:
    program = build_wsp_integrity_program(SOC)
    names = [name for name, _, _ in program.tests]
    assert names[:2] == ["reset", "wir_length"]
    assert [n for n in names if n.startswith("u0_")] == [f"u0_code_{c:03b}" for c in range(8)]
    assert [n for n in names if n.startswith("u1_")] == [f"u1_code_{c:03b}" for c in range(8)]
    assert names[-2:] == ["held_update", "capture_update"]


def test_a_block_observes_wso_its_outputs_and_its_scan_outs_a_chain_wso_and_scan_outs() -> None:
    assert [(p.name, p.kind) for p in observation_points(BLOCK)] == [
        ("wso", "wso"),
        ("y", "output"),
        ("so0", "scan_out"),
        ("so1", "scan_out"),
    ]
    assert [(p.name, p.kind) for p in observation_points(SOC)] == [
        ("wso", "wso"),
        ("u0.so0", "scan_out"),
        ("u0.so1", "scan_out"),
        ("u1.so0", "scan_out"),
        ("u1.so1", "scan_out"),
    ]
    assert [p.name for p in observation_points(BLOCK, scan_outs=False)] == ["wso", "y"]
    assert drive_ports(BLOCK) == ("a", "b")
    assert drive_ports(SOC) == ("u0.a", "u0.b", "u1.a", "u1.b")


def test_the_bypass_registers_show_the_chains_length_in_the_reset_test() -> None:
    """WRSTN and CaptureWR clear every WBY: a sentinel through N of them comes out N
    shifts late, behind N zeros."""
    for design, depth in ((BLOCK, 1), (SOC, 2)):
        program = build_wsp_integrity_program(design, margin=8)
        shifts = _shifts(program, "reset")
        assert len(shifts) == depth + 8
        got = _column(program, "wso", shifts)
        assert got[:depth] == [0] * depth
        sent = [program.steps[n].cycle.wsi for n in shifts]
        assert got[depth:] == sent[:8]


def test_the_wir_length_test_shows_the_sentinel_after_exactly_as_many_shifts_as_the_wirs_are_long() -> None:
    for design, total in ((BLOCK, 3), (SOC, 6)):
        program = build_wsp_integrity_program(design, margin=8)
        shifts = _shifts(program, "wir_length", wir=True)
        assert len(shifts) == total + 8
        sent = [program.steps[n].cycle.wsi for n in shifts]
        got = _column(program, "wso", shifts)
        # The shift stage is unknown after WRSTN: no care until the sentinel arrives.
        assert got[:total] == [None] * total
        assert got[total:] == sent[:8]
        assert sent[:8] != [0] * 8 and sent[8:] == [0] * total


def test_ws_extest_captures_the_input_ports_and_holds_the_output_cell() -> None:
    """WS_EXTEST: the register is 3 long (y next to WSO, then b, a). The input cells capture
    the ports the program drives -- a, b = 0, 1 in the first scan and 1, 0 in the second --
    and the output cell, which drives its port from its flop, holds what was shifted into it."""
    program = build_wsp_integrity_program(BLOCK, margin=8)
    shifts = _shifts(program, "code_001")
    # Two scans (ports phase 0, then phase 1), each 3 + margin shifts.
    assert len(shifts) == 2 * (3 + 8)
    first, second = shifts[: 3 + 8], shifts[3 + 8 :]
    got1, got2 = _column(program, "wso", first), _column(program, "wso", second)
    sent1 = [program.steps[n].cycle.wsi for n in first]
    sent2 = [program.steps[n].cycle.wsi for n in second]
    assert got1[0] is None  # y has never been loaded
    assert (got1[1], got1[2]) == (1, 0) and (got2[1], got2[2]) == (0, 1)
    assert got2[0] == sent1[8]  # y holds the first of the last three bits shifted in
    assert got1[3:] == sent1[:8] and got2[3:] == sent2[:8]


def test_ws_intest_captures_the_core_into_the_output_cell_which_is_no_care() -> None:
    program = build_wsp_integrity_program(BLOCK, margin=8)
    shifts = _shifts(program, "code_100")
    first, second = shifts[: 3 + 8], shifts[3 + 8 :]
    assert _column(program, "wso", first)[0] is None
    assert _column(program, "wso", second)[0] is None


def test_a_bypass_code_scans_a_single_bit() -> None:
    program = build_wsp_integrity_program(BLOCK, margin=8)
    shifts = _shifts(program, "code_000")
    assert len(shifts) == 1 + 8
    got = _column(program, "wso", shifts)
    sent = [program.steps[n].cycle.wsi for n in shifts]
    assert got[0] == 0 and got[1:] == sent[:8]


def test_an_unused_code_acts_as_bypass_in_the_program() -> None:
    program = build_wsp_integrity_program(BLOCK, margin=8)
    assert len(_shifts(program, "code_101")) == len(_shifts(program, "code_000"))


def test_what_the_core_decides_is_no_care_and_the_rest_is_cared_for() -> None:
    program = build_wsp_integrity_program(BLOCK)
    unknown = [
        (n, p.name)
        for n, step in enumerate(program.steps)
        for k, p in enumerate(program.observe)
        if step.expect[k] is None
    ]
    cared = sum(
        value is not None for step in program.steps for value in step.expect
    )
    assert unknown and cared > len(unknown)
    # Live bits: every no-care bit differs between a model with the core at 0 and at 1,
    # or is unknown to both (nothing to expect); every care bit agrees in both.
    low, high = replay_wsp_program(program, live=0), replay_wsp_program(program, live=1)
    for n, step in enumerate(program.steps):
        for k, value in enumerate(step.expect):
            if value is not None:
                assert low[n][k] == high[n][k] == value
    assert any(
        low[n][k] != high[n][k] and step.expect[k] is None
        for n, step in enumerate(program.steps)
        for k in range(len(program.observe))
    )


@pytest.mark.parametrize("design", [BLOCK, SOC], ids=["block", "chain"])
@pytest.mark.parametrize("live", [0, 1])
def test_the_model_replays_its_own_program_with_no_mismatch(design, live) -> None:
    program = build_wsp_integrity_program(design)
    assert check_wsp_integrity(program, replay_wsp_program(program, live=live)) == []


def test_a_wrapper_that_decodes_an_instruction_differently_is_caught() -> None:
    """The check has teeth: a WS_EXTEST that doesn't hold the cells, an extra serial
    instruction, a changed length -- each differs from the program somewhere."""
    program = build_wsp_integrity_program(BLOCK)
    swapped = tuple(
        replace(i, mode=(0, 0)) if i.name == "WS_EXTEST" else i for i in DEFAULT_INSTRUCTIONS
    )
    assert check_wsp_integrity(program, replay_wsp_program(program, replace(BLOCK, instructions=swapped)))
    serial = tuple(
        replace(i, serial=True) if i.name == "WP_INTEST" else i for i in DEFAULT_INSTRUCTIONS
    )
    assert check_wsp_integrity(program, replay_wsp_program(program, replace(BLOCK, instructions=serial)))
    longer = Wrapper1500(
        name="blk",
        chains=((WbrCell("a", "input"), WbrCell("b", "input"), WbrCell("c", "input")),
                (WbrCell("y", "output"),)),
    )
    with pytest.raises(WspIntegrityError, match="doesn't drive"):
        replay_wsp_program(program, longer)


def test_the_program_is_json_and_reads_back_to_the_same_program() -> None:
    program = build_wsp_integrity_program(SOC)
    data = json.loads(json.dumps(program.to_json()))
    assert data["format"] == FORMAT and data["version"] == VERSION
    again = WspIntegrityProgram.from_json(data)
    assert [s.cycle for s in again.steps] == [s.cycle for s in program.steps]
    assert [s.expect for s in again.steps] == [s.expect for s in program.steps]
    assert [s.ports for s in again.steps] == [s.ports for s in program.steps]
    assert again.tests == program.tests and again.observe == program.observe
    assert again.drive == program.drive and dict(again.roles) == dict(program.roles)
    assert [s.test for s in again.steps] == [s.test for s in program.steps]
    assert again.digest() == program.digest()
    # A program read from JSON checks a design by replaying it.
    assert check_wsp_integrity(again, replay_wsp_program(again, SOC)) == []


def test_the_json_names_the_roles_the_columns_and_the_care_strings() -> None:
    data = build_wsp_integrity_program(BLOCK).to_json()
    assert data["roles"]["wsi"] == "wsi" and data["roles"]["wrstn"] == "wrstn"
    count = len(data["wrstn"])
    for column in ("selectwir", "shiftwr", "capturewr", "updatewr", "wsi", "pulse"):
        assert len(data[column]) == count
    assert set(data["inputs"]) == {"a", "b"}
    assert set(data["expect"]) == {"wso", "y", "so0", "so1"}
    wso = data["expect"]["wso"]
    assert len(wso["value"]) == len(wso["care"]) == count
    # No-care bits carry a 0 value and a 0 care.
    assert all(v == "0" for v, c in zip(wso["value"], wso["care"]) if c == "0")


def test_the_digest_follows_the_content() -> None:
    a = build_wsp_integrity_program(BLOCK)
    assert a.digest() == build_wsp_integrity_program(BLOCK).digest()
    assert a.digest() != build_wsp_integrity_program(BLOCK, margin=9).digest()
    assert a.digest() != build_wsp_integrity_program(SOC).digest()


def test_a_chains_wirs_are_loaded_far_wrapper_first() -> None:
    """u0's code_010 test: the WIR scan is 6 bits, u1's (the far one's) 000 first."""
    program = build_wsp_integrity_program(SOC)
    start = next(s for n, s, _ in program.tests if n == "u0_code_010")
    stop = next(e for n, _, e in program.tests if n == "u0_code_010")
    shifts = [
        n
        for n in range(start, stop)
        if program.steps[n].cycle.selectwir and program.steps[n].cycle.shiftwr
    ]
    bits = [program.steps[n].cycle.wsi for n in shifts]
    # u1's WIR holds 000 (3 bits); u0's code 0b010 reads 0, 1, 0 whichever the order.
    assert bits == [0, 0, 0, 0, 1, 0]


def test_a_program_with_a_margin_too_small_to_show_a_length_is_refused() -> None:
    with pytest.raises(WspIntegrityError, match="margin"):
        build_wsp_integrity_program(BLOCK, margin=1)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="warptap-tap-program"),
        lambda d: d.update(version=2),
        lambda d: d.pop("expect"),
        lambda d: d.update(shiftwr=d["shiftwr"][:-1]),
        lambda d: d.update(wsi=d["wsi"][:-1] + "2"),
        lambda d: d["tests"].append({"name": "x", "start": 5, "stop": 10**6}),
        lambda d: d.update(wrstn=""),
    ],
    ids=["format", "version", "no-expect", "ragged", "not-binary", "range", "empty"],
)
def test_malformed_programs_are_refused(mutate) -> None:
    data = json.loads(json.dumps(build_wsp_integrity_program(BLOCK).to_json()))
    mutate(data)
    with pytest.raises(WspIntegrityError):
        WspIntegrityProgram.from_json(data)


def test_an_instruction_set_without_a_serial_instruction_still_has_a_program() -> None:
    plain = Wrapper1500(
        name="plain",
        chains=((WbrCell("a", "input"), WbrCell("y", "output")),),
        instructions=(
            Instruction("WS_BYPASS", 0b000, False, (0, 0)),
            Instruction("WP_INTEST", 0b010, False, (1, 0)),
        ),
    )
    program = build_wsp_integrity_program(plain)
    assert [n for n, _, _ in program.tests][-2:] == ["held_update", "capture_update"]
    assert check_wsp_integrity(program, replay_wsp_program(program, live=1)) == []


def test_the_core_side_output_of_a_chain_is_never_an_observation() -> None:
    """Block outputs in a chain aren't pins: only WSO and the scan-outs are observed."""
    program = build_wsp_integrity_program(SOC)
    assert {p.kind for p in program.observe} == {"wso", "scan_out"}
    forced = build_wsp_integrity_program(SOC, outputs=True)
    assert {p.kind for p in forced.observe} == {"wso", "output", "scan_out"}
