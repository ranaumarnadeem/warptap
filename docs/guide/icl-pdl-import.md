# ICL / PDL import

`import_icl`/`import_pdl` read real `.icl`/`.pdl` text back into warptap's own model: the
"other direction" from `to_icl`/`to_pdl`. Both are narrower in scope than the emit side:

- `import_icl` recognizes **warptap's own canonical network shape** (a top module whose direct
  children are SIB/ScanMux instances chained `tdi` to `tdo`, each gating one instrument found
  via its own `fromSO` binding): arbitrary third-party ICL using a different idiom (a bare
  top-level `ScanMux ... SelectedBy`, IR-decoded DR-mux TAPs, generic instrument libraries) is
  rejected with a specific reason, not silently misparsed.
- `import_pdl` recognizes **only `to_pdl()`'s own output shape**: one statement per line, the
  five statement kinds it emits (`iTarget`/`iWrite`/`iRead`/`iApply`/`iRunLoop`). An arbitrary
  hand-authored `.pdl` file (multiple statements per line, real `iProc` definitions, etc.) is
  rejected, not misparsed.
- Neither round-trips `signal_bits`/`capture_value`: ICL only ever records those in a `//`
  comment, which any real parser discards, so an imported `InstrumentNode` always comes back
  with `signal_bits=()` and `capture_value=0` regardless of the original.

## Setup

Both functions parse through a real, independent, MIT-licensed parser
([`Honza255/icl_parser`](https://github.com/Honza255/icl_parser)) rather than a self-written
one: warptap never trusts its own reading of the ICL/PDL grammar. warptap ships a copy of it,
taken from the fork its source repository pins as a submodule (the fork adds the compiled PDL
parser `import_pdl` needs, which upstream lacks). The copy needs ANTLR's Python runtime, which
the `import` extra installs:

```bash
pip install "warptap[import]"
```

```python
from pathlib import Path

from warptap import import_icl, import_pdl, PDLInterpreter

graph, root = import_icl([Path("network.icl")], "top_module")

pdl = PDLInterpreter(graph, root)
import_pdl(pdl_text, pdl)
```

The runtime is `antlr4-python3-runtime` 4.13: the shipped lexers and parsers were generated
with ANTLR 4.13.2, and ANTLR-generated code needs a runtime of the same minor version. Without
it, or with another minor version, both functions raise `ImportError` saying what to install.
Nothing else is needed: the parser's z3-based retargeting model is never built by these
importers.

To parse with another build of the parser, such as a checkout of the fork, pass its classes:
`import_icl(..., icl_parser_module=Ijtag)`, and
`import_pdl(..., pdl_lexer_class=pdlLexer, pdl_parser_class=pdlParser)` (both or neither).
