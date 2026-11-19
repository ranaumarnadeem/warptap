"""PDL import of the constructs beyond what :func:`~warptap.pdl_emit.to_pdl` writes: several
statements per line, ``iNote``, ``iProc``/``iCall`` with arguments and defaults. Each is checked
against the same calls made on the interpreter directly, and each has a control: the near miss
that must still be rejected, with the reason pinned.
"""

from __future__ import annotations

import pytest

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.pdl_import import MAX_CALL_DEPTH, PdlImportError, import_pdl
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan


@pytest.fixture
def network():
    specs = [
        InstrumentSpec(
            "ctrl", width=4, capture_value=0, direction=InstrumentDirection.WRITE,
            signal_bits=tuple(SignalBinding("mode", bit) for bit in range(4)),
        ),
        InstrumentSpec("status", width=3, capture_value=0b101),
    ]
    return build_sib_plan(specs, top_name="chip")


@pytest.fixture
def replay(network, pdl_lexer_parser_classes):
    """``replay(text)``: a fresh interpreter over ``network`` with ``text`` imported into it."""
    lexer, parser = pdl_lexer_parser_classes

    def run(text: str) -> PDLInterpreter:
        interp = PDLInterpreter(*network)
        import_pdl(text, interp, pdl_lexer_class=lexer, pdl_parser_class=parser)
        return interp

    return run


@pytest.fixture
def direct(network):
    """The interpreter after the reference sequence, made by calling it directly."""
    interp = PDLInterpreter(*network)
    interp.iTarget("ctrl")
    interp.iWrite(0x9)
    interp.iApply()
    interp.iRunLoop(20)
    interp.iTarget("status")
    interp.iRead(0x5)
    interp.iApply()
    return interp


def _same(a: PDLInterpreter, b: PDLInterpreter) -> None:
    assert a.history == b.history
    assert a.program == b.program


ONE_PER_LINE = """iTarget ctrl;
iWrite ctrl 0x9;
iApply;
iRunLoop 20 -tck;
iTarget status;
iRead status 0x5;
iApply;
"""


def test_the_reference_sequence_one_statement_per_line(replay, direct):
    _same(replay(ONE_PER_LINE), direct)


def test_several_statements_on_one_line(replay, direct):
    text = "iTarget ctrl; iWrite ctrl 0x9; iApply; iRunLoop 20 -tck\niTarget status; iRead status 0x5; iApply\n"
    _same(replay(text), direct)


def test_statements_end_at_a_line_end_or_a_semicolon_not_at_a_space(replay):
    """Control: two statements need a ``;`` or a line end between them."""
    with pytest.raises(PdlImportError, match="not valid PDL syntax"):
        replay("iTarget ctrl; iWrite ctrl 0x9 iApply;\n")


def test_comments_and_notes_change_nothing(replay, direct):
    """``iNote`` is accepted and ignored; ``#`` comments and blank statements too."""
    text = (
        '# reference sequence\n'
        'iNote -comment "set the mode";\n'
        "iTarget ctrl; iWrite ctrl 0x9; iApply\n"
        '\n'
        'iNote -status "settling"\n'
        "iRunLoop 20 -tck;;\n"
        "iTarget status; iRead status 0x5; iApply; # done\n"
    )
    _same(replay(text), direct)


def test_control_inote_takes_one_of_its_two_flags(replay):
    with pytest.raises(PdlImportError, match="not valid PDL syntax"):
        replay('iNote -warning "no such flag";\n')


PROCS = """
iProc set_mode {value} {
    iTarget ctrl
    iWrite ctrl $value
    iApply
}
iProc settle {{cycles 20}} {
    iRunLoop $cycles -tck
}
iProc check {field {expected 0x5}} {
    iTarget status
    iRead $field $expected
    iApply
}
"""


def test_iproc_and_icall_bind_arguments_and_defaults(replay, direct):
    """``settle`` runs on its default, ``check`` binds its first argument and defaults the
    second, and an argument can stand for a name as well as a number."""
    _same(replay(PROCS + "iCall set_mode 0x9; iCall settle; iCall check status\n"), direct)


def test_an_argument_given_overrides_its_default(replay, network):
    interp = PDLInterpreter(*network)
    interp.iRunLoop(7)
    _same(replay(PROCS + "iCall settle 7\n"), interp)


def test_a_proc_can_call_another(replay, direct):
    text = PROCS + "iProc all {mode} {\n    iCall set_mode $mode\n    iCall settle\n    iCall check status\n}\niCall all 9\n"
    _same(replay(text), direct)


@pytest.mark.parametrize(
    "call, reason",
    [
        ("iCall set_mode", "no value for iProc 'set_mode''s argument 'value', which has no default"),
        ("iCall set_mode 1 2", "iProc 'set_mode' takes 1 argument\\(s\\), given 2"),
        ("iCall nothing_defined 1", "no iProc named 'nothing_defined' is defined before this call"),
        ("iCall chip.u1.set_mode 1", "iCall on an instance path or in a namespace isn't supported"),
        ("iCall set_mode mode_a", "'mode_a' is not a number the interpreter can take"),
    ],
)
def test_control_calls_that_do_not_bind(replay, call, reason):
    with pytest.raises(PdlImportError, match=reason):
        replay(PROCS + call + "\n")


def test_control_a_call_before_the_definition(replay):
    with pytest.raises(PdlImportError, match="no iProc named 'set_mode' is defined before this call"):
        replay("iCall set_mode 9\n" + PROCS)


def test_control_an_argument_reference_outside_any_proc(replay):
    with pytest.raises(PdlImportError, match="\\$value is not an argument of anything: it is outside any iProc"):
        replay("iTarget ctrl; iWrite ctrl $value\n")


def test_control_an_argument_the_proc_does_not_have(replay):
    text = "iProc bad {value} {\n    iTarget ctrl\n    iWrite ctrl $other\n}\niCall bad 1\n"
    with pytest.raises(PdlImportError, match="\\$other is not an argument of the iProc it is in"):
        replay(text)


def test_control_a_proc_that_calls_itself(replay):
    with pytest.raises(PdlImportError, match=f"iCall nests more than {MAX_CALL_DEPTH} deep"):
        replay("iProc loop {} {\n    iCall loop\n}\niCall loop\n")


def test_control_a_proc_naming_an_argument_twice(replay):
    with pytest.raises(PdlImportError, match="names argument\\(s\\) \\['a'\\] more than once"):
        replay("iProc twice {a a} {\n    iApply\n}\n")


@pytest.mark.parametrize(
    "text, reason",
    [
        ("iPrefix chip.u1;", "iPrefix isn't supported"),
        ("iReset;", "iReset isn't supported"),
        ("iTake ctrl;", "iTake isn't supported"),
        ("iClock sysclk;", "iClock isn't supported"),
        ("iPDLLevel 0 -version STD_1687_2014;", "iPDLLevel isn't supported"),
        ("iProcsForModule chip;", "iProcsForModule isn't supported"),
        ("iApply -together;", "iApply -together isn't supported"),
        # The grammar can't parse these two at all: its time suffixes lex as names, and its
        # iUseProcNameSpace rule has no space after the keyword.
        ("iRunLoop -time 1.0us;", "not valid PDL syntax"),
        ("iUseProcNameSpace ns;", "not valid PDL syntax"),
    ],
)
def test_control_what_is_still_rejected(replay, text, reason):
    with pytest.raises(PdlImportError, match=reason):
        replay(text + "\n")


def test_itarget_is_still_an_ordinary_name_elsewhere(replay, network):
    """``iTarget`` is read as a command only where a command starts: as a proc's name it is
    just a name."""
    interp = PDLInterpreter(*network)
    interp.iTarget("ctrl")
    interp.iWrite(0x3)
    interp.iApply()
    _same(replay("iProc iTarget {iTarget} {\n    iTarget ctrl\n    iWrite ctrl $iTarget\n    iApply\n}\niCall iTarget 3\n"), interp)


def test_a_single_digit_zero_or_one_is_a_number(replay, network):
    """``0`` and ``1`` lex as tokens of their own in this grammar; they are still numbers."""
    interp = PDLInterpreter(*network)
    interp.iTarget("ctrl")
    interp.iWrite(1)
    interp.iApply()
    interp.iRunLoop(1)
    interp.iWrite(0)
    interp.iApply()
    _same(replay("iTarget ctrl; iWrite ctrl 1; iApply; iRunLoop 1 -tck; iWrite ctrl 0; iApply\n"), interp)
