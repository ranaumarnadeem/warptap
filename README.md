# tapestry

Python library to insert IEEE 1149.1 (JTAG/TAP) and IEEE 1687 (IJTAG/ICL+PDL) test access into
RTL designs, pre-synthesis, via Yosys netlist surgery. Package/CLI name is `warptap`.

Supports nested SIB networks, multi-arm ScanMux, named sub-field addressing, ICL/PDL
emit+import, TAP-only BSDL emit, SVF/STAPL/STIL pattern export, faultflow pattern
retargeting, and TAP/IJTAG network-integrity patterns.

## Install

```bash
pip install warptap
```

Needs a real `yosys` on `PATH` (shelled out to, never bundled).

Docs: https://ranaumarnadeem.github.io/warptap/
Source: https://github.com/ranaumarnadeem/warptap

## Development

Requires a real `yosys` on `PATH` (this project shells out to it, same as faultflow does for
its own Yosys usage — it's never bundled). On Windows, run everything through WSL; system `pip`
there is externally-managed (PEP 668), so install into a local venv:

```bash
git clone --recurse-submodules https://github.com/ranaumarnadeem/warptap.git
cd warptap
python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'
./.venv/bin/python -m pytest
```

`third_party/icl_parser` is a git submodule (an ICL/PDL parser the grammar and import tests run
against). In an existing clone, and in every `git worktree`, run
`git submodule update --init third_party/icl_parser` first. The `dev` extra installs its Python
dependencies (`antlr4-python3-runtime==4.7.2`, `z3-solver`, `sympy`, `networkx`). Without the
submodule or those packages the parser tests fail with that instruction;
`WARPTAP_ALLOW_MISSING_ICL_PARSER=1` makes them skip instead.

If a real `yosys` isn't available at all, `WARPTAP_YOSYS_CMD` can point at any
Yosys-compatible executable — `tests/conftest.py` falls back to a pip-installed
`yowasp-yosys` (WASM build, see the `dev` extra) if nothing is found on `PATH`.

Some tests also cross-simulate hand-authored RTL against its Python behavioral-model
counterpart using Icarus Verilog (`iverilog`/`vvp`, already present in the documented WSL
environment). There's no WASM fallback for these — if neither is on `PATH` nor pointed at via
`WARPTAP_IVERILOG_CMD`/`WARPTAP_VVP_CMD`, those tests skip cleanly rather than failing.
