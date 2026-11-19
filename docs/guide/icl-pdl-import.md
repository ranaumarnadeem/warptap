# ICL / PDL import

`import_icl`/`import_pdl` read `.icl`/`.pdl` text back into warptap's model: the other direction
from `to_icl`/`to_pdl`.

## What `import_icl` reads

`import_icl(paths, top)` elaborates `top` from the given files and follows its scan path from the
top module's scan-out back to its scan-in. Ports are only wires to it, so module boundaries and
wrapper instances don't change what it sees, and it recognizes elements by their structure, not
by their names:

- **a SIB**: a 1-bit scan register fed by a 2-way ScanMux that the register itself selects, the
  `0` arm a straight bypass and the `1` arm the gated segment (the SIB_mux_pre form, which
  `to_icl` writes);
- **a ScanMux slot**: any other ScanMux whose output goes straight into its own select register.
  The register may sit in an instance of its own (a segment control register read through a
  `DataOutPort`); each arm is selected by one value, and arms that hold nothing are bypass;
- in a SIB's segment or a ScanMux arm: **one scan register** (an instrument), or a chain of
  further SIBs and ScanMux slots, to any depth.

Names are instance paths joined with `__` (`core.g1` becomes `core__g1`); a top-level instance
named with one of warptap's prefixes (`warptap_sib_` and the others) loses it, so warptap's own
ICL imports back to the names it was emitted from. A register is read from its scan-in end
whichever way it is declared (`[0:7]` or `[7:0]`). An instrument is WRITE when its module, or the
wrapper around it, holds a `DataRegister`, READ otherwise. The result is checked with
`validate_physical_graph`.

Anything else raises `IclImportError`, the message starting with the reason (all of them are in
`icl_import.REJECTION_REASONS`) and naming the element:

| Reason | What it means |
|---|---|
| `a scan register outside any SIB or ScanMux` | an always-on register: the model has no slot for one |
| `more than one scan register in one SIB segment or ScanMux arm` | registers in series behind one gate |
| `a SIB whose register comes before its ScanMux (SIB_mux_post)` | the other SIB form |
| `a SIB that opens on 0` | the segment on the `0` arm |
| `a ScanMux selected by a register that is not its own select register` | a select shared with another slot, or part of a register |
| `a ScanMux selected by something other than a scan register` | an IR decode, the TAP, a port, logic |
| `a ScanMux arm selected by more than one value` | one arm for several select values |
| `a ScanMux with fewer than two arms that hold anything` | |
| `a scan path that is not a tree` | a segment on two paths, or a cycle |
| `a scan path that leaves a scan register before its last bit` | |
| `no scan path from the top module's scan-in to its scan-out` | not one scan-in and one scan-out |
| `an ICL construct the vendored parser doesn't elaborate` | an `AccessLink`, `ClockMux`, `ToClockPort` with a `Source`, addressed instance or broadcast in the network; none is in the model either |
| `invalid ICL` | a syntax error, an unresolved name, a failed check, with the parser's message |

An `AccessLink` stops the import only when the network uses it: one in a module the top doesn't
instantiate is parsed and left alone. warptap's own ICL imports when emitted with
`include_access_link=False`.

Import doesn't recover `signal_bits` or `capture_value`: ICL records them only in a `//`
comment, which the parser discards, so an imported `InstrumentNode` always has `signal_bits=()`
and `capture_value=0`.

### The ITC'16 benchmark set

`tests/test_icl_import_itc16.py` runs `import_icl` on every network of the
[ITC'16 IJTAG benchmark set](https://gitlab.com/Honza255/benchmarks) (the parser author's
`Fixes` branch, commit `f4578a8`), each with the file list the set's own build script uses.
2 of its 25 networks import, both SIB trees, and round-trip through `to_icl`; their counts match
a separate structural walk of the ICL and the parser's own inventory of every scan register and
ScanMux. The other 23 are rejected:

| Network | Result |
|---|---|
| `Classic/q12710` | imports: 27 SIBs, 23 instruments, 26189 register bits |
| `Classic/t512505` | imports: 159 SIBs, 128 instruments, 77005 register bits |
| `Advanced/N17D3`, `N32D6`, `N73D14`, `N132D4`, `NE600P150`, `NE1200P430` | a scan register outside any SIB or ScanMux |
| `Basic/TreeBalanced`, `Basic/TreeUnbalanced`, `Classic/p22810` | registers in series behind one SIB |
| `Advanced/FlexScan`, `Advanced/TrapOrFlap`, `Basic/BasicSCB`, `Basic/TreeFlat` | a ScanMux selected by a register that isn't right after it (a segment control register elsewhere on the path or on another chain, or part of a wider register) |
| `Basic/Mingle` | a SIB_mux_post SIB |
| `Standard/CAD`, `Standard/SoC_Debug_Access_Port` | not elaborated by the parser: an addressed instance |
| `Standard/Multi_Core_AccessLink` | not elaborated by the parser: a `ToClockPort` with a `Source` |
| `Basic/TreeFlat_Ex` | invalid ICL: syntax errors (the `Fixes` branch leaves them in this file) |
| `Classic/a586710`, `Classic/p34392`, `Classic/p93791` | invalid ICL: a ScanMux used as data in a `LogicSignal` |
| `Standard/Kernel` | invalid ICL: `Parameter bidirs` defined twice in one module |
| `Standard/Multi_Test_Access_Port` | invalid ICL: one `ScanInterface` with both client-TAP and host ports |

The first two groups, 9 networks, are always-on registers; an import with them skipped, done
only as an experiment, gets through each of the 9 with nothing else in the way.

## What `import_pdl` reads

`import_pdl(text, interp)` replays PDL against a `PDLInterpreter`, each statement as the
interpreter call it stands for. A `;` or a line end ends a statement, so a line can hold several.
It reads:

- `iTarget`, `iWrite`, `iRead`, `iApply`, `iRunLoop`: what `to_pdl` writes;
- `iNote`: accepted and ignored (it is a message for the test log, and changes nothing shifted);
- `iProc` and `iCall`: a definition records the proc where it appears, so it must come before a
  call to it. A call binds the arguments in order, a missing one taking the default its
  definition gives (`iProc settle {{cycles 20}} { iRunLoop $cycles -tck }`), and runs the proc's
  commands with each `$name` replaced. Calls can nest.

Everything else raises `PdlImportError` naming it: the other commands (`iPrefix`, `iReset`,
`iScan`, `iTake`, `iClock` and the rest), `iPDLLevel` and `iProcsForModule`, an `iCall` on an
instance path or in a namespace, `iApply -together`, and a value that is an enum name or has `x`
bits. `iRunLoop -time` and `iUseProcNameSpace <name>` don't parse with the vendored grammar at
all, so they fail as syntax errors.

## Setup

Both functions parse through a real, independent, MIT-licensed parser
([`Honza255/icl_parser`](https://github.com/Honza255/icl_parser)) rather than a self-written
one: warptap never trusts its own reading of the ICL/PDL grammar. warptap ships a copy of it,
taken from the fork its source repository pins as a submodule (the fork adds the compiled PDL
parser `import_pdl` needs, which upstream lacks, and fixes in both grammars). The copy needs
ANTLR's Python runtime, which the `import` extra installs:

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
