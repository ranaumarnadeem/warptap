# Multi-arm ScanMux

A real IEEE 1687 `ScanMux` selects one of several arms into the live scan chain based on a
select value — only the matched arm's own content is actually shiftable; every other arm stays
physically detached until it's selected. `ScanMuxNode`/`ScanArm` model this directly.

Unlike `HierarchySpec`/`InstrumentSpec`, `build_sib_plan` doesn't construct a `ScanMuxNode`
yet — build the `PhysicalGraph` (and its matching `ModuleInstance` tree) directly instead:

```python
from warptap import (
    InstrumentDirection, InstrumentNode, ModuleInstance,
    PhysicalGraph, ScanArm, ScanMuxNode,
)

mux = ScanMuxNode(
    "mux_a", select_width=2,
    arms=(
        ScanArm(
            values=(1,),
            instrument=InstrumentNode(
                name="arm1", width=3, capture_value=0,
                direction=InstrumentDirection.WRITE,
            ),
        ),
        ScanArm(
            values=(2,),
            instrument=InstrumentNode(
                name="arm2", width=2, capture_value=0,
                direction=InstrumentDirection.WRITE,
            ),
        ),
    ),
)
graph = PhysicalGraph(chain=(mux,))
root = ModuleInstance("top", children=(ModuleInstance("arm1"), ModuleInstance("arm2")))
```

Each `ScanArm`'s own `values` is the select value(s) that route to it (`arm1` at select=1,
`arm2` at select=2); an arm gates exactly one of `instrument`/`nested` — the same leaf-or-
hierarchy shape a plain `SibNode` has, so a mux arm can gate a further nested sub-network
instead of a single instrument, just like `HierarchySpec` does.

## Driving it

Targeting `arm1` then `arm2` retargets the mux directly from one arm to the other — no
intermediate "close everything" step:

```python
from warptap import PDLInterpreter

pdl = PDLInterpreter(graph, root)
pdl.iTarget("arm1")
pdl.iWrite(0b110)
pdl.iApply()

pdl.iTarget("arm2")     # switches the mux's own select value directly
pdl.iWrite(0b01)
pdl.iApply()

pdl.iTarget("arm1")     # switches back -- arm1's own value survives the round trip
pdl.iRead(0b110)
pdl.iApply()
```

A written value survives being switched away from and back, on real hardware — confirmed by
cross-simulating this exact scenario against real RTL.
