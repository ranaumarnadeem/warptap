from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest


def _default_yosys_command() -> str:
    if os.environ.get("WARPTAP_YOSYS_CMD"):
        return os.environ["WARPTAP_YOSYS_CMD"]
    if shutil.which("yosys"):
        return "yosys"
    # No system Yosys on PATH — fall back to the yowasp-yosys console-script
    # installed alongside this interpreter (e.g. in a dev venv), per
    # implementation_plan.md's note on using the WASM build for local dev/test.
    candidate = Path(sys.executable).with_name(
        "yowasp-yosys.exe" if os.name == "nt" else "yowasp-yosys"
    )
    if candidate.exists():
        return str(candidate)
    return "yosys"  # let it fail loudly with a clear "not found" error


@pytest.fixture(scope="session")
def yosys_command() -> str:
    return _default_yosys_command()


def _default_tool_command(env_var: str, tool_name: str) -> str | None:
    if os.environ.get(env_var):
        return os.environ[env_var]
    if shutil.which(tool_name):
        return tool_name
    return None  # unlike Yosys, no WASM fallback exists for Icarus Verilog


@pytest.fixture(scope="session")
def iverilog_command() -> str:
    cmd = _default_tool_command("WARPTAP_IVERILOG_CMD", "iverilog")
    if cmd is None:
        pytest.skip("iverilog not found on PATH and WARPTAP_IVERILOG_CMD not set")
    return cmd


@pytest.fixture(scope="session")
def vvp_command() -> str:
    cmd = _default_tool_command("WARPTAP_VVP_CMD", "vvp")
    if cmd is None:
        pytest.skip("vvp not found on PATH and WARPTAP_VVP_CMD not set")
    return cmd


@pytest.fixture(scope="session")
def openocd_command() -> str:
    cmd = _default_tool_command("WARPTAP_OPENOCD_CMD", "openocd")
    if cmd is None:
        pytest.skip("openocd not found on PATH and WARPTAP_OPENOCD_CMD not set")
    return cmd


@pytest.fixture(scope="session")
def jam_command() -> str:
    """Deliberately env-var-only, no PATH fallback (unlike every other *_command fixture
    here) -- "jam" collides with the name of an unrelated, real, historically-common
    Perforce/Jam build tool, and a bare PATH lookup risks silently running the wrong
    program. See jamplayer_check.py's module docstring for how to build a real one."""
    cmd = os.environ.get("WARPTAP_JAM_CMD")
    if cmd is None:
        pytest.skip(
            "WARPTAP_JAM_CMD not set -- see jamplayer_check.py's module docstring for how "
            "to build a real Jam STAPL Player"
        )
    return cmd


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def _default_openmbist_dir() -> Path:
    if os.environ.get("WARPTAP_OPENMBIST_DIR"):
        return Path(os.environ["WARPTAP_OPENMBIST_DIR"])
    # tests/conftest.py -> tests -> tapestry -> tapestry's own parent: openMBIST is a sibling
    # checkout, not nested inside this repo (implementation_plan.md §7 Stage 9 §5.2).
    return Path(__file__).resolve().parents[2] / "openMBIST"


@pytest.fixture(scope="session")
def openmbist_dir() -> Path:
    """The real openMBIST checkout this project cross-simulates Stage 9's functional
    instruments against -- a sibling project, never vendored into tapestry (implementation_
    plan.md §7 Stage 9 §5.2's own licensing note: read live from the sibling checkout at test
    time). Skips (not fails) when not found, mirroring iverilog_command/vvp_command's own
    "optional external dependency" discipline."""
    candidate = _default_openmbist_dir()
    if not candidate.is_dir():
        pytest.skip(f"openMBIST checkout not found at {candidate} (set WARPTAP_OPENMBIST_DIR)")
    return candidate


@pytest.fixture(scope="session")
def autombist_generator(openmbist_dir: Path):
    """``autombist.generator.generate_from_config`` imported live from the sibling openMBIST
    checkout's own ``src/`` tree (confirmed pure Python + Jinja2, no subprocess/WSL/cocotb
    needed) -- same skip-if-not-importable guard as ``openmbist_dir``, not a hard failure,
    since a missing/incompatible sibling checkout is an environment gap, not a warptap bug."""
    src_dir = str(openmbist_dir / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from autombist.generator import generate_from_config
    except ImportError as exc:
        pytest.skip(f"autombist.generator not importable from {src_dir}: {exc}")
    return generate_from_config


def _default_icl_parser_dir() -> Path:
    if os.environ.get("WARPTAP_ICL_PARSER_DIR"):
        return Path(os.environ["WARPTAP_ICL_PARSER_DIR"])
    # tests/conftest.py -> tests -> tapestry -> third_party/icl_parser (a git submodule
    # checked out inside this repo, unlike openmbist_dir's sibling-checkout convention --
    # icl_parser is vendored, per implementation_plan.md §7 Stage 10's own reasoning: it has
    # no setup.py/pyproject.toml, so pip install isn't available, and there's no established
    # precedent here for silently copying external source with no update mechanism).
    return Path(__file__).resolve().parents[1] / "third_party" / "icl_parser"


@pytest.fixture(scope="session")
def icl_parser_dir() -> Path:
    candidate = _default_icl_parser_dir()
    if not candidate.is_dir() or not any(candidate.iterdir()):
        pytest.skip(
            f"third_party/icl_parser not checked out at {candidate} -- run "
            "`git submodule update --init third_party/icl_parser`"
        )
    return candidate


@pytest.fixture(scope="session")
def icl_parser_module(icl_parser_dir: Path):
    """``Ijtag`` (the vendored ``Honza255/icl_parser``'s own public API class) imported live
    from ``third_party/icl_parser`` -- MIT-licensed, git-submodule-vendored (see
    ``icl_parser_dir``), not pip-installable. Requires ``antlr4-python3-runtime`` (pinned to
    4.7.2, matching the exact version the submodule's checked-in generated lexer/parser were
    produced by -- ANTLR-generated code is not reliably forward/backward compatible across
    runtime versions), ``z3-solver``, ``sympy``, and ``networkx`` -- dev/test only, never a
    runtime dependency of warptap itself (``pyproject.toml``'s own ``dependencies = []`` stays
    empty). Skips (not fails) when the submodule isn't checked out or any of those aren't
    importable, matching every other external-tool fixture's "optional dependency" discipline
    in this file."""
    src_dir = str(icl_parser_dir)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from src.ijtag import Ijtag
    except ImportError as exc:
        pytest.skip(
            f"icl_parser not importable from {src_dir}: {exc} -- run `git submodule update "
            "--init third_party/icl_parser` and `pip install antlr4-python3-runtime==4.7.2 "
            "z3-solver networkx` (sympy is a warptap dependency already)"
        )
    return Ijtag


@pytest.fixture(scope="session")
def pdl_parser_module(icl_parser_dir: Path):
    """A ``parse_pdl(text) -> list[str]`` callable backed by a real, independently-authored PDL
    grammar (``third_party/icl_parser/src/pdl_parser/pdl.g4``, a "PDL0 grammar v20130806") --
    compiled here via ANTLR 4.7.2 (matching the same pinned version ``icl_parser_module`` uses)
    after fixing several real bugs in the grammar file itself (an unterminated rule, an
    undefined rule reference, two rule names -- ``range``/``format`` -- colliding with Python
    builtins under the Python3 target, a missing ``WS`` token before ``-sck``'s own port
    operand) and adding one new rule (see below) -- see ``pdl_emit.py``'s own module docstring
    for the full story, including the real ``pdl_emit.py`` bug this compiled grammar caught
    (``-sck`` rendered with no port name, which the real grammar requires).

    Entry point is a new ``flat_commands : commands EOF ;`` rule (added here, not part of the
    original grammar) rather than ``commands`` directly: invoking ``commands`` (a bare
    ``command*``) as an ANTLR entry point does NOT require it to consume the whole input --
    zero repetitions is a legal match, so text starting with anything ``command`` doesn't
    recognize silently "succeeds" with zero reported errors while leaving everything
    unconsumed. Confirmed directly (not assumed): the original ``pdl_parser_module`` design
    used bare ``commands`` and reported zero errors for both genuine garbage AND, more
    importantly, for every ``iTarget``-prefixed sequence ``to_pdl()`` actually emits -- because
    **this specific "PDL0" grammar has no ``iTarget``/scoping construct at all** (confirmed:
    absent from the ``keyword`` list, absent from ``command``'s own alternatives, absent
    anywhere in the file) -- an older PDL dialect than the one this project's own earlier
    research found ``iTarget`` in. ``flat_commands`` makes that failure loud (a real
    "mismatched input ``'iTarget'`` expecting ``<EOF>``" error) instead of silent.

    **Consequence for callers**: this grammar can validate each individual statement's own
    inner syntax (``iWrite``/``iRead``/``iApply``/``iRunLoop``, values, hex formatting, the
    ``-sck``/``-tck`` flags) but NOT a `to_pdl()`-emitted sequence as a whole, since real
    output always opens with ``iTarget``. Callers validating real ``to_pdl()`` output need to
    strip ``iTarget`` lines first (see ``test_pdl_emit_grammar_validation.py``'s own
    ``_strip_itarget_lines`` helper) and treat ``iTarget``'s own syntax as a confirmed,
    permanent gap in this specific bundled dialect -- not silently unchecked.

    Returns a list of ``"line L:C message"`` syntax-error strings (empty means a clean parse) --
    both lexer and parser errors are collected via a custom ``ErrorListener`` rather than
    letting ANTLR print to stderr, matching this project's other external-tool fixtures'
    "return something a test can assert on directly" convention. Requires
    ``antlr4-python3-runtime==4.7.2`` (the same dependency ``icl_parser_module`` already needs)
    -- skips (not fails) when the submodule isn't checked out or that package isn't importable."""
    src_dir = str(icl_parser_dir / "src" / "pdl_parser")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from antlr4 import CommonTokenStream, InputStream
        from antlr4.error.ErrorListener import ErrorListener
        from pdlLexer import pdlLexer
        from pdlParser import pdlParser
    except ImportError as exc:
        pytest.skip(
            f"pdl parser not importable from {src_dir}: {exc} -- run `git submodule update "
            "--init third_party/icl_parser` and `pip install antlr4-python3-runtime==4.7.2`"
        )

    class _CollectingErrorListener(ErrorListener):
        def __init__(self):
            super().__init__()
            self.errors: list[str] = []

        def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
            self.errors.append(f"line {line}:{column} {msg}")

    def parse_pdl(text: str) -> list[str]:
        errors = _CollectingErrorListener()
        lexer = pdlLexer(InputStream(text))
        lexer.removeErrorListeners()
        lexer.addErrorListener(errors)
        parser = pdlParser(CommonTokenStream(lexer))
        parser.removeErrorListeners()
        parser.addErrorListener(errors)
        parser.flat_commands()
        return errors.errors

    return parse_pdl


@pytest.fixture(scope="session")
def semiate_stil_parser():
    """``STILParser`` (``Semi-ATE-STIL``'s own public API class, pip-installable unlike
    ``icl_parser`` -- implementation_plan.md §7 Stage 13) -- a real, independent, Lark-based
    STIL syntax+semantic checker, despite its own README's "not yet ready for production"
    disclaimer (confirmed usable this session against real STIL text, including the
    multi-``WaveformTable`` mechanism :mod:`warptap.tap_ir_stil` depends on). Requires ``lark``
    -- not declared as an install dependency by the package itself, so genuinely optional here
    too. Dev/test only, never a runtime dependency of warptap itself. Skips (not fails) when
    either package isn't importable, matching ``icl_parser_module``'s own discipline.

    Real, empirically-confirmed usage note (not documented anywhere in the package itself):
    ``parser.parse_semantic()`` always returns ``None`` regardless of outcome -- check
    ``parser.is_parsing_done is True`` and ``parser.err_msg == ""`` for real success, after
    calling ``parser.parse_syntax()`` then ``parser.parse_semantic()`` in that order."""
    try:
        from Semi_ATE.STIL.parsers.STILParser import STILParser
    except ImportError as exc:
        pytest.skip(
            f"Semi-ATE-STIL/lark not importable: {exc} -- run `pip install Semi-ATE-STIL lark`"
        )
    return STILParser
