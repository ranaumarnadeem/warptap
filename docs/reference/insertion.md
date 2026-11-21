# Insertion

Building a network plan, then actually inserting it into a netlist.

## The TDO pin

`insert_test_access`, `insert_sib_network` and `insert_bsr` all take `tdo_drive`, which says how
the inserted TAP drives its `tdo` pin. In every case TDO changes only on the falling edge of TCK.

| `tdo_drive` | `tdo` outside Shift-IR/Shift-DR | Added ports |
| --- | --- | --- |
| `"tristate"` (default) | high impedance: a `$tribuf` on the pin, enabled by `tap_core`'s `tdo_oe` | none |
| `"low"` | driven low, as in warptap 0.0.6 and earlier; not IEEE 1149.1 conformant | none |
| `"enable_port"` | driven low; the pad that drives the chip's TDO pin does the tri-state | `tdo_oe`, high only in Shift-IR/Shift-DR |

Use `"enable_port"` for a design that is a core rather than the whole chip, where the pad ring is
added later. Give [`to_bsdl`](bsdl.md) the same value: its `DESIGN_WARNING` says how TDO is
driven when it is not tri-stated in the design.

## The network's instruction

The TAP `insert_test_access` and `insert_sib_network` insert selects the IJTAG network with an
instruction of its own, IJTAG_ACCESS, at `ijtag_access_opcode` (default
`warptap.tap_model.OPCODE_IJTAG_ACCESS`, `1100`). EXTEST, SAMPLE and PRELOAD select the 1-bit
BYPASS register, so a board-level EXTEST and its scans leave the network, and every signal a
WRITE instrument drives, untouched. [`to_bsdl`](bsdl.md), [`to_icl`](icl.md) and
[`TapConfig`](integrity.md) describe that TAP by default, and a pattern loads
`select_instruction(OPCODE_IJTAG_ACCESS)` before the PDL program.

`ijtag_access_opcode=None` selects the network with EXTEST instead, as warptap 0.0.6 and earlier
did by default. Describe that TAP with `to_bsdl(..., ijtag_access_opcode=None)`,
`to_icl(..., ijtag_access=False)` and `TapConfig(ijtag_access_opcode=None)`, and load
`select_instruction(OPCODE_EXTEST)`. A boundary-scan design (`insert_bsr`) has no network: its
EXTEST, SAMPLE and PRELOAD select the boundary register.

::: warptap.sib_plan
    options:
      members: [HierarchySpec, InstrumentSpec, build_sib_plan]

::: warptap.sib_insert
    options:
      members: [SibInsertError, insert_sib_network]

::: warptap.bsr_insert
    options:
      members: [BsrInsertError, insert_bsr]

::: warptap.pipeline
    options:
      members: [insert_test_access]
