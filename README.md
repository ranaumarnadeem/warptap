# tapestry

[WIP] A python package to add JTAG, IJTAG and TAP in DFT-inserted circuitry and do ICL and
PDL — package/CLI name is `warptap`. See
[implementation_plan.md](https://github.com/ranaumarnadeem/tapestry/blob/main/implementation_plan.md)
for the design and roadmap.

## Usage

warptap is a library, not (yet) a CLI tool — import it and call its functions directly. The
core pipeline is: insert a JTAG/IJTAG test-access network into your design, then either drive
it (`PDLInterpreter`) and emit a real ATE pattern file, or hand the inserted netlist onward.

```python
from warptap import (
    InstrumentSpec, InstrumentDirection, SignalBinding,
    insert_test_access, PDLInterpreter, to_svf,
)

# One instrument per real signal you want write/read access to.
specs = [
    InstrumentSpec(
        "ctrl_write", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("ctrl_in"),),
    ),
    InstrumentSpec(
        "status_read", width=1, capture_value=0,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("status_out"),),
    ),
]

# Ingest your design, insert the TAP + SIB/instrument network, get back synthesizable Verilog
# plus everything needed to drive it.
inserted_verilog, graph, root = insert_test_access(["my_design.v"], "my_design", specs)

# Drive it: write ctrl_write, wait for it to settle, read status_read.
pdl = PDLInterpreter(graph, root)
pdl.iTarget("ctrl_write")
pdl.iWrite(1)
pdl.iApply()
pdl.iRunLoop(3)
pdl.iTarget("status_read")
pdl.iRead(1)
pdl.iApply()

# Emit a real ATE pattern file from the same ops.
print(to_svf(pdl.program))
```

`to_stapl`/`to_stil` render the identical `pdl.program` as STAPL/STIL instead; `to_icl`
describes the inserted network's own topology as real ICL text; `retarget_faultflow_patterns`
remaps a faultflow `--export-patterns` JSON export through the same network. Every exception
this library raises subclasses `WarptapError`. See
[implementation_plan.md](https://github.com/ranaumarnadeem/tapestry/blob/main/implementation_plan.md)
for the full design, standards grounding, and per-stage validation story.

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
