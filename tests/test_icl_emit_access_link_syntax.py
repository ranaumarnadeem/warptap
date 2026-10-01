"""Grammar-level check of the ``AccessLink`` block ``to_icl`` emits, against the vendored
``Honza255/icl_parser``'s generated ANTLR parser (``icl.g4``'s ``accessLink1149_def`` rules).

This uses the vendored tool's raw ``iclLexer``/``iclParser`` only -- NOT its ``Ijtag`` class, whose
processor raises "Not supported" for any AccessLink -- with a collecting error listener (the tool
itself only prints syntax errors) and an explicit end-of-input check (``icl_source`` has no ``EOF``
in its rule, so leftover unparsed text otherwise goes unnoticed).

**A real limitation of that tool shapes this test.** ``AccessLinkGeneric_def`` in ``icl.g4`` is a
*lexer* rule, so an ordinarily formatted ``AccessLink X Of STD_1149_1_2001 {`` header lexes as one
token and fails to parse -- the same happens to every published example. The tool's own grammar
comment (icl.g4:393-394) predicts it. The header's whitespace is therefore rewritten to ``/**/``
before parsing, which makes the lexer take the intended tokens; nothing else is altered.
``test_ordinary_formatting_is_not_parseable_by_the_vendored_lexer`` pins the limitation: if it
starts failing, the vendored tool was fixed and the workaround (and the matching notes in
``icl_emit``'s docstrings) can go.

**What this proves and doesn't.** It shows the emitted block is accepted by an independently
authored grammar for IEEE 1687's ``accessLink1149_def``, and -- via the controls below -- that the
harness rejects blocks that break the grammar (no ``BSDLEntity``, no instruction block, an empty
instruction body, a three-part instance path). It does not check meaning (that the instruction
exists in the BSDL file, see ``test_icl_bsdl_agreement.py``) or the normative IEEE text (paywalled,
not read). The control blocks below follow the published examples' *shape* (the IJTAG benchmark
set's ``MultiCoreAccessLink.icl``/``E30.icl``, MAST's test files) but are written for this test.
"""

from __future__ import annotations

import re
from typing import List, Tuple

import pytest
from test_icl_emit_golden import CASES

from warptap.icl_emit import to_icl

_HEADER = re.compile(r"AccessLink (\w+) Of (STD_1149_1_200[13]) \{")

# case name -> the chain's first slot, which the emitted ScanInterface list names
_FIRST_SLOT = {
    "flat_read_write": "warptap_sib_sensor",
    "nested_sib": "warptap_sib_outer",
    "scan_mux": "warptap_scan_mux_mux_outer",
}


def _lexable(text: str) -> str:
    """Rewrite the AccessLink header's whitespace to ``/**/`` (see the module docstring)."""
    return _HEADER.sub(r"AccessLink/**/\1/**/Of/**/\2/**/{", text)


@pytest.fixture
def parse(icl_parser_module):
    """``parse(text) -> (syntax_errors, consumed_all_input, access_links)`` where each access link
    is ``(name, entity, [(instruction, [scan_interface, ...]), ...])``."""
    from antlr4 import CommonTokenStream, InputStream, ParseTreeWalker, Token
    from antlr4.error.ErrorListener import ErrorListener
    from src.icl_parser.iclLexer import iclLexer
    from src.icl_parser.iclListener import iclListener
    from src.icl_parser.iclParser import iclParser

    class Errors(ErrorListener):
        def __init__(self):
            super().__init__()
            self.messages: List[str] = []

        def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
            self.messages.append(f"{line}:{column} {msg}")

    class Links(iclListener):
        def __init__(self):
            self.found: List[Tuple[str, str, list]] = []

        def enterAccessLink1149_def(self, ctx):
            instructions = []
            for ref in ctx.bsdl_instr_ref():
                interfaces: List[str] = []
                for item in ref.bsdl_instr_selected_item():
                    interfaces += [s.getText() for s in item.accessLink1149_ScanInterface_name()]
                instructions.append((ref.bsdl_instr_name().getText(), interfaces))
            self.found.append(
                (ctx.accessLink_name().getText(), ctx.bsdlEntity_name().getText(), instructions)
            )

    def run(text: str):
        errors = Errors()
        lexer = iclLexer(InputStream(text))
        lexer.removeErrorListeners()
        lexer.addErrorListener(errors)
        parser = iclParser(CommonTokenStream(lexer))
        parser.removeErrorListeners()
        parser.addErrorListener(errors)
        tree = parser.icl_source()
        consumed = parser.getCurrentToken().type == Token.EOF
        links = Links()
        ParseTreeWalker.DEFAULT.walk(links, tree)
        return errors.messages, consumed, links.found

    return run


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("entity", [None, "chip_tap"])
@pytest.mark.parametrize("ijtag_access", [False, True])
def test_emitted_access_link_parses_and_names_the_expected_things(
    case, entity, ijtag_access, parse
):
    graph, root = CASES[case]()
    text = to_icl(
        graph, root, include_access_link=True, bsdl_entity_name=entity, ijtag_access=ijtag_access
    )
    errors, consumed, links = parse(_lexable(text))
    assert errors == []
    assert consumed
    instruction = "IJTAG_ACCESS" if ijtag_access else "EXTEST"
    assert links == [
        ("warptap_tap", entity or root.name, [(instruction, [_FIRST_SLOT[case]])]),
    ]


def test_ordinary_formatting_is_not_parseable_by_the_vendored_lexer(parse):
    graph, root = CASES["flat_read_write"]()
    errors, consumed, _ = parse(to_icl(graph, root, include_access_link=True))
    assert errors or not consumed, (
        "the vendored lexer now accepts an ordinarily formatted AccessLink: the icl.g4 lexer-rule "
        "problem is fixed, so drop the /**/ workaround here and update icl_emit's docstrings"
    )


def _module(access_link_body: str) -> str:
    return (
        "Module Leaf { ScanInPort SI; ScanOutPort SO { Source SI; } ScanInterface scan_client "
        "{ Port SI; Port SO; } }\n"
        "Module Top {\n"
        "    ScanInPort tdi;\n"
        "    ScanOutPort tdo { Source tdi; }\n"
        "    TCKPort tck;\n"
        "    TMSPort tms;\n"
        "    TRSTPort trst_n;\n"
        "    Instance I1 Of Leaf { }\n"
        "    Instance I2 Of Leaf { }\n"
        f"    {access_link_body}\n"
        "}\n"
    )


_LINK = "AccessLink/**/Tap/**/Of/**/STD_1149_1_2001/**/{ %s }"


def test_control_published_shape_with_two_instructions_and_active_signals_parses(parse):
    body = _LINK % (
        "BSDLEntity Chip; "
        "ijtag_en { ScanInterface { I1.scan_client; } ActiveSignals { en[1:0]; } } "
        "ijtag_en_2 { ScanInterface { I2.scan_client; I1; } }"
    )
    errors, consumed, links = parse(_module(body))
    assert errors == [] and consumed
    assert links == [
        (
            "Tap",
            "Chip",
            [("ijtag_en", ["I1.scan_client"]), ("ijtag_en_2", ["I2.scan_client", "I1"])],
        )
    ]


@pytest.mark.parametrize(
    ("what", "body"),
    [
        ("no BSDLEntity", _LINK % "ijtag_en { ScanInterface { I1; } }"),
        ("no instruction block", _LINK % "BSDLEntity Chip;"),
        ("empty instruction body", _LINK % "BSDLEntity Chip; ijtag_en { }"),
        ("three-part instance path", _LINK % "BSDLEntity Chip; ijtag_en { ScanInterface { a.b.c; } }"),
    ],
)
def test_control_the_harness_rejects_blocks_that_break_the_grammar(what, body, parse):
    errors, consumed, _ = parse(_module(body))
    assert errors or not consumed, f"the harness accepted an AccessLink with {what}"
