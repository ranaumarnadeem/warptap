"""Round-trip tests for PDL import: build a real PDLInterpreter history, emit it with to_pdl()
(Stage 11), parse it back with import_pdl() into a FRESH interpreter over the same graph, and
confirm the recovered history/program match the original -- mirroring
test_icl_import_roundtrip.py's own style, generalized from ICL's declarative shape to PDL's
procedural one (see pdl_import.py's own module docstring for why that changes what "round
trip" means here: recovered state, not a reconstructed object).
"""

from __future__ import annotations

import pytest

from warptap.icl_model import Alias, InstrumentDirection, SignalBinding
from warptap.pdl_emit import to_pdl
from warptap.pdl_import import PdlImportError, import_pdl
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan


def test_write_read_apply_round_trips_through_a_fresh_interpreter(pdl_lexer_parser_classes):
    pdl_lexer_class, pdl_parser_class = pdl_lexer_parser_classes
    specs = [
        InstrumentSpec(
            "ctrl_write", width=1, capture_value=0,
            direction=InstrumentDirection.WRITE, signal_bits=(SignalBinding("bist_start"),),
        ),
    ]
    graph, root = build_sib_plan(specs, top_name="chip")
    original = PDLInterpreter(graph, root)
    original.iTarget("ctrl_write")
    original.iWrite(1)
    original.iApply()
    pdl_text = to_pdl(original.history, graph)

    imported = PDLInterpreter(graph, root)
    import_pdl(
        pdl_text, imported,
        pdl_lexer_class=pdl_lexer_class, pdl_parser_class=pdl_parser_class,
    )

    assert imported.history == original.history
    assert imported.program == original.program


def test_realistic_multi_instrument_scenario_round_trips(pdl_lexer_parser_classes):
    """Mirrors test_pdl_emit_grammar_validation.py's own realistic scenario -- write, settle
    (mixing both -tck and -sck iRunLoop forms), read, write again -- exercising all 5
    supported statement kinds and a real iTarget scope switch in one flow, not just one
    statement kind in isolation."""
    pdl_lexer_class, pdl_parser_class = pdl_lexer_parser_classes
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
    original = PDLInterpreter(graph, root)
    original.iTarget("self_repair_start")
    original.iWrite(1)
    original.iApply()
    original.iRunLoop(10)
    original.iRunLoop(5, sck_port="sysclk")
    original.iTarget("self_repair_busy")
    original.iRead(1)
    original.iApply()
    original.iTarget("self_repair_start")
    original.iWrite(0)
    original.iApply()
    pdl_text = to_pdl(original.history, graph)

    imported = PDLInterpreter(graph, root)
    import_pdl(
        pdl_text, imported,
        pdl_lexer_class=pdl_lexer_class, pdl_parser_class=pdl_parser_class,
    )

    assert imported.history == original.history
    assert imported.program == original.program


def test_alias_addressed_write_round_trips(pdl_lexer_parser_classes):
    """A field naming a declared Alias, not the whole instrument -- confirms import_pdl's own
    current_target/field comparison correctly distinguishes the two cases (this test's own
    field is "hi", never equal to the target instrument's own name "status_reg")."""
    pdl_lexer_class, pdl_parser_class = pdl_lexer_parser_classes
    specs = [
        InstrumentSpec(
            "status_reg", width=4, capture_value=0,
            aliases=(Alias("hi", 2, 3), Alias("lo", 0, 1)),
        ),
    ]
    graph, root = build_sib_plan(specs, top_name="chip")
    original = PDLInterpreter(graph, root)
    original.iTarget("status_reg")
    original.iWrite(0b11, field="hi")
    original.iApply()
    pdl_text = to_pdl(original.history, graph)

    imported = PDLInterpreter(graph, root)
    import_pdl(
        pdl_text, imported,
        pdl_lexer_class=pdl_lexer_class, pdl_parser_class=pdl_parser_class,
    )

    assert imported.history == original.history
    assert imported.program == original.program


def test_unsupported_statement_kind_raises_named_error(pdl_lexer_parser_classes):
    pdl_lexer_class, pdl_parser_class = pdl_lexer_parser_classes
    graph, root = build_sib_plan([], top_name="chip")
    interp = PDLInterpreter(graph, root)
    with pytest.raises(PdlImportError, match="doesn't recognize"):
        import_pdl(
            "iCall someProc;\n", interp,
            pdl_lexer_class=pdl_lexer_class, pdl_parser_class=pdl_parser_class,
        )


def test_together_apply_raises_named_error(pdl_lexer_parser_classes):
    pdl_lexer_class, pdl_parser_class = pdl_lexer_parser_classes
    graph, root = build_sib_plan([], top_name="chip")
    interp = PDLInterpreter(graph, root)
    with pytest.raises(PdlImportError, match="-together"):
        import_pdl(
            "iApply -together;\n", interp,
            pdl_lexer_class=pdl_lexer_class, pdl_parser_class=pdl_parser_class,
        )


def test_invalid_syntax_raises_named_error(pdl_lexer_parser_classes):
    pdl_lexer_class, pdl_parser_class = pdl_lexer_parser_classes
    graph, root = build_sib_plan([], top_name="chip")
    interp = PDLInterpreter(graph, root)
    with pytest.raises(PdlImportError, match="not valid PDL syntax"):
        import_pdl(
            "completely bogus garbage 123 ;;;\n", interp,
            pdl_lexer_class=pdl_lexer_class, pdl_parser_class=pdl_parser_class,
        )
