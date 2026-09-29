# BSDL

A TAP-only [BSDL](https://en.wikipedia.org/wiki/Boundary_scan_description_language) emitter: the
file the ICL `AccessLink` written by [`to_icl`](icl.md) points at, so ICL, BSDL and PDL together
say how to reach the network. It is **not** a chip-level BSDL -- it declares no boundary register,
so tools that require one will reject it. See the module docstring below for the exact scope, what
`EXTEST` means for a SIB network, and what is (and is not) independently validated.

```python
from warptap import to_bsdl, to_icl

bsdl_text = to_bsdl("mem_subsystem_mbist", tck_max_freq_hz=10e6)
icl_text = to_icl(graph, root, bsdl_entity_name="mem_subsystem_mbist")
```

::: warptap.bsdl_emit
    options:
      members: [BsdlEmitError, to_bsdl]
