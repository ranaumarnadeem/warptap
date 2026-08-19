# tapestry — project handoff / charter

Status: pre-implementation. No repo exists yet. This is the seed document — read this first
if you're starting the actual build, or picking this up as a fresh agent/collaborator with no
memory of the design discussion that produced it. The full raw discussion (more exploratory,
less structured) lives alongside this file in `jtag_ijtag_ecosystem_v2.md` — treat this doc as
the distilled, actionable version; that one as the appendix if you need the "why" in more
depth on any point.

Gitignored (`docs/plans/`), same as the file it's derived from — working notes, not committed.

---

## What tapestry is

A pip-installable Python tool that inserts and manages IEEE 1149.1 (JTAG/TAP) and IEEE 1687
(IJTAG — ICL + PDL) test-access infrastructure into a design. Given a design and a spec/
manifest describing what instruments and access structure you want, it:

1. Inserts the TAP controller, boundary-scan register, and IJTAG network (SIBs, instrument
   wiring) into the netlist/RTL.
2. Checks connectivity against the ICL model (does the inserted network structurally match
   what was declared).
3. Emits a design with everything wired up, plus a Yosys script for synthesis that preserves
   the test instruments (prevents `opt_clean`/`opt_expr` from eliminating logic that looks
   functionally dead but is load-bearing test infrastructure).

Later (not v1): retargets faultflow-generated patterns through the inserted TAP/IJTAG network
into ATE-deliverable pattern files (STIL for generic patterns, SVF/STAPL for JTAG-native
sequences), and validates PDL procedures functionally.

## What tapestry is explicitly NOT

- **Not a fault simulator.** No stuck-at/transition fault grading, no SAT ATPG. That's
  faultflow's job, permanently — see "Relationship to faultflow" below.
- **Not part of faultflow.** Separate repo, separate PyPI package, separate release cadence,
  separate identity doc. This was a deliberate architecture decision (see next section), not
  a default.
- **Not (initially) a full IEEE 1687 implementation.** v1 scope is deliberately narrow — see
  "v1 scope" below. Dynamic `existPr`-conditional PDL, hierarchical multi-level SIB trees, and
  full instrument-catalog support are explicitly deferred, not silently missing.

## Relationship to faultflow (why separate — the actual reasoning, not just the rule)

This was decided via a 4-agent design panel (3 independent advocates for integrate/separate/
hybrid + 1 judge synthesis) during planning, not a snap call. Full arguments for all three
positions are in `jtag_ijtag_ecosystem_v2.md` section 1 if you want to re-litigate it. The
short version:

- faultflow already drew this line once, in public, with OpenTestability (separate tool,
  interchange-based, not absorbed) and its own conference positioning ("slots into" a
  Yosys -> OpenROAD flow, doesn't absorb adjacent stages).
- faultflow's `CLAUDE.md` treats X-state, latches, and TBUF/tristate as *permanent* scope
  exclusions — the project has a demonstrated, deliberate discipline about not chasing every
  adjacent DFT concern into one codebase. JTAG/IJTAG is a bigger version of the same
  temptation to violate that discipline.
- ICL parsing + PDL's procedural execution semantics is a standalone hard problem, comparable
  in weight to faultflow's own SAT-ATPG engine. It doesn't fit faultflow's "flat arrays, no
  maps, immutable CompiledSimGraph" discipline, and doesn't need to.
- Different audience: faultflow users ask "did my ATPG vectors catch this fault"; tapestry
  users ask "how do I address and drive this embedded instrument through a TAP."

**What NOT to do because of this**: don't try to reuse faultflow's retargeting *algorithm* by
sharing code or a library. The retargeting problems are related in concept but not the same
problem — see "The three retargeting problems" below. Port understanding, not code.

## Integration mechanism

tapestry is pip-installable and pure Python (unlike Yosys, which is a subprocess dependency
either way), so the integration is tighter than faultflow<->OpenTestability's file/subprocess
boundary:

- faultflow depends on tapestry as an **optional, lazily-imported** dependency
  (`pip install faultflow[jtag]`-style extra, or a try/except import in `faultflow/cli.py`).
- If tapestry is importable, `ff.py` registers `jtag`/`ijtag` wrapper subcommands that call
  tapestry's Python API in-process.
- If tapestry is not installed, `ff.py` works exactly as it does today. Hard requirement: this
  must never become a hard dependency of faultflow's core.
- tapestry itself ships fully standalone — own CLI, own README, usable with faultflow never
  installed. The faultflow-side wrapper is a convenience layer on top, not the primary
  interface.
- Faultflow-side documentation footprint: one line in its own docs ("if `tapestry` is
  installed, these subcommands appear"). Do not add tapestry to faultflow's `CLAUDE.md` or
  `docs/roadmap.md` as a planned feature — it isn't one, from faultflow's side.

## The three retargeting problems — do not conflate these

This distinction is the single most important thing to get right architecturally. There are
three different problems that all get casually called "retargeting":

1. **faultflow's existing SoC-level retargeting** (already built, stays in faultflow,
   nothing for tapestry to do here). Block's own fault-detecting pattern -> SoC-level chain
   offset, preserving the proof the same fault is still detected. CLI precedent:
   `ff.py sim --scan --export-patterns PATH`, `ff.py retarget --patterns`.

2. **tapestry's own internal retargeting** (tapestry's actual core problem). Given an ICL
   network (SIBs, instruments, possibly nested), compute the SIB-select sequence to route
   TDI/TDO to a named instrument. Pure graph/tree traversal over the ICL model. Does **not**
   need faultflow's retargeting algorithm, ported or shared — this is IEEE 1687's own defined
   problem, self-contained. Complexity risk: PDL's `existPr` conditions (reachability that
   depends on other SIBs' current state, i.e. dynamic topology) — v1 should assume a static
   flat SIB tree and explicitly not handle this yet (see v1 scope).

3. **Translating a faultflow pattern into TAP-pin-protocol domain** (the boundary between the
   two projects, needs knowledge from both sides). faultflow generates patterns in the
   pseudo-PI/PO (scan-chain-bit) addressing domain. If the chip's only external access to that
   chain is through a TAP, the ATE can't set "chain bit K" directly — only TCK/TMS/TDI. This
   needs translating chain-bit-domain patterns into TAP instruction-select + Shift-DR bit
   sequences. **Mechanism**: reuse faultflow's existing `--export-patterns` JSON as the
   interchange format; tapestry consumes it and remaps through its own network model instead
   of a SoC chain-offset table. No faultflow internals needed, just its existing pattern
   export shape (possibly extended with new fields — open question, see below).

## PDL verification — who owns what

- **Functional correctness of tapestry's own retargeting output** ("did my computed TDI/TMS
  sequence actually produce the PDL-declared expected value") — tapestry's job. No fault
  model involved. Either a narrow purpose-built simulator (tapestry already knows the exact
  TAP+SIB structure it just inserted) or, if useful, depending on faultflow's existing
  golden-reference simulator as a library for this specific check.
- **Fault coverage of the TAP/SIB logic itself** (stuck-at/transition faults in the inserted
  infrastructure) — faultflow's job, unmodified. Once tapestry hands back a netlist, it's just
  a netlist to faultflow. Only new work needed on faultflow's side: confirm any IJTAG-standard
  cell types used map onto existing Sky130/OSU035 cell-map `GateType`s, or add JSON cell-map
  entries if not.

## Pattern export format: STIL vs SVF

- STIL (already a stub on faultflow's roadmap — `write_patterns`, currently raises "not
  implemented") is protocol-agnostic pin/timing data. Fine for faultflow's own
  stuck-at/transition patterns. Not JTAG-aware — if it happens to encode a correct TAP walk,
  playback works because the sequence is correct, not because STIL or the ATE understood
  JTAG.
- SVF/STAPL ARE protocol-aware (`SIR`/`SDR`/`STATE` framing instead of raw pin values).
  Dedicated JTAG tooling/hardware consumes these directly.
- **tapestry's retargeting output (problem 3 above) should target SVF/STAPL as the primary
  format**, not STIL — PDL is already expressed in instrument-level operations close to SVF's
  own vocabulary. Offer STIL export only if there's a concrete need to interleave JTAG access
  sequences into a broader mixed-pattern ATE test program.

## v1 scope (deliberately narrow)

- Flat SIB network only — no nested/hierarchical SIB-gating-SIB trees yet.
- Static PDL only — `iWrite`/`iRead`, no `existPr` conditional reachability. Document this
  limitation loudly; don't silently produce wrong results on a design that needs dynamic
  reachability.
- One instrument type first, prove the pipeline end-to-end before generalizing.
- Insertion + ICL connectivity check + synthesis-preserving Yosys output only. No pattern
  retargeting (problem 3), no PDL functional verification, no faultflow integration wrapper
  commands — those are explicit v1.x/v2 follow-ons once the core insertion flow round-trips
  correctly.
- Reuse faultflow's own locked-synthesis-script discipline as the model for the
  test-preserving Yosys script (`(* keep *)`-style directives or equivalent) — faultflow
  already hit a closely related bug class (IEEE 1500 WBR cells silently computing 0 in
  FUNCTIONAL mode because a case wasn't modeled in synthesis/lowering) and has a working
  pattern for avoiding it.

## First milestone (smallest end-to-end proof)

TAP FSM + minimal ICL parser + a PDL interpreter that retargets through *one*
faultflow-emitted IEEE-1500 wrapper. This proves the interchange contract with faultflow's
existing wrapper/scan output without requiring any new faultflow-side work, and is the
natural seam: you're not simulating a fault, you're proving you can shift a bit through the
wrapper faultflow already builds.

## Packaging notes

- Will need to shell out to Yosys for the synthesis-preserving script — this is an external
  subprocess dependency, same as faultflow's own Yosys usage. pip can't bundle it. Don't let
  "pip install tapestry" imply zero external setup; document the Yosys dependency clearly,
  matching however faultflow's own README frames this.
- Suggested package name: `tapestry` on PyPI — **not yet verified for availability**, check
  before committing to it anywhere permanent (setup.py, announcements, etc.).

## Open questions carried over (unresolved, need a decision before or during implementation)

- Exact shape of the pattern-export JSON extension needed for retargeting problem 3 — does
  faultflow's existing `--export-patterns` format need new fields, or is it already sufficient
  as an opaque chain-bit-pattern payload tapestry re-addresses on its own?
- Scan compression/decompression (EDT-style: XOR/LFSR decompressor on scan-in, XOR-tree/MISR
  compactor on scan-out) is a **faultflow-side** feature, not tapestry's concern — it's a
  separate, not-yet-scoped v2+ item on faultflow's side. Full technical breakdown (SAT CNF
  integration approach, aliasing risk, new coverage-policy category needed for
  "compression-unreachable" faults) is in `jtag_ijtag_ecosystem_v2.md` sections 6 and the
  "Compression policy — open questions" subsection under section 8. Not tapestry's problem,
  listed here only so it isn't lost/conflated with tapestry's own scope.
- Whether/when to formalize faultflow's X-state and latches as permanent exclusions in its own
  `CLAUDE.md`/`feature.md`/`docs/roadmap.md` — decided in principle this session, explicitly
  not yet written into those files ("keep it like that" — deferred, not forgotten).

## Naming

Decided: **tapestry**. Rationale: pun on TAP (Test Access Port), and a genuinely fitting
metaphor — an ICL network of nested SIBs *is* a woven structure of interconnected access
paths. Real English word, no known collision with existing tools (checked against the
tempting-but-taken alternatives: `OpenJTAG` is an existing open-source USB JTAG hardware
adapter project; `OpenTAP` is Keysight's existing open-source test-automation framework;
`OpenAccess` is Cadence's existing foundational EDA database API — all avoided for that
reason).
