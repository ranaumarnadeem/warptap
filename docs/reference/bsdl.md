# BSDL

A TAP-only [BSDL](https://en.wikipedia.org/wiki/Boundary_scan_description_language) emitter: the
file the ICL `AccessLink` written by [`to_icl`](icl.md) points at, so ICL, BSDL and PDL together
say how to reach the network. It is **not** a chip-level BSDL -- it declares no boundary register,
so tools that require one will reject it. See the module docstring below for the exact scope, what
`EXTEST` means for a SIB network, and what is (and is not) independently validated.

```python
from warptap import insert_test_access, to_bsdl, to_icl

IDCODE = 0x5CA1AB1F  # your own: 32 bits, bit 0 set (IEEE 1149.1)

verilog, graph, root = insert_test_access(sources, "mem_subsystem_mbist", specs, idcode_value=IDCODE)
bsdl_text = to_bsdl("mem_subsystem_mbist", tck_max_freq_hz=10e6, idcode_value=IDCODE)
icl_text = to_icl(graph, root, bsdl_entity_name="mem_subsystem_mbist")
```

Give `to_bsdl` the same `idcode_value` the TAP was inserted with: the BSDL's `IDCODE_REGISTER`
must match what the hardware shifts out. Without it, both use `warptap.tap_model.IDCODE_VALUE`,
a placeholder whose manufacturer field lands in an assigned JEP106 slot, so set your own for
anything you ship. `TapModel` and `tap_integrity.TapConfig` take the same `idcode_value` when
modelling or testing that TAP.

::: warptap.bsdl_emit
    options:
      members: [BsdlEmitError, to_bsdl]
