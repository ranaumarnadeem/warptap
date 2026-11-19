# BSDL

A [BSDL](https://en.wikipedia.org/wiki/Boundary_scan_description_language) emitter for the TAP
warptap inserts. It writes two kinds of file:

- **TAP-only**, for a SIB-network design: the file the ICL `AccessLink` written by
  [`to_icl`](icl.md) points at, so ICL, BSDL and PDL together say how to reach the network. It
  declares no boundary register, so tools that require one will reject it.
- **Chip-level**, for a design with a boundary-scan register (`insert_bsr`): every port with its
  declared range, the boundary register cell by cell, and EXTEST, SAMPLE and PRELOAD selecting
  it.

See the module docstring below for the exact scope, what `EXTEST` means for a SIB network, and
what is (and is not) independently validated.

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

Give `to_bsdl` the same `tdo_drive` as well ([The TDO pin](insertion.md#the-tdo-pin)). By
default the inserted TDO is tri-stated outside the shift states and the `DESIGN_WARNING` says
nothing about it; with `"low"` it says the TAP is not fully IEEE 1149.1 conformant, and with
`"enable_port"` that the pad does the tri-state, enabled by `tdo_oe`.

## A boundary-scan design

Pass the plan the design was inserted with as `boundary`:

```python
from warptap import Netlist, ingest, insert_bsr, to_bsdl
from warptap.bsr_plan import build_bsr_plan

netlist = Netlist.from_json(ingest(sources, "chip"))
plan = build_bsr_plan(netlist.module("chip"), clock_ports=frozenset({"clk"}))
insert_bsr(netlist, "chip", plan, idcode_value=IDCODE)
bsdl_text = to_bsdl("chip", tck_max_freq_hz=10e6, idcode_value=IDCODE, boundary=plan)
```

Every bit of a multi-bit port has its own cells (`d(3)` for bit 3 of `input [3:0] d`), and each
output or bidirectional bit its own control cell, which disables the pin at 0. Ports left off the
register (`clock_ports`, `excluded_ports`) are declared but have no cell; the `DESIGN_WARNING`
names them. Port names must be valid VHDL identifiers that aren't reserved words (`bus`, `in`
and `out` are not), and the `PIN_MAP` is a placeholder that names each pin after its port.

The instruction register and every update stage latch on the rising edge of TCK that leaves
Update-IR or Update-DR, half a TCK period after IEEE 1149.1's falling edge in those states. Every
file's `DESIGN_WARNING` says so: a tester that stops TCK in Update-DR sees the new outputs only
at the next rising edge.

::: warptap.bsdl_emit
    options:
      members: [BsdlEmitError, to_bsdl]
