"""Live PDL grammar validation against a real, independently-authored grammar
(implementation_plan.md §7 Stage 11, §8's own testing strategy: "validated by feeding output
through an existing consumer... not a self-written parser"). Runs `to_pdl()`'s own output
through a real PDL parser compiled from the vendored `Honza255/icl_parser` submodule's own
`src/pdl_parser/pdl.g4` -- see `pdl_emit.py`'s own module docstring for how that grammar went
from an unwired, never-compiled artifact to a real parser, and the real bugs (in both the
grammar and in `pdl_emit.py` itself) that surfaced along the way.

**Scope, stated as loudly as every other external-tool validation in this project**: this
proves *grammar* correctness (`to_pdl()`'s output really is valid PDL syntax, confirmed by
code this project didn't write) -- not *retargeting-semantics* correctness. No tool anywhere
checks the latter for PDL; the vendored `icl_parser`'s own retargeting engine
(`IclRegisterModel`) was tried directly against a warptap-canonical SIB network and confirmed
not to work for it (its own README lists "Data registers"/"Retargeting for data registers" as
explicitly not supported, exactly the register kind a WRITE instrument's committed value lives
in) -- see `pdl_emit.py`'s own module docstring for that investigation. This is the same tier
Stage 7's SVF/STAPL validation already sits at (OpenOCD/the Jam STAPL Player also only prove
grammar, via their own no-hardware stub drivers, never behavior).

**A real, confirmed gap in this specific bundled grammar, found only by fixing a real bug in
this file's own first draft**: the first version of `pdl_parser_module` invoked the grammar's
bare `commands` rule directly as its entry point, which does NOT require the whole input to be
consumed (`command*` legally matches zero times) -- so it silently reported zero errors for
both genuine garbage AND, critically, for every real `to_pdl()` output, which always opens
with `iTarget <name>;`. Investigating why led to discovering this specific "PDL0" grammar has
no `iTarget`/scoping construct at all (confirmed: absent from its own `keyword` list, absent
from `command`'s own alternatives, absent anywhere in the file) -- an older PDL dialect than
the one this project's own earlier research found `iTarget` in. Fixed at the grammar level (a
new `flat_commands : commands EOF ;` entry rule, added to the fork) so leftover unconsumed
input is now a real, reported error -- see `pdl_parser_module`'s own docstring for the full
story. Consequence: this grammar can validate each individual statement's own inner syntax but
NOT a `to_pdl()`-emitted sequence as a whole (which always includes `iTarget`) -- every test
below strips `iTarget` lines before validating (`_strip_itarget_lines`), and
`test_itarget_is_a_confirmed_gap_in_this_grammar` below pins the gap itself with a real,
reported error, rather than leaving it silently unchecked the way the first draft did.
"""

from __future__ import annotations

from warptap.icl_model import Alias, InstrumentDirection, SignalBinding
from warptap.pdl_emit import to_pdl
from warptap.pdl_history import PdlRunLoopStmt
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan


def _strip_itarget_lines(pdl_text: str) -> str:
    """This bundled PDL0 grammar has no iTarget/scoping construct at all (confirmed, see
    pdl_parser_module's own docstring) -- strip iTarget lines before validating everything
    else to_pdl() emits, since real to_pdl() output always opens with one. Only ever removes
    whole lines starting with the literal statement keyword, never touches anything else."""
    lines = [line for line in pdl_text.splitlines() if not line.startswith("iTarget ")]
    return "\n".join(lines) + ("\n" if lines else "")


def test_itarget_is_a_confirmed_gap_in_this_grammar(pdl_parser_module):
    """Pins the real, confirmed gap this module's own docstring describes, rather than leaving
    it silently unchecked: an iTarget-prefixed sequence genuinely fails against this specific
    bundled grammar (not a to_pdl() bug -- this dialect simply has no such construct)."""
    pdl_text = to_pdl([], build_sib_plan([], top_name="chip")[0])  # sanity: empty parses clean
    assert pdl_parser_module(pdl_text) == []

    errors = pdl_parser_module("iTarget ctrl_write;\niWrite ctrl_write 0x1;\niApply;\n")
    assert errors != []
    assert "iTarget" in errors[0]


def test_garbage_is_genuinely_rejected(pdl_parser_module):
    """Sanity check that the validator has real teeth -- confirms the fixed flat_commands
    entry point (not the original, silently-permissive bare `commands` one) is what's wired
    up here."""
    assert pdl_parser_module("completely bogus garbage 123 ;;;") != []


def test_write_read_apply_round_trip_is_grammatically_valid(pdl_parser_module):
    specs = [
        InstrumentSpec(
            "ctrl_write", width=1, capture_value=0,
            direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("bist_start"),),
        ),
    ]
    graph, root = build_sib_plan(specs, top_name="chip")
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert pdl_parser_module(_strip_itarget_lines(pdl_text)) == []


def test_read_statement_is_grammatically_valid(pdl_parser_module):
    specs = [InstrumentSpec("status_b", width=8, capture_value=0)]
    graph, root = build_sib_plan(specs, top_name="chip")
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("status_b")
    pdl.iRead(0xAB)
    pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert pdl_parser_module(_strip_itarget_lines(pdl_text)) == []


def test_irunloop_tck_is_grammatically_valid(pdl_parser_module):
    graph, _root = build_sib_plan([], top_name="chip")
    pdl_text = to_pdl([PdlRunLoopStmt(100)], graph)
    assert pdl_parser_module(pdl_text) == []


def test_irunloop_sck_with_port_name_is_grammatically_valid(pdl_parser_module):
    """Regression test for the real bug this grammar caught: -sck used to render as a bare
    flag with no port name, which fails to parse (the grammar's own irunloop_def rule requires
    a port operand after -sck). Confirms the fix (pdl_emit.py now renders it)."""
    graph, _root = build_sib_plan([], top_name="chip")
    pdl_text = to_pdl([PdlRunLoopStmt(100, "sysclk")], graph)
    assert "sysclk" in pdl_text
    assert pdl_parser_module(pdl_text) == []


def test_alias_addressed_write_is_grammatically_valid(pdl_parser_module):
    specs = [
        InstrumentSpec(
            "status_reg", width=4, capture_value=0,
            aliases=(Alias("hi", 2, 3), Alias("lo", 0, 1)),
        ),
    ]
    graph, root = build_sib_plan(specs, top_name="chip")
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("status_reg")
    pdl.iWrite(0b11, field="hi")
    pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert pdl_parser_module(_strip_itarget_lines(pdl_text)) == []


def test_realistic_multi_instrument_scenario_is_grammatically_valid(pdl_parser_module):
    """Mirrors test_mem_subsystem_mbist_pdl_emit.py's own real scenario -- write, settle
    (mixing both -tck and -sck iRunLoop forms), read, write again -- a genuinely mixed,
    multi-statement history, not just one statement kind in isolation."""
    specs = [
        InstrumentSpec(
            "self_repair_start", width=1, capture_value=0,
            direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("self_repair_start"),),
        ),
        InstrumentSpec(
            "self_repair_busy", width=1, capture_value=0,
            direction=InstrumentDirection.READ, signal_bits=(SignalBinding("self_repair_busy"),),
        ),
    ]
    graph, root = build_sib_plan(specs, top_name="mem_subsystem_mbist")
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("self_repair_start")
    pdl.iWrite(1)
    pdl.iApply()
    pdl.iRunLoop(10)
    pdl.iRunLoop(5, sck_port="sysclk")
    pdl.iTarget("self_repair_busy")
    pdl.iRead(1)
    pdl.iApply()
    pdl.iTarget("self_repair_start")
    pdl.iWrite(0)
    pdl.iApply()

    pdl_text = to_pdl(pdl.history, graph)
    assert pdl_parser_module(_strip_itarget_lines(pdl_text)) == []
