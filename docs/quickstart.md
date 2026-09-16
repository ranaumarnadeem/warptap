# Quickstart

```bash
pip install warptap
```

warptap also needs a real [Yosys](https://github.com/YosysHQ/yosys) — it shells out to it for
netlist ingest and re-emission, and never bundles it. If nothing is found on `PATH`, set
`WARPTAP_YOSYS_CMD` to point at any Yosys-compatible executable, or install the `yowasp-yosys`
WASM build as a fallback.

## The core pipeline

Insert a JTAG/IJTAG test-access network into your design, then either drive it with
`PDLInterpreter` and emit a real ATE pattern file, or hand the inserted netlist on to your own
normal synthesis flow.

The example below mirrors a real integration shape: a generator (e.g. an MBIST wrapper
generator) produces RTL with real control/status ports, then warptap wraps a subset of them
with JTAG/IJTAG access before synthesis.

### 1. Declare the instruments

One `InstrumentSpec` per real signal you want write/read access to — mix `WRITE` (control) and
`READ` (status) freely in one network. Each one's `signal_bits` names
the real host port (and, for a multi-bit signal, the specific bit) it binds to.

```python
from warptap import InstrumentSpec, InstrumentDirection, SignalBinding

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
```

### 2. Insert the network

`insert_test_access` ingests your RTL, inserts the TAP plus the IJTAG scan network the specs
above describe, and hands back synthesizable Verilog plus everything needed to drive it. From
here, feed the returned Verilog into your own normal synthesis flow instead of the original
sources. See the [Insertion reference](reference/insertion.md) for the exact signature.

```python
from warptap import insert_test_access

inserted_verilog, graph, root = insert_test_access(
    ["mem_subsystem_mbist.sv"], "mem_subsystem_mbist", specs,
)
```

### 3. Drive it

`PDLInterpreter` is a small state machine over four commands: `iTarget` selects which
instrument the next command addresses, `iWrite`/`iRead` queue a value to write or an expected
value to check, and `iApply` actually emits the scan operations that retarget the network to
that instrument and shift the value through. `iRunLoop` advances time — by default in
Run-Test/Idle (`-tck`), or, given a real functional-clock port name, by pulsing that clock
directly instead (see [System-clock pulsing](guide/sck-clock-pulsing.md)).

```python
pdl = PDLInterpreter(graph, root)
pdl.iTarget("self_repair_start")
pdl.iWrite(1)
pdl.iApply()
pdl.iRunLoop(10)                # let it settle
pdl.iTarget("self_repair_busy")
pdl.iRead(1)
pdl.iApply()
```

### 4. Emit a pattern file

Every op `PDLInterpreter` recorded is sitting in `pdl.program` — render it as whichever ATE
format you need:

```python
from warptap import to_svf

print(to_svf(pdl.program))
```

`to_stapl`/`to_stil` render the identical `pdl.program` as STAPL/STIL instead. `to_icl`
describes the inserted network's own topology as real ICL text. Every exception this library
raises subclasses `WarptapError`. See the [Pattern Export](reference/pattern-export.md) and
[ICL](reference/icl.md) reference pages for details.

## Next

- [Guide](guide/nested-sib-networks.md) for nested networks, ScanMux, field addressing, import,
  clock pulsing, and faultflow retargeting.
- [API Reference](reference/index.md) for the full public surface.
