"""warptap.pdl_1500: PDL on an IEEE 1500 wrapper, or a chain of them -- iReset, iTarget,
iWrite, iRead, iRunLoop, iApply -- retargeted onto the wrapper serial port as the scan
operations of :mod:`warptap.tap_ir` (:mod:`warptap.wsp_play` lowers them to cycles).

A WIR write is a WIR scan through every wrapper's WIR at once, the far wrapper's bits
first; a wrapper not written keeps its committed instruction. A register write is a data
scan through whatever each wrapper's instruction selects: its bypass bit, or in a serial
instruction its boundary register, one ICL register per wrapper chain."""

from __future__ import annotations

import pytest

from warptap.pdl_1500 import (
    Pdl1500Error,
    Wsp1500Interpreter,
    import_pdl_1500,
    to_pdl_1500,
)
from warptap.tap_ir import Runtest, ShiftDR, ShiftIR
from warptap.wsp1500_model import (
    WbrCell,
    Wrapper1500,
    Wsp1500Chain,
    WspChainDesign,
)
from warptap.wsp_ir import WspReset
from warptap.wsp_play import mismatches, play, to_wsp_rows

BLOCK = Wrapper1500(
    name="blk",
    chains=(
        (WbrCell("a", "input"), WbrCell("b", "input")),
        (WbrCell("y", "output"),),
    ),
)
SOC = WspChainDesign(top="soc", wrappers=(BLOCK, BLOCK))


def _run(interp: Wsp1500Interpreter, design) -> Wsp1500Chain:
    wrappers = design.wrappers if isinstance(design, WspChainDesign) else (design,)
    chain = Wsp1500Chain(list(wrappers))
    play(chain, to_wsp_rows(interp.program))
    return chain


def test_a_wir_write_by_instruction_name_is_a_wir_scan_with_the_iclvalue() -> None:
    pdl = Wsp1500Interpreter(BLOCK)
    pdl.iReset()
    pdl.iWrite("WIR", "WS_EXTEST")
    pdl.iApply()
    # WS_EXTEST's ICL value is 3'b100: bit 0 (0) shifts first, then 0, then 1.
    assert pdl.program == [WspReset(), ShiftIR(3, 0b100)]
    assert _run(pdl, BLOCK).sims[0].instruction == "WS_EXTEST"


def test_a_value_may_be_a_number_and_the_block_may_be_addressed_by_name() -> None:
    pdl = Wsp1500Interpreter(BLOCK)
    pdl.iReset()
    pdl.iTarget("blk")
    pdl.iWrite("WIR", 0b010)  # WP_INTEST's value
    pdl.iApply()
    assert pdl.program[-1] == ShiftIR(3, 0b010)
    assert _run(pdl, BLOCK).sims[0].instruction == "WP_INTEST"


def test_one_wir_scan_loads_every_wrapper_of_a_chain_the_far_one_first() -> None:
    pdl = Wsp1500Interpreter(SOC)
    pdl.iReset()
    pdl.iWrite("u0.WIR", "WP_INTEST")
    pdl.iWrite("u1.WIR", "WS_EXTEST")
    pdl.iApply()
    assert pdl.program == [WspReset(), ShiftIR(6, 0b100 | (0b010 << 3))]
    chain = _run(pdl, SOC)
    assert [s.instruction for s in chain.sims] == ["WP_INTEST", "WS_EXTEST"]


def test_a_wrapper_not_written_keeps_its_committed_instruction() -> None:
    pdl = Wsp1500Interpreter(SOC)
    pdl.iReset()
    pdl.iWrite("u0.WIR", "WP_INTEST")
    pdl.iWrite("u1.WIR", "WS_EXTEST")
    pdl.iApply()
    pdl.iTarget("u0")
    pdl.iWrite("WIR", "WS_BYPASS")
    pdl.iApply()
    assert pdl.program[-1] == ShiftIR(6, 0b100 | (0b000 << 3))
    assert [s.instruction for s in _run(pdl, SOC).sims] == ["WS_BYPASS", "WS_EXTEST"]


def test_a_wir_not_known_and_not_written_is_refused() -> None:
    pdl = Wsp1500Interpreter(SOC)
    pdl.iWrite("u0.WIR", "WP_INTEST")
    with pytest.raises(Pdl1500Error, match="u1.*unknown"):
        pdl.iApply()


def test_a_bypass_register_is_read_where_the_chain_puts_it() -> None:
    """Two wrappers in bypass: u1's bit leaves WSO first, u0's second."""
    pdl = Wsp1500Interpreter(SOC)
    pdl.iReset()
    pdl.iRead("u0.WBY", 0)
    pdl.iApply()
    assert pdl.program[-1] == ShiftDR(2, 0, tdo=0, mask=0b10)
    rows = to_wsp_rows(pdl.program)
    assert mismatches(rows, play(Wsp1500Chain(list(SOC.wrappers)), rows)) == []
    pdl = Wsp1500Interpreter(SOC)
    pdl.iReset()
    pdl.iRead("u0.WBY", 1)  # the reset value is 0
    pdl.iApply()
    rows = to_wsp_rows(pdl.program)
    assert mismatches(rows, play(Wsp1500Chain(list(SOC.wrappers)), rows)) != []


def test_boundary_registers_are_written_chain_by_chain_msb_first_into_the_cells() -> None:
    pdl = Wsp1500Interpreter(BLOCK)
    pdl.iReset()
    pdl.iWrite("WIR", "WS_EXTEST")
    pdl.iApply()
    pdl.iWrite("WBR0", 0b01)  # a (the head, MSB) = 0, b (the tail, LSB) = 1
    pdl.iWrite("WBR1", 0b1)  # y = 1
    pdl.iApply()
    # WBR1's bit goes first (it ends nearest WSO), then WBR0's LSB, then its MSB.
    assert pdl.program[-1] == ShiftDR(3, 0b011)
    sim = _run(pdl, BLOCK).sims[0]
    assert sim.wbr == [0, 1, 1]  # a, b, y


def test_a_register_the_instruction_does_not_select_is_refused() -> None:
    pdl = Wsp1500Interpreter(BLOCK)
    pdl.iReset()
    pdl.iWrite("WBR0", 1)  # WS_BYPASS in force: the data register is WBY
    with pytest.raises(Pdl1500Error, match="WBR0.*WS_BYPASS"):
        pdl.iApply()
    pdl = Wsp1500Interpreter(BLOCK)
    pdl.iReset()
    pdl.iWrite("WIR", "WS_EXTEST")
    pdl.iApply()
    pdl.iWrite("WBY", 1)
    with pytest.raises(Pdl1500Error, match="WBY.*WS_EXTEST"):
        pdl.iApply()


def test_a_wir_and_a_register_in_one_apply_scan_the_wir_first() -> None:
    pdl = Wsp1500Interpreter(BLOCK)
    pdl.iReset()
    pdl.iWrite("WIR", "WS_EXTEST")
    pdl.iWrite("WBR1", 1)
    pdl.iApply()
    assert [type(op) for op in pdl.program] == [WspReset, ShiftIR, ShiftDR]


def test_runloop_is_idle_cycles_and_unknown_things_are_refused() -> None:
    pdl = Wsp1500Interpreter(BLOCK)
    pdl.iRunLoop(4)
    assert pdl.program == [Runtest(4)]
    with pytest.raises(Pdl1500Error, match="no register"):
        pdl.iWrite("NOPE", 1)
        pdl.iApply()
    with pytest.raises(Pdl1500Error, match="no wrapper"):
        pdl.iTarget("u9")
    with pytest.raises(Pdl1500Error, match="WIR"):
        Wsp1500Interpreter(BLOCK).iRead("WIR", 0)
    with pytest.raises(Pdl1500Error, match="no instruction"):
        Wsp1500Interpreter(BLOCK).iWrite("WIR", "WS_NOPE")


def test_the_history_prints_as_pdl_and_reads_back_into_the_same_program() -> None:
    pdl = Wsp1500Interpreter(SOC)
    pdl.iReset()
    pdl.iTarget("u1")
    pdl.iWrite("WIR", "WS_EXTEST")
    pdl.iWrite("u0.WIR", 0b010)
    pdl.iApply()
    pdl.iWrite("u1.WBR1", 1)
    pdl.iRead("u1.WBR0", 0b01)
    pdl.iRunLoop(2)
    pdl.iApply()
    text = to_pdl_1500(pdl.history, SOC)
    assert text.splitlines() == [
        "iReset;",
        "iTarget u1;",
        "iWrite WIR WS_EXTEST;",
        "iWrite u0.WIR 3'b010;",
        "iApply;",
        "iWrite u1.WBR1 1'b1;",
        "iRead u1.WBR0 2'b01;",
        "iRunLoop 2 -sck wrck;",
        "iApply;",
    ]
    again = Wsp1500Interpreter(SOC)
    import_pdl_1500(text, again)
    assert again.program == pdl.program and again.history == pdl.history


def test_pdl_text_that_isnt_this_subset_is_refused() -> None:
    again = Wsp1500Interpreter(SOC)
    for bad in ("iProc foo {};", "iWrite WIR;", "iWrite u0.WIR WS_EXTEST", "iCall x;"):
        with pytest.raises(Pdl1500Error):
            import_pdl_1500(bad + "\n", again)
