"""Third-party code warptap ships inside its own package.

``icl_parser`` is `Honza255/icl_parser <https://github.com/Honza255/icl_parser>`_ (MIT, its
license is in ``icl_parser/LICENSE``), copied from the fork in warptap's
``third_party/icl_parser`` submodule by ``scripts/vendor_icl_parser.py``; ``icl_parser/SOURCE``
names the commit. :func:`~warptap.icl_import.import_icl`,
:func:`~warptap.icl_import.import_one_hot_data_group` and :func:`~warptap.pdl_import.import_pdl`
parse with it unless given other parser classes. It needs ANTLR's Python runtime, which the
``import`` extra installs: ``pip install "warptap[import]"``. Parsing needs nothing else; the
parser's own retargeting model (z3, sympy, networkx) is never built by warptap's importers.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util

#: The ANTLR release the vendored lexers and parsers were generated with. ANTLR-generated code
#: needs a runtime of the same minor version.
ANTLR_VERSION = "4.13.2"


def _require_antlr(feature: str) -> None:
    """Raise :class:`ImportError` saying how to fix it when the ANTLR runtime is missing or has
    a different minor version than the vendored parsers were generated with."""
    if importlib.util.find_spec("antlr4") is None:
        raise ImportError(
            f"{feature} needs ANTLR's Python runtime, which a plain install of warptap leaves "
            'out: pip install "warptap[import]"'
        )
    try:
        installed = importlib.metadata.version("antlr4-python3-runtime")
    except importlib.metadata.PackageNotFoundError:
        return  # an antlr4 module not installed as that distribution; let it try
    if installed.split(".")[:2] != ANTLR_VERSION.split(".")[:2]:
        wanted = ".".join(ANTLR_VERSION.split(".")[:2])
        raise ImportError(
            f"{feature} needs antlr4-python3-runtime {wanted}.x, the version its parser was "
            f"generated with (ANTLR {ANTLR_VERSION}), but {installed} is installed: "
            'pip install "warptap[import]"'
        )


def ijtag(feature: str = "import_icl"):
    """The vendored parser's ``Ijtag`` class."""
    _require_antlr(feature)
    from warptap._vendor.icl_parser.ijtag import Ijtag

    return Ijtag


def pdl_lexer_and_parser(feature: str = "import_pdl"):
    """The vendored PDL grammar's generated ``pdlLexer`` and ``pdlParser`` classes."""
    _require_antlr(feature)
    from warptap._vendor.icl_parser.pdl_parser.pdlLexer import pdlLexer
    from warptap._vendor.icl_parser.pdl_parser.pdlParser import pdlParser

    return pdlLexer, pdlParser
