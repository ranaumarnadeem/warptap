"""Refresh warptap's vendored copy of icl_parser from the ``third_party/icl_parser`` submodule.

warptap ships the parser inside its own package (``warptap/_vendor/icl_parser``) so that
``import_icl`` and ``import_pdl`` work after ``pip install "warptap[import]"``. The submodule
stays the source: fix the parser in the fork, move the submodule pointer, then run

    python scripts/vendor_icl_parser.py

``tests/test_vendored_icl_parser.py`` fails while the copy and the submodule differ.

The copy holds the submodule's ``src/`` package (the hand-written modules and the generated ICL
and PDL parsers), its license, and a ``SOURCE`` file naming the commit. It leaves out the
grammars, ANTLR's ``.interp``/``.tokens`` files and ``src/parser/``, a stale duplicate of the
generated ICL parser that nothing imports.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUBMODULE = REPO / "third_party" / "icl_parser"
VENDORED = REPO / "src" / "warptap" / "_vendor" / "icl_parser"

_SUBPACKAGES = ("icl_parser", "pdl_parser")
FORK_URL = "https://github.com/ranaumarnadeem/icl_parser"
UPSTREAM_URL = "https://github.com/Honza255/icl_parser"


def vendored_files(submodule: Path = SUBMODULE) -> dict[str, Path]:
    """Each vendored file's path inside the copy, mapped to its source in ``submodule``."""
    src = submodule / "src"
    files = {p.name: p for p in sorted(src.glob("*.py"))}
    for sub in _SUBPACKAGES:
        for p in sorted((src / sub).glob("*.py")):
            files[f"{sub}/{p.name}"] = p
    files["LICENSE"] = submodule / "LICENSE"
    return files


def submodule_commit(submodule: Path = SUBMODULE) -> str:
    return subprocess.run(
        ["git", "-C", str(submodule), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def source_text(commit: str) -> str:
    return (
        "warptap's copy of icl_parser (MIT, see LICENSE in this directory).\n"
        f"Upstream: {UPSTREAM_URL}\n"
        f"Fork the copy is taken from: {FORK_URL}\n"
        f"Commit: {commit}\n"
        "Refreshed with scripts/vendor_icl_parser.py; do not edit these files here.\n"
    )


def main() -> None:
    files = vendored_files()
    if VENDORED.exists():
        shutil.rmtree(VENDORED)
    for rel, source in files.items():
        target = VENDORED / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (VENDORED / "SOURCE").write_text(source_text(submodule_commit()), encoding="utf-8")
    print(f"copied {len(files)} files from {SUBMODULE} into {VENDORED}")


if __name__ == "__main__":
    main()
