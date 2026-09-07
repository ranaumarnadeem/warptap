# tapestry

[WIP] A python package to add JTAG, IJTAG and TAP in DFT-inserted circuitry and do ICL and
PDL — package/CLI name is `warptap`.

## Usage

warptap is a library, not (yet) a CLI tool: `pip install warptap`, then call its functions
from your own build/generation pipeline — there's no daemon or service, each call does one
piece of work and returns. The core pipeline is: insert a JTAG/IJTAG test-access network into
your design, then either drive it (`PDLInterpreter`) and emit a real ATE pattern file, or hand
the inserted netlist on to your own normal synthesis flow.

The example below mirrors a real integration shape: a generator (e.g. an MBIST wrapper
generator) produces RTL with real control/status ports, then warptap wraps a subset of them
with JTAG/IJTAG access before synthesis.

```python
from warptap import (
    InstrumentSpec, InstrumentDirection, SignalBinding,
    insert_test_access, PDLInterpreter, to_svf,
)

# One instrument per real signal you want write/read access to -- mix WRITE (control) and
# READ (status) freely in one network.
specs = [
    InstrumentSpec(
        "self_repair_start", width=1, capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=(SignalBinding("self_repair_start"),),
    ),
    InstrumentSpec(
        "self_repair_busy", width=1, capture_value=0,
        direction=InstrumentDirection.READ,
        signal_bits=(SignalBinding("self_repair_busy"),),
    ),
]

# Ingest your generated/hand-written RTL, insert the TAP + IJTAG network, get back
# synthesizable Verilog plus everything needed to drive it. From here, feed the returned
# Verilog into your own normal synthesis flow instead of the original sources.
inserted_verilog, graph, root = insert_test_access(
    ["mem_subsystem_mbist.sv"], "mem_subsystem_mbist", specs,
)

# Drive it: write self_repair_start, wait for it to settle, read self_repair_busy.
pdl = PDLInterpreter(graph, root)
pdl.iTarget("self_repair_start")
pdl.iWrite(1)
pdl.iApply()
pdl.iRunLoop(10)
pdl.iTarget("self_repair_busy")
pdl.iRead(1)
pdl.iApply()

# Emit a real ATE pattern file from the same ops.
print(to_svf(pdl.program))
```

`to_stapl`/`to_stil` render the identical `pdl.program` as STAPL/STIL instead; `to_icl`
describes the inserted network's own topology as real ICL text; `retarget_faultflow_patterns`
remaps a faultflow `--export-patterns` JSON export through the same network. Every exception
this library raises subclasses `WarptapError`.

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
