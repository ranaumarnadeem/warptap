# Faultflow pattern retargeting

[`faultflow`](https://github.com/ranaumarnadeem/faultflow)'s own `--export-patterns` produces a
JSON array of chain-level load/unload patterns, addressed by SoC-level chain id — not warptap's
own instrument names, and not routed through warptap's own SIB/TAP network at all.
`retarget_faultflow_patterns` bridges the two: given a mapping from chain id to instrument
name, it drives a real `PDLInterpreter` over your own inserted network and retargets every
pattern through it, returning the accumulated ops.

```python
from warptap import retarget_faultflow_patterns, to_stil

patterns = json.loads(exported_patterns_path.read_text())

ops = retarget_faultflow_patterns(
    patterns,
    {0: "stim_write", 1: "captured_read"},   # chain id -> warptap instrument name
    graph, root,
    clock_port="sysclk",
    clock_pulse_count=1,
)

print(to_stil(ops, pulse_periods={"sysclk": "20ns"}))
```

`clock_port`/`clock_pulse_count` matter when a pattern's own capture depends on a real
functional clock edge, not just a JTAG shift/update — the same `-sck` pulsing mechanism
[System-clock pulsing](sck-clock-pulsing.md) describes, here inserted automatically between a
pattern's load and its unload rather than called by hand. `chain_to_instrument` must cover
every chain id any pattern actually references, and every name in it must be a real instrument
in `graph` — either gap raises `FaultflowRetargetError` naming the offending chain.

`to_stil` is the only pattern-export backend that can render the resulting ops: SVF and STAPL
have no way to express a functional-clock pulse independent of TCK's own timing domain, only
STIL does.
