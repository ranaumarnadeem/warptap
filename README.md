# tapestry

[WIP] A python package to add JTAG, IJTAG and TAP in DFT-inserted circuitry and do ICL and
PDL — package/CLI name is `warptap`. See [implementation_plan.md](implementation_plan.md) for
the design and roadmap.

## Development

Requires a real `yosys` on `PATH` (this project shells out to it, same as faultflow does for
its own Yosys usage — it's never bundled). On Windows, run everything through WSL, which
already has `yosys`/`python3`/`pytest` installed system-wide; only the package itself needs an
editable install, and system `pip` there is externally-managed (PEP 668), so that install goes
through a small local venv:

```bash
wsl bash -lc "cd /mnt/c/path/to/tapestry && python3 -m venv --system-site-packages .venv && ./.venv/bin/pip install -e ."
wsl bash -lc "cd /mnt/c/path/to/tapestry && ./.venv/bin/python -m pytest"
```

`--system-site-packages` lets the venv see WSL's system-installed `pytest` instead of
reinstalling it. If a real `yosys` isn't available at all, `WARPTAP_YOSYS_CMD` can point at any
Yosys-compatible executable — `tests/conftest.py` falls back to a pip-installed
`yowasp-yosys` (WASM build, see the `dev` extra) if nothing is found on `PATH`.

Some tests also cross-simulate hand-authored RTL against its Python behavioral-model
counterpart using Icarus Verilog (`iverilog`/`vvp`, already present in the documented WSL
environment). There's no WASM fallback for these — if neither is on `PATH` nor pointed at via
`WARPTAP_IVERILOG_CMD`/`WARPTAP_VVP_CMD`, those tests skip cleanly rather than failing.
