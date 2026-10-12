"""Pure-Python tests for warptap.wsp1500_model: an IEEE 1500 wrapper's registers (WIR,
WBY, the boundary register) and their cycle-by-cycle behaviour on the wrapper serial
port, one wrapper and a chain of them.

The reference every other 1500 piece of warptap (ICL, PDL retargeting, the integrity
program) is checked against; the directed vectors below are the behaviour it fixes."""

from __future__ import annotations

import pytest

from warptap.wsp1500_model import (
    DEFAULT_INSTRUCTIONS,
    WbrCell,
    Wrapper1500,
    Wsp1500Chain,
    Wsp1500Sim,
    WspCycle,
    program_cycles,
)

# Chain 0 shifts a, b (head first); chain 1 is y. Serial order: a, b, y.
BLOCK = Wrapper1500(
    name="blk",
    chains=(
        (WbrCell("a", "input"), WbrCell("b", "input")),
        (WbrCell("y", "output"),),
    ),
)


def _load(sim: Wsp1500Sim, name: str) -> None:
    for cycle in program_cycles(BLOCK, name):
        sim.step(cycle)


@pytest.mark.parametrize(
    ("name", "code", "serial", "mode"),
    [
        ("WS_BYPASS", 0b000, False, (0, 0)),
        ("WS_EXTEST", 0b001, True, (0, 1)),
        ("WP_INTEST", 0b010, False, (1, 0)),
        ("WP_EXTEST", 0b011, False, (0, 1)),
        ("WS_INTEST", 0b100, True, (1, 0)),
    ],
)
def test_the_default_instructions(
    name: str, code: int, serial: bool, mode: tuple[int, int]
) -> None:
    (found,) = [i for i in DEFAULT_INSTRUCTIONS if i.name == name]
    assert (found.code, found.serial, found.mode) == (code, serial, mode)
    assert BLOCK.instruction_of(code) == name


@pytest.mark.parametrize("code", [0b101, 0b110, 0b111])
def test_an_unused_code_acts_as_bypass(code: int) -> None:
    assert BLOCK.instruction_of(code) == "WS_BYPASS"


def test_a_wrapper_describes_its_boundary_register() -> None:
    assert BLOCK.cells == (
        WbrCell("a", "input"),
        WbrCell("b", "input"),
        WbrCell("y", "output"),
    )
    assert BLOCK.sides == ("input", "input", "output")
    assert BLOCK.ir_bits == 3
    with pytest.raises(ValueError, match="side"):
        WbrCell("z", "inout")
    with pytest.raises(ValueError, match="same code"):
        Wrapper1500(
            "bad",
            chains=((WbrCell("a", "input"),),),
            instructions=(DEFAULT_INSTRUCTIONS[0], DEFAULT_INSTRUCTIONS[0]),
        )


def test_wrstn_clears_the_instruction_and_the_bypass_register() -> None:
    sim = Wsp1500Sim(BLOCK)
    assert sim.instruction is None  # unknown until reset
    assert sim.step(WspCycle(wrstn=0, pulse=False)) == 0  # WBY is 0
    assert sim.instruction == "WS_BYPASS"


@pytest.mark.parametrize(
    "name", ["WS_BYPASS", "WS_EXTEST", "WP_INTEST", "WP_EXTEST", "WS_INTEST"]
)
def test_shifting_the_code_in_and_updating_puts_the_instruction_in_force(
    name: str,
) -> None:
    sim = Wsp1500Sim(BLOCK)
    _load(sim, name)
    assert sim.instruction == name


def test_the_update_stage_is_a_falling_edge_register_so_a_shift_alone_changes_nothing() -> (
    None
):
    sim = Wsp1500Sim(BLOCK)
    _load(sim, "WP_INTEST")
    # Shift WS_EXTEST's code in without updating: the instruction stays.
    for bit in (0, 0, 1):
        sim.step(WspCycle(selectwir=1, shiftwr=1, wsi=bit))
    assert sim.instruction == "WP_INTEST"
    sim.step(WspCycle(selectwir=1, updatewr=1))
    assert sim.instruction == "WS_EXTEST"


def test_an_update_without_selectwir_or_a_shift_without_it_leaves_the_wir_alone() -> (
    None
):
    sim = Wsp1500Sim(BLOCK)
    _load(sim, "WP_INTEST")
    for bit in (1, 1, 1):
        sim.step(WspCycle(selectwir=0, shiftwr=1, wsi=bit))  # shifts WBY, not the WIR
    sim.step(WspCycle(selectwir=0, updatewr=1))  # nothing selected to update
    assert sim.instruction == "WP_INTEST"


def test_wso_is_the_wir_when_selected_wby_in_bypass_and_the_wbr_in_a_serial_instruction() -> (
    None
):
    sim = Wsp1500Sim(BLOCK)
    sim.step(WspCycle(wrstn=0, pulse=False))
    assert sim.wso(WspCycle(selectwir=0)) == 0  # WBY, reset
    # The WIR's last bit, the one shifted in three shifts before.
    for bit in (1, 0, 1):
        sim.step(WspCycle(selectwir=1, shiftwr=1, wsi=bit))
    assert sim.wso(WspCycle(selectwir=1)) == 1  # bits 1, 0, 1: bit 2 is the first in
    sim.step(WspCycle(selectwir=1, updatewr=1))  # 101: unused, bypass
    assert sim.wso(WspCycle(selectwir=0)) == 0
    _load(sim, "WS_EXTEST")
    assert sim.wso(WspCycle(selectwir=0)) is None  # the WBR, unknown yet


def test_wby_is_one_bit_that_captures_zero() -> None:
    sim = Wsp1500Sim(BLOCK)
    _load(sim, "WS_BYPASS")
    sim.step(WspCycle(shiftwr=1, wsi=1))
    assert sim.wso(WspCycle()) == 1
    sim.step(WspCycle(shiftwr=1, wsi=0))
    assert sim.wso(WspCycle()) == 0
    sim.step(WspCycle(shiftwr=1, wsi=1))
    sim.step(WspCycle(capturewr=1))
    assert sim.wso(WspCycle()) == 0


def test_ws_extest_captures_the_ports_into_the_input_cells_and_shifts_them_out() -> None:
    sim = Wsp1500Sim(BLOCK)
    _load(sim, "WS_EXTEST")
    ports = (1, 0, 1)  # a, b and the core's y
    sim.step(WspCycle(capturewr=1, cfi=ports))
    # The input cells hold their ports; the output cell kept its (unknown) value.
    shifted = [sim.step(WspCycle(shiftwr=1, wsi=0, cfi=ports)) for _ in range(3)]
    assert shifted[0] is None  # y, the output cell, held its unknown flop
    assert shifted[1:] == [0, 1]  # then b, then a


def test_a_serial_instruction_holds_every_cell_unless_the_wir_is_deselected_and_it_captures() -> (
    None
):
    sim = Wsp1500Sim(BLOCK)
    _load(sim, "WS_EXTEST")
    sim.step(WspCycle(capturewr=1, cfi=(1, 1, 1)))
    # CaptureWR with the WIR selected is the WIR's own capture: the cells hold.
    sim.step(WspCycle(selectwir=1, capturewr=1, cfi=(0, 0, 0)))
    sim.step(WspCycle(cfi=(0, 0, 0)))
    out = [sim.step(WspCycle(shiftwr=1, wsi=0, cfi=(0, 0, 0))) for _ in range(3)]
    assert out[1:] == [1, 1]


def test_parallel_instructions_drive_the_cells_by_the_mode() -> None:
    sim = Wsp1500Sim(BLOCK)
    inputs = WspCycle(cfi=(1, 0, 1))
    _load(sim, "WS_BYPASS")  # functional: every cell passes its input through
    assert sim.outputs(inputs) == [1, 0, 1]
    _load(sim, "WP_INTEST")  # input cells hold (unknown), the output cell is safe: 0
    assert sim.outputs(inputs) == [None, None, 0]
    _load(sim, "WP_EXTEST")  # input cells safe: 0, the output cell holds
    assert sim.outputs(inputs) == [0, 0, None]


def test_a_chain_of_wrappers_shares_the_controls_and_passes_wso_to_the_next_wsi() -> None:
    chain = Wsp1500Chain((BLOCK, BLOCK))
    cycles = []
    # One load for both WIRs: the one nearest WSO first, bit 2 first.
    for code in (0b001, 0b010):  # WS_EXTEST for the far one, WP_INTEST for the near
        for k in (2, 1, 0):
            cycles.append(WspCycle(selectwir=1, shiftwr=1, wsi=(code >> k) & 1))
    cycles.append(WspCycle(selectwir=1, updatewr=1))
    chain.run([WspCycle(wrstn=0, pulse=False), *cycles])
    assert [s.instruction for s in chain.sims] == ["WP_INTEST", "WS_EXTEST"]
    # In bypass the chain is two bits long: what goes in comes out two shifts later.
    chain = Wsp1500Chain((BLOCK, BLOCK))
    out = chain.run(
        [
            WspCycle(wrstn=0, pulse=False),
            *[WspCycle(shiftwr=1, wsi=b) for b in (1, 0, 1, 1, 0, 0)],
        ]
    )
    assert out[-4:] == [1, 0, 1, 1]  # the bits fed two shifts before


def test_unknown_values_stay_unknown_until_something_defines_them() -> None:
    sim = Wsp1500Sim(BLOCK)
    assert sim.wso(WspCycle()) is None
    assert sim.outputs(WspCycle(cfi=(1, 1, 1))) == [None, None, None]
    sim.step(WspCycle(wrstn=0, pulse=False))
    assert sim.outputs(WspCycle(cfi=(1, 1, 1))) == [1, 1, 1]
