"""warptap ships icl_parser inside its package.

The copy (``src/warptap/_vendor/icl_parser``) is taken from the ``third_party/icl_parser``
submodule by ``scripts/vendor_icl_parser.py``. These tests check that the copy hasn't drifted
from the submodule.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import warptap._vendor

_REPO = Path(__file__).resolve().parents[1]
_COPY = Path(warptap._vendor.__file__).resolve().parent / "icl_parser"


def _sync_script():
    spec = importlib.util.spec_from_file_location(
        "vendor_icl_parser", _REPO / "scripts" / "vendor_icl_parser.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
