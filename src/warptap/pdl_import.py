"""PDL import: replays real PDL text against a live :class:`~warptap.pdl_interpreter.
PDLInterpreter` (:mod:`warptap.pdl_emit`'s own natural companion -- the "other direction"
:mod:`warptap.icl_import` already plays for ICL, extended to PDL now that a real, compiled PDL
grammar exists -- see :mod:`warptap.pdl_emit`'s own module docstring for how that grammar went
from an unwired, never-compiled artifact to a real parser).

**Structurally different from :mod:`warptap.icl_import`'s own shape, and deliberately so**:
ICL is declarative (a topology to reconstruct into a fresh :class:`~warptap.icl_model.
PhysicalGraph`/:class:`~warptap.icl_model.ModuleInstance`); PDL is procedural (a program to
*execute*). So this doesn't build new objects the way ``import_icl`` does -- it drives an
already-constructed :class:`~warptap.pdl_interpreter.PDLInterpreter`'s own public ``iTarget``/
``iWrite``/``iRead``/``iApply``/``iRunLoop`` methods, in call order, exactly as if the caller
had typed those calls directly. That naturally populates the interpreter's own ``history``
(comparable against the original for a round trip) and ``program`` (real, usable ``tap_ir``
ops) as a side effect, with no separate reconstruction step needed for either.

**``iTarget`` is hand-recognized, not grammar-parsed** -- confirmed empirically (not assumed):
the vendored, bundled PDL grammar (``third_party/icl_parser/src/pdl_parser/pdl.g4``, compiled
this session) is a real "PDL0" dialect with no ``iTarget``/scoping construct at all (absent
from its own ``keyword`` list, absent from every ``command`` alternative, absent anywhere in
the file) -- an older dialect than the one this project's own earlier research found
``iTarget`` in (``implementation_plan.md``'s own §3.2/§9). Every *other* statement kind
(``iWrite``/``iRead``/``iApply``/``iRunLoop``) is genuinely grammar-parsed here, walking the
real compiled ANTLR parse tree via its own generated context accessors, never regexed --
``iTarget`` is the one necessary exception, recognized by a small, tightly-scoped pattern
matching exactly what :func:`~warptap.pdl_emit.to_pdl` itself ever renders for it
(``iTarget <dotted>;``, one bare name, no real grammar construct to defer to).

**v1 scope: the 5 statement kinds ``to_pdl()`` actually emits** -- ``iTarget``, ``iWrite``,
``iRead``, ``iApply``, ``iRunLoop``. Everything else the real grammar recognizes (``iProc``/
``iCall``, ``iOverrideScanInterface``, ``iTake``/``iRelease``/``iMerge``, ``iClock``/
``iClockOverride``, ``iState``, ``iGetReadData``/``iGetMiscompares``/``iGetStatus``/
``iSetFail``, ``iScan``, ``iNote``) is rejected with a specific, named :class:`PdlImportError`
-- matching :mod:`warptap.icl_import`'s own "reject what's outside scope, don't misparse it"
discipline, never silently ignored. Within the five supported kinds, a sub-form
:class:`~warptap.pdl_interpreter.PDLInterpreter` itself has no equivalent for is rejected the
same way: ``iApply -together`` (no batched-apply concept here), ``iRunLoop -time`` (cycle
counts only, no wall-clock timing), and an enum-named or omitted ``iWrite``/``iRead`` value
(``to_pdl()`` only ever emits a numeric hex literal).

**A real, stated scope limitation**: this parses one statement per line, matching
``to_pdl()``'s own always-one-statement-per-line rendering convention exactly -- not the full
``pdl_source`` grammar (multiple statements per line, a statement spanning multiple lines,
free-standing comments). This imports what ``to_pdl()`` emits; it is not a general-purpose
parser for arbitrary, hand-authored ``.pdl`` files.
"""

from __future__ import annotations

import re

from warptap.errors import WarptapError
from warptap.pdl_interpreter import PDLInterpreter

_ITARGET_RE = re.compile(r"^iTarget\s+(\S+)\s*;$")


class PdlImportError(WarptapError):
    """Raised for a PDL statement outside this importer's v1 scope (see module docstring for
    the exact list), or when the compiled PDL grammar itself rejects a line as invalid syntax
    -- rather than silently misinterpreting or dropping it."""


def _number_value(ctx, stmt_text: str) -> int:
    """The int value of an ``iwrite_def``/``iread_def``'s own ``pdl_number`` -- raises for an
    ``enum_name`` value or a missing one entirely, neither of which ``to_pdl()`` ever emits
    (v1 scope, see module docstring). ``int(text, 0)`` auto-senses ``to_pdl()``'s own
    ``0x``-hex rendering (confirmed compatible with the grammar's own ``TCL_HEX_NUMBER`` token
    in ``pdl_emit.py``'s own module docstring) as well as plain decimal, needed for nothing
    ``to_pdl()`` emits today but free to support."""
    number = ctx.pdl_number()
    if number is None:
        raise PdlImportError(
            f"{stmt_text!r} has no numeric value (an enum-named or omitted value isn't "
            "supported -- v1 scope: to_pdl() never emits either)"
        )
    return int(number.getText(), 0)


def _replay_command(cmd_ctx, interp: PDLInterpreter, current_target) -> None:
    """One already-grammar-parsed, non-``iTarget`` ``CommandContext`` -> exactly one call
    against ``interp``'s own public API. Exactly one of ``cmd_ctx``'s own ``iXxx_def()``
    accessors is non-``None`` (confirmed empirically: ANTLR's own alternative-selection for
    the ``command`` rule), so this dispatches on whichever one is populated rather than
    inspecting raw text."""
    stmt_text = cmd_ctx.getText()

    write = cmd_ctx.iwrite_def()
    if write is not None:
        field = write.reg_or_port().getText()
        value = _number_value(write, stmt_text)
        if field == current_target:
            interp.iWrite(value)
        else:
            interp.iWrite(value, field=field)
        return

    read = cmd_ctx.iread_def()
    if read is not None:
        field = read.reg_or_port().getText()
        expected = _number_value(read, stmt_text)
        if field == current_target:
            interp.iRead(expected)
        else:
            interp.iRead(expected, field=field)
        return

    apply_ = cmd_ctx.iapply_def()
    if apply_ is not None:
        if "-together" in apply_.getText():
            raise PdlImportError(
                f"{stmt_text!r}: iApply -together isn't supported -- PDLInterpreter has no "
                "batched-apply concept (v1 scope)"
            )
        interp.iApply()
        return

    runloop = cmd_ctx.irunloop_def()
    if runloop is not None:
        cycle_count = runloop.cycleCount()
        if cycle_count is None:
            raise PdlImportError(
                f"{stmt_text!r}: iRunLoop -time isn't supported -- PDLInterpreter only "
                "accepts a cycle count, never wall-clock timing (v1 scope)"
            )
        count = int(cycle_count.getText(), 0)
        port_ctx = runloop.port()
        if port_ctx is None:
            interp.iRunLoop(count)
        else:
            interp.iRunLoop(count, sck_port=port_ctx.getText())
        return

    raise PdlImportError(
        f"{stmt_text!r} is a real PDL statement this importer doesn't recognize -- outside "
        "v1 scope (see module docstring for the exact list of what's supported)"
    )


def import_pdl(pdl_text: str, interp: PDLInterpreter, *, pdl_lexer_class, pdl_parser_class) -> None:
    """Replay ``pdl_text`` against ``interp`` (an already-constructed
    :class:`~warptap.pdl_interpreter.PDLInterpreter` over the caller's own graph/root), one
    line at a time, exactly as if each statement had been typed as a direct method call.
    ``pdl_lexer_class``/``pdl_parser_class`` are the compiled grammar's own ``pdlLexer``/
    ``pdlParser`` classes, injected the same way :func:`~warptap.icl_import.import_icl`
    accepts its own ``icl_parser_module`` -- see ``tests/conftest.py`` for how a caller/test
    imports them from the vendored submodule.

    Raises :class:`PdlImportError` for anything outside v1 scope (see module docstring) or a
    line the compiled grammar itself rejects as invalid PDL syntax; lets any error from
    ``interp``'s own methods (e.g. :class:`~warptap.icl_model.ICLAddressError` for an unknown
    ``iTarget``) propagate unwrapped, matching this project's own "let a lower layer's own
    precisely-named exception through rather than re-wrapping it into something vaguer"
    convention (e.g. ``pdl_interpreter.iTarget`` itself already does this)."""
    from antlr4 import CommonTokenStream, InputStream
    from antlr4.error.ErrorListener import ErrorListener

    class _CollectingErrorListener(ErrorListener):
        def __init__(self):
            super().__init__()
            self.errors: list[str] = []

        def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
            self.errors.append(f"line {line}:{column} {msg}")

    current_target = None
    for raw_line in pdl_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        itarget_match = _ITARGET_RE.match(line)
        if itarget_match is not None:
            current_target = itarget_match.group(1)
            interp.iTarget(current_target)
            continue

        errors = _CollectingErrorListener()
        lexer = pdl_lexer_class(InputStream(line))
        lexer.removeErrorListeners()
        lexer.addErrorListener(errors)
        parser = pdl_parser_class(CommonTokenStream(lexer))
        parser.removeErrorListeners()
        parser.addErrorListener(errors)
        tree = parser.flat_commands()
        if errors.errors:
            raise PdlImportError(
                f"{line!r} is not valid PDL syntax: {'; '.join(errors.errors)}"
            )

        commands = tree.commands()
        real_commands = [
            commands.getChild(i)
            for i in range(commands.getChildCount())
            if commands.getChild(i).getText().strip()
        ]
        if len(real_commands) != 1:
            raise PdlImportError(
                f"{line!r} must be exactly one PDL statement (v1 scope: one statement per "
                f"line, matching to_pdl()'s own rendering) -- found {len(real_commands)}"
            )
        _replay_command(real_commands[0], interp, current_target)
