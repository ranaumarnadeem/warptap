# Nested SIB networks

A SIB doesn't have to gate a single instrument directly — it can gate a whole nested
sub-network instead, at any depth. `HierarchySpec` models this: it takes a `name` and a list of
`children`, where each child is either an `InstrumentSpec` (a leaf) or another `HierarchySpec`
(another level of nesting). It composes with plain `InstrumentSpec`s in the same list
`build_sib_plan` already takes — nothing else about the API changes.

```python
from warptap import HierarchySpec, InstrumentSpec, build_sib_plan

specs = [
    HierarchySpec(
        "bank_a",
        children=[
            InstrumentSpec("deep_a", width=1, capture_value=0),
            InstrumentSpec("deep_b", width=1, capture_value=0),
        ],
    ),
    InstrumentSpec("top_level", width=1, capture_value=0),
]
graph, root = build_sib_plan(specs)
```

## Addressing stays flat

Even though `deep_a`/`deep_b` sit two SIBs deep (one for `bank_a`, one for the leaf itself),
`root`'s own children are still a flat sibling list — `deep_a`, `deep_b`, and `top_level` are
all direct children of `root`, matching the same dotted-address convention a non-nested network
uses. Target `deep_a` with `iTarget("deep_a")`, not a hierarchical path — `HierarchySpec` never
contributes a `ModuleInstance` of its own, only its leaf descendants do.

## What actually happens on real hardware

Opening a target nested `d` levels deep costs `d` separate Capture-Shift-Update rounds, not
one — a closed SIB's nested content isn't physically part of the live scan chain until its own
gating SIB opens, so reaching `deep_a` means first opening `bank_a`'s own SIB, then `deep_a`'s.
`PDLInterpreter.iApply` handles this transparently: it stages exactly as many rounds as the
target's real depth requires, and reuses whatever ancestor prefix is already open from a
previous `iApply` rather than re-opening it from scratch (retargeting two siblings under the
same open ancestor back to back costs less than two cold opens).
