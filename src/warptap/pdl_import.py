"""PDL import: replays PDL text against a live :class:`~warptap.pdl_interpreter.
PDLInterpreter` (:mod:`warptap.pdl_emit`'s companion, the direction :mod:`warptap.icl_import`
plays for ICL).

**Structurally different from :mod:`warptap.icl_import`, and deliberately so**: ICL is
declarative (a topology to reconstruct); PDL is procedural (a program to *execute*). So this
builds nothing: it drives an already-constructed :class:`~warptap.pdl_interpreter.
PDLInterpreter`'s public ``iTarget``/``iWrite``/``iRead``/``iApply``/``iRunLoop`` methods, in
call order, as if the caller had made those calls. That fills the interpreter's ``history``
(comparable against the original for a round trip) and ``program`` (``tap_ir`` ops) as it goes.

**Read with the grammar's own rules, the whole text at once.** The vendored grammar
(``third_party/icl_parser/src/pdl_parser/pdl.g4``) has no rule for a text that mixes ``iProc``
definitions with commands (``pdl_source`` holds only definitions, ``flat_commands`` only
commands), so the importer takes the text one top-level item at a time from one token stream:
an ``iproc_def`` where it starts with ``iProc``, a ``command`` everywhere else. A ``;`` or a line
end ends a statement (the grammar's ``eoc``), so a line can hold several, and a statement can't
span lines.

**iTarget**: the grammar has no scoping construct (:mod:`warptap.pdl_emit`'s module docstring
covers this). Its ``iPrefix`` rule has the same syntax, a keyword and an instance path, so the
importer lexes ``iTarget`` as that keyword and tells the two apart by the token's text. Keywords
aren't reserved in this grammar (``scalar_id`` includes them), so ``iTarget`` used as a name
anywhere else still reads as one.

**What is read**:

* ``iTarget``, ``iWrite``, ``iRead``, ``iApply``, ``iRunLoop``: the statements
  :func:`~warptap.pdl_emit.to_pdl` writes, each replayed as the interpreter call of that name;
* ``iNote``: accepted and ignored. It is a message for the test log, which the interpreter has
  no place for, and it changes nothing that is shifted;
* ``iProc`` and ``iCall``: a definition records the proc where it appears (so it must come before
  any call to it); a call binds the proc's arguments in order, a missing one taking the default
  its definition gives (``{name default}``), and runs the proc's commands with each ``$name``
  replaced by the value bound to it. Calls can nest, up to :data:`MAX_CALL_DEPTH` deep.

**Everything else is rejected** with a :class:`PdlImportError` naming it, never skipped: the
other commands (``iPrefix``, ``iReset``, ``iScan``, ``iOverrideScanInterface``, ``iTake``,
``iRelease``, ``iMerge``, ``iClock``, ``iClockOverride``, ``iState``, ``iUseProcNameSpace``), the
other top-level definitions (``iPDLLevel``, ``iProcsForModule``), an ``iCall`` on an instance
path or in a namespace (the interpreter's names aren't scoped by instance), and the sub-forms
the interpreter has no equivalent for: ``iApply -together`` (no batched apply), ``iRunLoop -time``
(cycle counts only), and an ``iWrite``/``iRead`` value that is an enum name, missing, or has
``x`` bits.
"""

from __future__ import annotations

from typing import NamedTuple

from warptap import _vendor
from warptap.errors import WarptapError
from warptap.pdl_interpreter import PDLInterpreter

MAX_CALL_DEPTH = 64
"""How deep ``iCall`` may nest before :func:`import_pdl` gives up on the text as recursive."""

# Top-level definitions the grammar's pdl_source allows that the importer doesn't read.
_UNSUPPORTED_DEFINITIONS = ("iPDLLevel", "iProcsForModule")


class PdlImportError(WarptapError):
    """Raised for a PDL statement :func:`import_pdl` doesn't read (see the module docstring for
    the list), a call it can't bind, or text the grammar rejects, rather than skipping or
    misreading it."""


class _Proc(NamedTuple):
    name: str
    params: tuple  # (name, default text or None), in order
    body: object  # the iproc_def's CommandsContext


class _Replay:
    """Runs parsed commands against ``interp``, holding what the text has set up so far: the
    procs defined and the current ``iTarget``."""

    def __init__(self, interp: PDLInterpreter, parser_class):
        self.interp = interp
        self.parser_class = parser_class
        self.procs: dict = {}
        self.current_target = None

    # -- values ---------------------------------------------------------------------------

    @staticmethod
    def _bound(text: str, env: dict, stmt: str) -> str:
        """``text``, or the value bound to it if it is an argument reference (``$name``)."""
        if not text.startswith("$"):
            return text
        name = text[1:]
        if name not in env:
            where = "the iProc it is in" if env else "anything: it is outside any iProc"
            raise PdlImportError(f"{stmt!r}: ${name} is not an argument of {where}")
        return env[name]

    def _number(self, ctx, env: dict, stmt: str) -> int:
        text = self._bound(ctx.getText(), env, stmt)
        try:
            return int(text, 0)
        except ValueError:
            raise PdlImportError(
                f"{stmt!r}: {text!r} is not a number the interpreter can take (decimal, 0x hex "
                "or 0b binary, with no x bits)"
            ) from None

    def _value(self, ctx, env: dict, stmt: str) -> int:
        """An ``iWrite``/``iRead`` value: its ``pdl_number``, never an enum name or nothing."""
        number = ctx.pdl_number()
        if number is None:
            raise PdlImportError(
                f"{stmt!r} has no numeric value (an enum-named or omitted value isn't supported)"
            )
        return self._number(number, env, stmt)

    # -- definitions and calls ------------------------------------------------------------

    def define(self, ctx) -> None:
        name = ctx.procName().getText()
        params = []
        for argument in ctx.argument():
            with_default = argument.argWithDefault()
            if with_default is None:
                params.append((argument.scalar_id().getText(), None))
            else:
                default = with_default.args().getText()
                if default.startswith("$"):
                    raise PdlImportError(
                        f"iProc {name!r}: the default {default!r} refers to an argument; a "
                        "default must be a value"
                    )
                params.append((with_default.scalar_id().getText(), default))
        names = [p for p, _ in params]
        repeated = sorted({p for p in names if names.count(p) > 1})
        if repeated:
            raise PdlImportError(f"iProc {name!r} names argument(s) {repeated} more than once")
        self.procs[name] = _Proc(name, tuple(params), ctx.commands())

    def call(self, ctx, env: dict, depth: int, stmt: str) -> None:
        if ctx.instancePath() is not None or ctx.pdl_namespace_name() is not None:
            raise PdlImportError(
                f"{stmt!r}: iCall on an instance path or in a namespace isn't supported (the "
                "interpreter's names aren't scoped by instance)"
            )
        name = ctx.procName().getText()
        proc = self.procs.get(name)
        if proc is None:
            raise PdlImportError(f"{stmt!r}: no iProc named {name!r} is defined before this call")
        values = [self._bound(arg.getText(), env, stmt) for arg in ctx.args()]
        if len(values) > len(proc.params):
            raise PdlImportError(
                f"{stmt!r}: iProc {name!r} takes {len(proc.params)} argument(s), given "
                f"{len(values)}"
            )
        bound = {}
        for k, (param, default) in enumerate(proc.params):
            if k < len(values):
                bound[param] = values[k]
            elif default is not None:
                bound[param] = default
            else:
                raise PdlImportError(
                    f"{stmt!r}: no value for iProc {name!r}'s argument {param!r}, which has "
                    "no default"
                )
        if depth >= MAX_CALL_DEPTH:
            raise PdlImportError(
                f"{stmt!r}: iCall nests more than {MAX_CALL_DEPTH} deep (a proc that calls "
                "itself?)"
            )
        for command in proc.body.command():
            self.command(command, bound, depth + 1)

    # -- commands -------------------------------------------------------------------------

    def command(self, ctx, env: dict, depth: int) -> None:
        """One parsed ``command``: exactly one of its ``iXxx_def()`` accessors is set (or none,
        for an empty statement or a comment)."""
        stmt = ctx.getText().strip()

        prefix = ctx.iprefix_def()
        if prefix is not None:
            if prefix.start.text != "iTarget":
                raise PdlImportError(f"{stmt!r}: iPrefix isn't supported")
            if prefix.instancePath() is None:
                raise PdlImportError(f"{stmt!r}: iTarget needs an instance path")
            self.current_target = prefix.instancePath().getText()
            self.interp.iTarget(self.current_target)
            return

        write = ctx.iwrite_def()
        if write is not None:
            field = self._bound(write.reg_or_port().getText(), env, stmt)
            value = self._value(write, env, stmt)
            if field == self.current_target:
                self.interp.iWrite(value)
            else:
                self.interp.iWrite(value, field=field)
            return

        read = ctx.iread_def()
        if read is not None:
            field = self._bound(read.reg_or_port().getText(), env, stmt)
            expected = self._value(read, env, stmt)
            if field == self.current_target:
                self.interp.iRead(expected)
            else:
                self.interp.iRead(expected, field=field)
            return

        apply_ = ctx.iapply_def()
        if apply_ is not None:
            if apply_.getChildCount() > 1:  # 'iApply' WS '-together'
                raise PdlImportError(
                    f"{stmt!r}: iApply -together isn't supported (the interpreter has no "
                    "batched apply)"
                )
            self.interp.iApply()
            return

        runloop = ctx.irunloop_def()
        if runloop is not None:
            cycle_count = runloop.cycleCount()
            if cycle_count is None:
                raise PdlImportError(
                    f"{stmt!r}: iRunLoop -time isn't supported (the interpreter takes a cycle "
                    "count, never a time)"
                )
            count = self._number(cycle_count, env, stmt)
            port = runloop.port()
            if port is None:
                self.interp.iRunLoop(count)
            else:
                self.interp.iRunLoop(count, sck_port=self._bound(port.getText(), env, stmt))
            return

        call = ctx.icall_def()
        if call is not None:
            self.call(call, env, depth, stmt)
            return

        if ctx.inote_def() is not None:
            return  # a message for the log, nothing to replay

        from antlr4 import ParserRuleContext

        other = next(
            (
                child
                for child in ctx.getChildren()
                if isinstance(child, ParserRuleContext)
                and child.getRuleIndex() != self.parser_class.RULE_eoc
            ),
            None,
        )
        if other is not None:
            raise PdlImportError(
                f"{stmt!r}: {other.start.text} isn't supported (see pdl_import's module "
                "docstring for what is)"
            )
        # an empty statement or a comment


def _error_listener():
    """An ANTLR ``ErrorListener`` that collects syntax errors in ``.messages``. Made on demand:
    ANTLR is an optional dependency, imported only once a parse is asked for."""
    from antlr4.error.ErrorListener import ErrorListener

    class _Collecting(ErrorListener):
        def __init__(self):
            super().__init__()
            self.messages: list = []

        def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
            self.messages.append(f"line {line}:{column} {msg}")

    return _Collecting()


def _check(errors) -> None:
    if errors.messages:
        raise PdlImportError(
            f"the PDL text is not valid PDL syntax: {'; '.join(errors.messages)}"
        )


def import_pdl(
    pdl_text: str, interp: PDLInterpreter, *, pdl_lexer_class=None, pdl_parser_class=None
) -> None:
    """Replay ``pdl_text`` against ``interp`` (an already-constructed
    :class:`~warptap.pdl_interpreter.PDLInterpreter` over the caller's own graph/root), each
    statement as the interpreter call it stands for, in order. ``pdl_lexer_class``/
    ``pdl_parser_class`` are the compiled grammar's ``pdlLexer``/``pdlParser`` classes. Both
    default to the copy warptap ships (:mod:`warptap._vendor`, needs
    ``pip install "warptap[import]"``); pass both to parse with another build, as
    ``tests/conftest.py`` does with the submodule's.

    Raises :class:`PdlImportError` for anything the module docstring lists as not read, a call
    it can't bind, or text the grammar rejects; an error from ``interp``'s own methods (e.g.
    :class:`~warptap.icl_model.ICLAddressError` for an unknown ``iTarget``) propagates
    unwrapped. Statements before the one that fails have already been replayed."""
    if (pdl_lexer_class is None) != (pdl_parser_class is None):
        raise TypeError(
            "pass both pdl_lexer_class and pdl_parser_class, or neither: a lexer and parser "
            "from different builds of the grammar disagree on token types"
        )
    if pdl_lexer_class is None:
        pdl_lexer_class, pdl_parser_class = _vendor.pdl_lexer_and_parser("import_pdl")
    from antlr4 import CommonTokenStream, InputStream, Token

    if not pdl_text.endswith("\n"):
        pdl_text += "\n"  # the last statement ends at a line end too
    errors = _error_listener()
    lexer = pdl_lexer_class(InputStream(pdl_text))
    lexer.removeErrorListeners()
    lexer.addErrorListener(errors)
    stream = CommonTokenStream(lexer)
    stream.fill()
    _check(errors)

    literal = {name.strip("'"): kind for kind, name in enumerate(pdl_parser_class.literalNames)}
    for token in stream.tokens:
        if token.type == pdl_parser_class.SCALAR_ID and token.text == "iTarget":
            token.type = literal["iPrefix"]
    definitions = {literal[name]: name for name in _UNSUPPORTED_DEFINITIONS}

    parser = pdl_parser_class(stream)
    parser.removeErrorListeners()
    parser.addErrorListener(errors)
    replay = _Replay(interp, pdl_parser_class)
    while stream.LA(1) != Token.EOF:
        start = stream.index
        if stream.LA(1) == pdl_parser_class.WS and stream.LA(2) in (
            literal["iProc"], *definitions
        ):
            stream.consume()
        if stream.LA(1) in definitions:
            raise PdlImportError(f"{definitions[stream.LA(1)]} isn't supported")
        if stream.LA(1) == literal["iProc"]:
            definition = parser.iproc_def()
            _check(errors)
            replay.define(definition)
        else:
            command = parser.command()
            _check(errors)
            replay.command(command, {}, 0)
        if stream.index == start:  # nothing consumed: never loop on the same token
            raise PdlImportError(
                f"the PDL text is not valid PDL syntax at {stream.LT(1).text!r}"
            )
