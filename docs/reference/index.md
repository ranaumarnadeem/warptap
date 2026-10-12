# API Reference

Every name documented here is re-exported from the top-level `warptap` package (`from warptap
import ...`) — this is warptap's own curated public surface, not a flattening of every internal
helper. Anything not listed here is an internal implementation detail and may change without
notice.

Every exception below subclasses `WarptapError`:

::: warptap.errors
    options:
      members: [WarptapError]
      show_root_heading: false
      show_root_toc_entry: false

## Pages

- [Netlist & Ingest](netlist.md) — Yosys JSON ingest and re-emission
- [Network Model](network-model.md) — the physical shift-topology graph and its node types
- [Insertion](insertion.md) — building and inserting a SIB/BSR network
- [PDL](pdl.md) — driving the network, PDL text emit/import, read verification
- [ICL](icl.md) — ICL text emit/import
- [BSDL](bsdl.md) — TAP-only BSDL emit, the file an ICL `AccessLink` points at
- [Network Integrity](integrity.md) — TAP/IJTAG network-integrity patterns and their TDO check
- [IEEE 1500 wrapper](ieee1500.md) — a wrapper and its serial port: model, ICL, PDL, scan operations, integrity program
- [Pattern Export](pattern-export.md) — the shared TAP-transaction IR, SVF/STAPL/STIL emitters
- [Faultflow](faultflow.md) — retargeting faultflow's own exported patterns
