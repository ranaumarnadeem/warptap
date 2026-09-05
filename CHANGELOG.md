# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/). No version has been
cut yet (`__version__` is still `0.0.1`) — everything below lives under `[Unreleased]`, one
entry per implementation stage (see [implementation_plan.md](implementation_plan.md) §7 for the
full design/validation story behind each).

## [Unreleased]

### Added

- **Stage 1 — Ingest/surgery/re-synth pipeline skeleton.** Yosys JSON ingest, a `Netlist`
  model, and re-emission back to Verilog, forming the surgery pipeline every later stage
  builds on.
- **Stage 2 — TAP FSM.** A table-driven, shared 16-state IEEE 1149.1 transition function, used
  identically by the RTL codegen and the Python behavioral model.
- **Stage 3 — BSR insertion.** Boundary-scan register insertion over pre-synthesis netlists,
  covering plain, full, and bidirectional (BC_7) cell types.
- **Stage 4 — ICL model + SIB primitive.** The physical shift-topology graph and
  module-instantiation tree data model, and the SIB (segment insertion bit) primitive.
- **Stage 5 — PDL interpreter + static retargeting.** `iWrite`/`iRead`/`iApply` over a flat/
  static SIB network, with correct phase-1/phase-2 bit composition.
- **Stage 6 — ICL connectivity check.** The "over-shifting" structural-consistency technique,
  confirming an inserted network matches its own declared topology.
- **Stage 7 — TAP-transaction IR + SVF/STAPL emitters.** A shared, format-independent op
  vocabulary (`tap_ir.py`), with SVF and STAPL text emitters live-validated against real
  OpenOCD and a real Jam STAPL Player build.
- **Stage 9 — Real functional instruments + PDL functional verification.** A genuine
  (non-stub) write-target TDR with a real update latch, and `pdl_verify.py`'s real-RTL
  `iRead`-expectation checking — proven against a real external design
  (`openMBIST`'s `mem_subsystem_mbist`).
- **Stage 10 — ICL emission.** Real ICL network-topology text emission, live-validated against
  a vendored, independent ICL parser (`Honza255/icl_parser`).
- **Stage 11 — PDL emission.** Real PDL statement text emission, backed by a new additive
  `PDLInterpreter.history` record; validated self-consistently after confirming no independent
  PDL parser exists anywhere to validate against.
- **Stage 12 — ICL import.** Parses real `.icl` files entirely through the same vendored ICL
  parser (never a self-written parser), recognizing warptap's own canonical SIB-network shape;
  round-trips Stage 10's own emission and correctly rejects every fixture in that parser's own
  real test corpus that isn't warptap-shaped.
- **Stage 13 — STIL emission.** This project's first new output format since SVF/STAPL. A new
  `PulsePin` op models a functional/system-clock pulse independent of TCK's own timing domain
  (SVF/STAPL cannot express this at all); live-validated against a real, independent,
  pip-installable STIL parser (`Semi-ATE-STIL`).
- **Stage 14 — faultflow pattern retargeting.** Consumes a real `faultflow` `--export-patterns`
  JSON export and retargets it through warptap's own SIB/TAP network (never faultflow's own
  SoC chain-offset table), validated at the gold-standard tier: a real Icarus cross-sim proving
  `PulsePin` pulses a genuinely independent clock domain against real simulated hardware.
- **Library-quality pass.** A shared `WarptapError` exception base (every specific error now
  subclasses it, fully backward compatible), a PEP 561 `py.typed` marker, a curated top-level
  `warptap` public API (`warptap/__init__.py`), and `insert_test_access()` — a single
  convenience function for the ingest → build-network → insert → re-emit sequence every
  cross-sim test previously hand-rolled.

### Explicitly out of scope

Nested/hierarchical SIB trees, dynamic `existPr`-conditional PDL reachability, PDL import (no
real parser exists anywhere to import via), any conformance claim against a specific IEEE
standard edition. See implementation_plan.md for the reasoning behind each.
