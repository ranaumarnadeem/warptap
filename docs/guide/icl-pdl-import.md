# ICL / PDL import

`import_icl`/`import_pdl` read real `.icl`/`.pdl` text back into warptap's own model — the
"other direction" from `to_icl`/`to_pdl`. Both are narrower in scope than the emit side:

- `import_icl` recognizes **warptap's own canonical network shape** (a top module whose direct
  children are SIB/ScanMux instances chained `tdi` to `tdo`, each gating one instrument found
  via its own `fromSO` binding) — arbitrary third-party ICL using a different idiom (a bare
  top-level `ScanMux ... SelectedBy`, IR-decoded DR-mux TAPs, generic instrument libraries) is
  rejected with a specific reason, not silently misparsed.
- `import_pdl` recognizes **only `to_pdl()`'s own output shape** — one statement per line, the
  five statement kinds it emits (`iTarget`/`iWrite`/`iRead`/`iApply`/`iRunLoop`). An arbitrary
  hand-authored `.pdl` file (multiple statements per line, real `iProc` definitions, etc.) is
  rejected, not misparsed.
- Neither round-trips `signal_bits`/`capture_value` — ICL only ever records those in a `//`
  comment, which any real parser discards, so an imported `InstrumentNode` always comes back
  with `signal_bits=()` and `capture_value=0` regardless of the original.

## A real setup requirement, not just an import statement

Both functions parse through a real, independent, MIT-licensed parser
([`Honza255/icl_parser`](https://github.com/Honza255/icl_parser)) rather than a self-written
one — warptap never trusts its own reading of the ICL/PDL grammar. That parser is vendored as a
git submodule in warptap's own source repository, not published on PyPI, so it isn't installed
by a plain `pip install warptap`. To use `import_icl`/`import_pdl`, clone it yourself and inject
its classes:

```bash
git clone https://github.com/Honza255/icl_parser.git
pip install antlr4-python3-runtime==4.7.2 z3-solver sympy networkx
```

```python
import sys
sys.path.insert(0, "icl_parser")
from src.ijtag import Ijtag
sys.path.insert(0, "icl_parser/src/pdl_parser")
from pdlLexer import pdlLexer
from pdlParser import pdlParser

from warptap import import_icl, import_pdl, PDLInterpreter

graph, root = import_icl([Path("network.icl")], "top_module", icl_parser_module=Ijtag)

pdl = PDLInterpreter(graph, root)
import_pdl(pdl_text, pdl, pdl_lexer_class=pdlLexer, pdl_parser_class=pdlParser)
```

`antlr4-python3-runtime` is pinned to `4.7.2` — the vendored submodule's own checked-in
generated lexer/parser code was produced by that exact version, and ANTLR-generated code isn't
reliably compatible across runtime versions.
