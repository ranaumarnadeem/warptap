"""warptap ships icl_parser inside its package, and the importers parse with that copy.

The copy (``src/warptap/_vendor/icl_parser``) is taken from the ``third_party/icl_parser``
submodule by ``scripts/vendor_icl_parser.py``. These tests check that the copy hasn't drifted
from the submodule, that ``import_icl``/``import_pdl`` use it when given no parser, and that it
needs nothing but ANTLR's runtime, which is all the ``import`` extra installs.

The last two run in a separate Python process with chosen modules made unimportable
(``sys.modules[name] = None``, which both ``import`` and ``importlib.util.find_spec`` honor), so
they see what a plain ``pip install "warptap[import]"`` would leave available.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import warptap._vendor
from warptap.icl_emit import to_icl
from warptap.icl_import import import_icl
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.pdl_emit import to_pdl
from warptap.pdl_import import import_pdl
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan

_REPO = Path(__file__).resolve().parents[1]
_COPY = Path(warptap._vendor.__file__).resolve().parent / "icl_parser"


def _sync_script():
    spec = importlib.util.spec_from_file_location(
        "vendor_icl_parser", _REPO / "scripts" / "vendor_icl_parser.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _network():
    specs = [
        InstrumentSpec(
            "ctrl", width=2, capture_value=0, direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("bist_start", 0), SignalBinding("bist_start", 1)),
        ),
        InstrumentSpec(
            "status", width=1, capture_value=0, direction=InstrumentDirection.READ,
            signal_bits=(SignalBinding("bist_done"),),
        ),
    ]
    return build_sib_plan(specs, top_name="chip")


def _icl_file(tmp_path: Path) -> Path:
    graph, root = _network()
    path = tmp_path / "chip.icl"
    path.write_text(to_icl(graph, root, include_access_link=False), encoding="utf-8")
    return path


def test_the_copy_matches_the_submodule(icl_parser_dir: Path) -> None:
    expected = _sync_script().vendored_files(icl_parser_dir)
    present = {
        p.relative_to(_COPY).as_posix()
        for p in _COPY.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    } - {"SOURCE"}
    assert present == set(expected), "run: python scripts/vendor_icl_parser.py"
    differing = [rel for rel, src in expected.items() if (_COPY / rel).read_bytes() != src.read_bytes()]
    assert not differing, f"{differing} differ from the submodule; run: python scripts/vendor_icl_parser.py"


def test_the_copy_names_the_submodule_commit_it_came_from(icl_parser_dir: Path) -> None:
    sync = _sync_script()
    assert (_COPY / "SOURCE").read_text(encoding="utf-8") == sync.source_text(
        sync.submodule_commit(icl_parser_dir)
    )


def test_import_icl_parses_with_the_copy_when_given_no_parser(icl_parser_module, tmp_path) -> None:
    path = _icl_file(tmp_path)
    by_default = import_icl([path], "chip")
    assert "warptap._vendor.icl_parser.ijtag" in sys.modules
    assert by_default == import_icl([path], "chip", icl_parser_module=icl_parser_module)


def test_import_pdl_parses_with_the_copy_when_given_no_classes(pdl_lexer_parser_classes) -> None:
    graph, root = _network()
    original = PDLInterpreter(graph, root)
    original.iTarget("ctrl")
    original.iWrite(2)
    original.iApply()
    text = to_pdl(original.history, graph)

    by_default = PDLInterpreter(graph, root)
    import_pdl(text, by_default)
    assert "warptap._vendor.icl_parser.pdl_parser.pdlParser" in sys.modules

    with_submodule = PDLInterpreter(graph, root)
    lexer, parser = pdl_lexer_parser_classes
    import_pdl(text, with_submodule, pdl_lexer_class=lexer, pdl_parser_class=parser)
    assert by_default.history == with_submodule.history == original.history
    assert by_default.program == with_submodule.program == original.program


def test_import_pdl_takes_both_classes_or_neither(pdl_lexer_parser_classes) -> None:
    graph, root = _network()
    lexer, _ = pdl_lexer_parser_classes
    with pytest.raises(TypeError, match="both pdl_lexer_class and pdl_parser_class"):
        import_pdl("", PDLInterpreter(graph, root), pdl_lexer_class=lexer)


_RUN_WITHOUT = r"""
import sys
for name in sys.argv[1].split(","):
    sys.modules[name] = None
from pathlib import Path
from warptap.icl_import import import_icl
try:
    graph, _ = import_icl([Path(sys.argv[2])], "chip")
except ImportError as exc:
    print("ImportError:", exc)
else:
    print("imported", len(graph.chain), "slots")
"""


def _import_without(modules: str, path: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _RUN_WITHOUT, modules, str(path)],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def test_the_copy_needs_neither_z3_nor_sympy_nor_networkx(tmp_path) -> None:
    assert _import_without("z3,sympy,networkx", _icl_file(tmp_path)) == "imported 2 slots"


def test_a_missing_antlr_runtime_says_which_extra_to_install(tmp_path) -> None:
    last = _import_without("antlr4", _icl_file(tmp_path))
    assert last.startswith("ImportError: import_icl needs ANTLR's Python runtime")
    assert 'pip install "warptap[import]"' in last
