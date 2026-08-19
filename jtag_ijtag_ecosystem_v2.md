# v2 planning: JTAG/IJTAG ecosystem tool + scan compression + the retargeting chain

Status: exploratory planning, not committed to any roadmap. Gitignored (`docs/plans/`) —
working notes, not a durable spec. Captures a design discussion; revisit before acting on it.

---

## 1. The core architectural decision: separate pip package, not part of faultflow

**Decision (from a 4-agent design panel: 3 independent advocates + 1 judge synthesis):
build JTAG (IEEE 1149.1) / IJTAG (IEEE 1687, ICL+PDL) support as a separate, pip-installable
package — not as new subcommands/modules inside faultflow's C++ core.**

### Why
- faultflow already made this exact call once, in public, with OpenTestability (a separate
  tool, subprocess/JSON interchange, not absorbed).
- faultflow's own conference positioning is "an open-flow tool that slots into Yosys ->
  OpenROAD" — composing, not absorbing adjacent stages.
- CLAUDE.md treats X-state, latches, and TBUF/tristate as *permanent* scope exclusions, not
  backlog debt. Building a full ICL parser + PDL interpreter into the same repo that just
  spent six phases hardening a flat-array, no-maps, immutable IR (`ParsedGraph ->
  NormalizedGraph -> CompiledSimGraph`) is the scope creep that discipline exists to prevent.
- Different audience: faultflow users ask "did my ATPG vectors catch this fault"; JTAG/IJTAG
  users ask "how do I address and drive this embedded instrument through a TAP." Different
  mental model, different inputs (BSDL/ICL/PDL vs Yosys JSON).
- ICL/PDL is a standalone hard problem (its own parser + PDL's procedural execution
  semantics, comparable in weight to faultflow's own SAT-ATPG engine) — folding it in either
  compromises the "flat arrays, no maps" discipline or maintains a second unrelated parser
  stack under one repo.

### Point the panel's losing side got right (keep this in mind)
Retargeting is real shared *conceptual* IP — faultflow's Stage 4/5 block-chain-to-SoC-pin
retargeting and IJTAG's instrument-to-TAP retargeting are the same *kind* of problem in
different vocabulary. The resolution: **port the algorithm as a spec, don't share the
codebase, and don't extract a shared library preemptively.** Extracting a library before a
second real consumer exists is guessing a stable interface from one data point — wait for an
actual divergence bug or a second consumer before pulling one out.

### Integration mechanism (revised after further discussion)
Original assumption was OpenTestability-style subprocess/file interchange. Since the new tool
will be pip-installable (pure Python, unlike Yosys), a tighter integration is reasonable:

- faultflow depends on it as an **optional, lazily-imported** dependency
  (`pip install faultflow[jtag]`-style extra, or try/except import).
- If importable, `ff.py` registers `jtag`/`ijtag` wrapper subcommands that call into the new
  package's Python API directly (no subprocess boundary needed, since it's in-process).
- If *not* installed, `ff.py` works exactly as it does today — no hard dependency.
- This is a common, well-precedented pattern (pytest plugins, CLI extras) — gets the "feels
  like one tool" UX without merging codebases. The new package still ships standalone with
  its own repo, README, release cadence, and is fully usable without faultflow installed.
- Faultflow-side documentation impact: one line ("if `<pkg>` is installed, these subcommands
  appear"), not a roadmap addition. `CLAUDE.md` / `docs/roadmap.md` stay as-is — the forward
  plan is still just X-state -> latches (both currently deferred, see decision below).

---

## 2. Separate decision, folded in here: X-state and latches are now permanent exclusions

During this same discussion, decided **not** to pursue Phase 7 (X-state / 3-valued sim). Cited
reasoning: even Verilator — a mature tool with a large contributor base and years of runway —
only recently added X-state support despite it being one of the hardest features to get
right. CLAUDE.md already called X-state "the single most invasive planned change"; this
confirms rather than surprises.

**Consequence**: Phase 8 (latches) is explicitly "built on X-state" per CLAUDE.md, so it drops
too, not just delays. This moves X-state and latches into the same bucket as TBUF/CDC —
permanent, documented scope boundaries, not "not yet." (Not yet applied to CLAUDE.md /
feature.md / docs/roadmap.md — user said "keep it like that" for now, i.e. don't edit those
files yet. Revisit if/when ready to formalize.)

Release-scoping consequence: faultflow's permanent scope is flip-flop-only synchronous
designs, no tristate. That's a legitimate, well-validated v1.0 scope (real SAT-proven
redundancy, hierarchical SoC-scale results, a published paper) as long as it's documented
clearly — not "handles arbitrary netlists."

---

## 3. The retargeting chain — three distinct problems, easy to conflate

There are **three different "retargeting" problems** in this space. Keep them separate:

### 3a. faultflow's existing SoC-level retargeting (already built, stays in faultflow)
Block's own fault-detecting pattern (generated via SAT ATPG against the block's own fault
list) -> remapped to SoC-level scan-chain offset, with sibling bypass, while preserving the
proof that the *same fault* is still detected at the new position. Coverage-preserving,
tightly coupled to faultflow's fault-tracking machinery. CLI precedent already exists:
`--export-patterns PATH` (export scan ATPG patterns as JSON) feeding a `retarget --patterns`
step. This is Stage 4/5 work, already validated on 4 real SoCs.

### 3b. IJTAG's own internal retargeting (new tool's job, does NOT need faultflow's algorithm)
Given an ICL-described network (SIBs, segments, instruments, possibly hierarchical
SIB-gating-SIB trees), compute the SIB-select bit sequence to route TDI/TDO to a named
instrument. This is IEEE 1687's own defined problem: a graph/tree traversal over the ICL
model, unrelated to fault detection or SAT ATPG. **Initial assessment that this needs
faultflow's retargeting algorithm was wrong** — for the "insert JTAG/TAP/IJTAG + check ICL
connectivity + emit synth-safe netlist" MVP, this is a self-contained, purely structural
problem. Standard IJTAG tools compute this directly from the ICL description.

Complexity risk here: **PDL's `existPr` conditions** — whether instrument A is reachable can
depend on the *current* state of other SIBs, so the network's active topology can change
dynamically. A naive implementation assuming a static path will break the moment two
instruments' access paths interact. Recommend scoping v1 to a flat/static SIB tree with no
dynamic `existPr` handling, expanding later.

### 3c. NEW — translating a faultflow-domain pattern into TAP-pin-protocol domain
This is the piece that was missing from the earlier discussion. faultflow generates patterns
in the **pseudo-PI/PO (scan chain) addressing domain**: "set chain bit K to V during Load,
observe bit J during Unload." That's a complete description of what needs to happen
electrically at the scan chain boundary. But if the chip's only external access to that chain
is through a TAP (not direct pins), the ATE cannot set "chain bit K" directly — it can only
drive TCK/TMS/TDI and read TDO. So a translation is required: faultflow's chain-bit pattern ->
the TAP instruction-select (Shift-IR) + Shift-DR bit sequence that produces the *same effect*
at the chain boundary.

This is structurally the same *kind* of problem as 3a (map a pattern from one addressing
domain to another while preserving correctness) — one hop further out: SoC-chain-offset
domain -> TAP-pin-protocol domain, instead of block-chain domain -> SoC-chain-offset domain.

**Where it lives**: needs both faultflow's pattern/vector semantics AND the TAP/IJTAG
network's addressing — not cleanly ownable by either side alone. Resolution: reuse the
*existing* interchange mechanism. faultflow already exports patterns
(`--export-patterns`/`retarget --patterns`); the new tool consumes that same exported JSON,
remaps it through its own ICL/TAP network model (instead of a SoC chain-offset table), and
emits the final TAP-pin-level sequence. No faultflow internals needed by the new tool — just
its existing pattern export format, extended to a new "target domain."

---

## 4. PDL verification — split by what "verification" means

- **Functional correctness of the retargeting itself** ("did my computed TDI/TMS sequence
  actually read/write the right value") — no fault model involved, golden-reference-style
  simulation (apply sequence, compare against PDL-declared expected value). This is the new
  tool's own responsibility — validating its own core output. Options: a narrow
  purpose-built simulator (the tool already knows the exact TAP+SIB structure it just
  inserted, so it doesn't need to be general-purpose), or depend on faultflow's existing
  golden-reference simulator as a library if that piece is exposed cleanly.
- **Fault coverage of the JTAG/IJTAG infrastructure itself** (stuck-at/transition faults in
  the TAP FSM or SIB muxes) — squarely faultflow's job. Once the new tool hands back a
  netlist with TAP/SIB logic inserted, it's just another netlist; feed it through faultflow's
  existing sim/ATPG unmodified, unless IJTAG-standard cells map to something not already in
  the Sky130/OSU035 cell maps.

**Conclusion: don't build a second fault simulator into the new tool.** Build/borrow a narrow
functional checker there; let faultflow own anything fault-coverage-shaped.

---

## 5. Pattern export format: STIL vs SVF

- **STIL** (IEEE 1450) is protocol-agnostic — "pin X = value Y at time T," no concept of a
  TAP state machine or instruction framing. It's a fine target for faultflow's own
  stuck-at/transition patterns (already a roadmap stub: `feature.md` lists `write_patterns`
  as a shell stub raising "not implemented"). It is NOT JTAG-aware — if TCK/TMS/TDI pin
  values in a STIL file happen to encode a correct TAP walk, playback works because the
  *sequence* is correct, not because STIL or the ATE understood JTAG semantics. Generic ATE
  has no independent JTAG protocol awareness unless using a dedicated JTAG/boundary-scan
  instrument board.
- **SVF** (Serial Vector Format) / **STAPL** ARE protocol-aware — described in TAP-native
  terms (`SIR` = shift instruction register, `SDR` = shift data register, `STATE` = go to TAP
  state X) rather than raw pin values. Dedicated JTAG programmers/boundary-scan tools consume
  these directly and handle low-level TMS sequencing from the higher-level statements.

**Recommendation**: the new tool's retargeting output (3c above) is a more natural fit for
SVF/STAPL than STIL, since PDL is already expressed in instrument-level operations close to
SVF's own vocabulary. STIL stays the right target specifically for faultflow's own
stuck-at/transition patterns, which don't involve TAP protocol framing at all. Same
underlying retargeted-sequence computation, two different serializations depending on
downstream consumer (ATE test program with mixed pattern types vs JTAG-native tooling).

---

## 6. Scan compression/decompression (faultflow-owned, not the new tool's concern)

Two separate problems on the scan-in/scan-out boundary, different difficulty:

### Decompressor (scan-in, K external channels -> N internal chains, N >> K) — hard
- **Combinational XOR broadcast** (fits entirely in existing `XOR2/XOR3/XOR4` GateTypes, no
  new sequential primitive): each internal chain's scan-in bit = XOR of a subset of the K
  external channels. Reachable internal-bit combinations form a K-dimensional linear subspace
  over GF(2) — not all 2^N patterns reachable, only those in the image of the linear map.
  ATPG patterns are usually >95% don't-care, so as long as the *care bits'* XOR-equations
  stay mutually consistent, a solution exists (Gaussian elimination check).
- **LFSR-based reseeding** (Mentor/Siemens EDT style): needs an actual shift-register-with-
  feedback-taps modeled across cycles — a genuinely new sequential primitive, bigger lift
  than the pure-XOR case.
- **Integration point**: fold the decompressor's logic into the same CNF native SAT ATPG
  already builds (`fault_solver.cpp`); make the *external* channels the free variables
  instead of internal scan-in bits; let CaDiCaL find external values that decompress
  correctly (or prove it can't, for that fault). Incremental extension of existing CNF
  builder for the pure-combinational case; LFSR reseeding needs multi-cycle CNF spanning
  evolving LFSR state, a bigger change.

### Compactor (scan-out, N internal chains -> K external channels) — easy to simulate, hard
to get exactly right
- XOR-tree or MISR downstream of chain outputs. Simulating it is trivial for faultflow's
  existing binary bit-parallel engine (just more gates); the compactor's output register
  becomes the new observable/PO set, same mechanism as `tp_nodes.json` today.
- Real risk: **aliasing** — two chains' effects cancelling in the same XOR group, masking a
  real fault. Industrial EDT tools reason about this *probabilistically* (residual risk ~
  2^-r for an r-bit MISR) because they lack a full simulator in the loop.
- **faultflow doesn't have that excuse**: it can simulate golden vs faulty through the exact
  compactor structure and compare signatures bit-for-bit, deterministically, per fault. This
  is a genuine differentiator — exact aliasing-aware coverage instead of a probability
  estimate. Worth highlighting if this ships.

### New policy question this raises
If a fault's required care-bit pattern is structurally unreachable through the decompressor
(linear system inconsistent), that must **not** be folded into "redundant" — Policy 2/3's
redundancy claim means no test exists at all, proven via UNSAT on the uncompressed problem.
Compression-unreachable-but-otherwise-detectable is a strictly weaker, different claim.
Conflating them breaks "coverage you can audit." Needs its own status category if pursued
(e.g. `excluded_compression_unreachable`, parallel to `excluded_clock`/`excluded_reset`).

### Implementation touchpoints (for later, not scoped/estimated yet)
- New NormalizedGraph concept: compression network as GATE nodes between top-level scan
  ports and per-chain scan cells (pure-XOR case: no new GateType; LFSR case: new
  NodeType/sequential primitive).
- ATPG CNF builder changes in `fault_solver.cpp` (which nets are "free PI-equivalent
  variables" changes when a decompressor is present).
- Observable-point config changes for compactor outputs (extends existing TP mechanism).
- `[scan]` config schema additions: compression topology (channel count, XOR fanout pattern
  or LFSR polynomial) as new fingerprinted fields (Policy 2).
- New coverage-report status category for compression-unreachable faults (Policy 3).

---

## 7. JTAG/IJTAG technicalities and known risk areas (for the new tool)

- **JTAG/TAP (1149.1)**: well-trodden. Fixed 16-state FSM (TMS/TCK-driven), standard
  instructions (EXTEST/INTEST/BYPASS/SAMPLE-PRELOAD/IDCODE), boundary-scan register insertion
  per BSDL cell types. Lots of prior open-source art to draw from.
- **IJTAG (1687)** is where complexity concentrates:
  - **ICL**: network graph (SIBs, instruments, hierarchical SIB trees). Parsing = build a
    graph, tractable.
  - **PDL**: procedural language, `existPr` conditions are the sharp edge (see 3b above).
- **Synthesis preservation is a real, already-seen-before risk.** Yosys's `opt_clean`/
  `opt_expr` will eliminate logic that looks functionally dead — a SIB mux whose select
  Yosys can't prove is ever driven non-constant at synthesis-analysis time is exactly the
  kind of thing that silently vanishes. faultflow already hit almost this exact bug (the
  "Wrappers that returned 0" lesson — IEEE 1500 WBR cells computing 0 in FUNCTIONAL mode
  because a case wasn't modeled). Mitigation: `(* keep *)`-style directives or a locked
  synthesis recipe, matching the discipline faultflow already uses for its own synth script
  (`docs plans` -- see CLAUDE.md's Yosys synthesis contract section for the pattern to copy).
- **Packaging reality check**: if the new tool shells out to Yosys for the synth-preserving
  script (it will need to), that's an external subprocess dependency pip can't bundle — same
  as faultflow's own Yosys usage. Not a blocker, just don't let "pip install" imply zero
  external setup.

### Recommended v1 scope for the new tool
Flat SIB network, static `existPr`-free PDL (iWrite/iRead only, no conditional reachability),
one instrument type first. Expand once that round-trips correctly end to end — same
incremental-cell-support philosophy faultflow used for Sky130 (cover what target circuits
need first, mark the rest as explicit deferred entries, don't try to boil the ocean day one).

---

## 8. Open questions / not yet decided

- ~~Exact naming for the new package~~ — **decided: `tapestry`**. See
  `tapestry_handoff.md` in this directory for the project charter.
- Whether to formalize the X-state/latches permanent-exclusion decision into
  `CLAUDE.md`/`feature.md`/`docs/roadmap.md` now or later (explicitly deferred this session
  — "keep it like that").
- Exact shape of the pattern-export JSON extension needed for 3c (TAP-domain retargeting) —
  does the existing `--export-patterns` format need new fields, or is it already sufficient
  as an opaque "here's the chain-bit pattern" payload the new tool re-addresses?
- New coverage-report status category naming/schema for compression-unreachable faults (6,
  "New policy question").
- Whether compaction/decompression is scoped for v2 at all, or is a separate v3+ item — this
  session covered the technical shape but did not commit it to any concrete milestone.

### Compression policy — open questions (expands on section 6's "New policy question")

- **Category name and semantics**: is "compression-unreachable" an `excluded_*` category
  (removed from the denominator entirely, like `excluded_clock`/`excluded_reset`) or a
  distinct `undetected` sub-reason (stays in the denominator, counts against coverage, but is
  tagged so it's distinguishable from "no vector found yet")? These have very different
  coverage-percent implications and need a deliberate choice, not a default.
- **Fingerprint/resume interaction**: should there be a `compression_model_id`, analogous to
  the existing `redundancy_model_id`, tied to the active compression topology (channel count,
  XOR fanout pattern or LFSR polynomial)? If the compression config changes, do previously
  "compression-unreachable" faults need to be invalidated and re-evaluated, the same way a
  fingerprint/model change already invalidates redundant classifications (Policy 2)?
- **Ordering relative to collapsing**: is compression-reachability computed before or after
  fault collapsing? Collapsing is about logical fault equivalence in the netlist, independent
  of the access mechanism — likely orthogonal and order shouldn't matter, but worth
  confirming rather than assuming, since collapsed faults are excluded from the denominator
  entirely (Policy 3) and a collapsed-away fault's reachability never gets evaluated.
- **Bypass/direct-access mode**: real EDT systems often provide a bypass path for a subset of
  chains, specifically to handle faults unreachable through compressed access. If faultflow
  ever models a bypass mode, "compression-unreachable" needs a precise definition — unreachable
  via *compressed* access specifically, with a separate (better) status for faults that are
  reachable via bypass. Not deciding whether to model bypass mode at all yet, just flagging
  that the category design should leave room for it rather than assume compressed-only access.
- **Aliasing scope**: faultflow's fault model is single-stuck-fault (one fault at a time, per
  existing architecture). Compactor aliasing is fundamentally a multi-fault-interaction risk
  (two simultaneous effects cancelling). Confirm this stays out of scope under the existing
  single-fault grading model rather than silently assumed — the "exact, deterministic
  aliasing-aware coverage" claim in section 6 is only about *this specific fault's* signature
  difference at the compactor, not about combinations of faults, and that scope limitation
  should be stated explicitly wherever this ships (paper, docs, or report schema).
