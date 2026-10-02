# warptap

[![PyPI](https://img.shields.io/pypi/v/warptap)](https://pypi.org/project/warptap/)
[![Docs](https://img.shields.io/badge/docs-github%20pages-2ea44f)](https://ranaumarnadeem.github.io/warptap/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**warptap is an open-source Python library that inserts IEEE 1149.1 (JTAG/TAP) and IEEE 1687 (IJTAG) test-access networks into RTL before synthesis, emits ICL and PDL, and generates SVF, STAPL and STIL patterns to drive them.**

Design-for-test (DFT) instruments such as MBIST controllers, scan compression and on-chip monitors need a standard access path from the chip pins. Commercial flows (Tessent IJTAG, Modus) do this; warptap does it in open source. You describe which control and status signals need access, warptap inserts a TAP controller, Segment Insertion Bits (SIBs) and Test Data Registers, returns synthesizable Verilog, and gives you an Instrument Connectivity Language (ICL) model plus a Procedural Description Language (PDL) interpreter to read and write those signals.

- Documentation: https://ranaumarnadeem.github.io/warptap/
- PyPI: `pip install warptap`
- Status: alpha (0.0.x). APIs may change between releases.

## Features

- TAP controller and instruction register insertion (IEEE 1149.1), with a configurable IDCODE
- IJTAG network insertion with SIBs and TDRs (IEEE 1687), including nested SIBs, multi-arm ScanMux and named sub-field addressing
- Boundary-scan register insertion, as an alternative to the IJTAG network (a design gets one or the other)
- ICL emission, and import of networks in warptap's own ICL shape
- TAP-only BSDL emission, matching the instruction the ICL `AccessLink` names
- PDL emission, import and an in-Python PDL interpreter
- Pattern output: SVF, STAPL (with CRC16) and STIL
- TAP and IJTAG network-integrity test programs, with the TDO they expect
- Retargeting of faultflow scan patterns, including compressed and compacted scan
- Pre-synthesis RTL insertion, so the result goes through your normal synthesis flow

## Install

```bash
pip install warptap
```

Needs a real `yosys` on `PATH` (shelled out to, never bundled).

warptap is a Python library. A `warptap` CLI entry point exists but is not stable yet: today
it only prints its version and help, with no subcommands.

## Development

Requires a real `yosys` on `PATH` (this project shells out to it, the same way faultflow does
for its own Yosys usage; it is never bundled). On Windows, run everything through WSL; system `pip`
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
Yosys-compatible executable, and `tests/conftest.py` falls back to a pip-installed
`yowasp-yosys` (WASM build, see the `dev` extra) if nothing is found on `PATH`.

Some tests also cross-simulate hand-authored RTL against its Python behavioral-model
counterpart using Icarus Verilog (`iverilog`/`vvp`, already present in the documented WSL
environment). There's no WASM fallback for these: if neither is on `PATH` nor pointed at via
`WARPTAP_IVERILOG_CMD`/`WARPTAP_VVP_CMD`, those tests skip cleanly rather than failing.
