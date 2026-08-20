# warptap — implementation plan

**Naming note**: this project was planned under the working name "tapestry." The PyPI name
was unavailable (taken by an unrelated GPG backup tool), so the project is renamed
**`warptap`** — PyPI distribution name, Python package/import name, and CLI command are all
`warptap`. The two prior planning docs (`tapestry_handoff.md`, `jtag_ijtag_ecosystem_v2.md`)
keep the old working name in their filenames and prose as a historical record of the design
discussion that produced this plan — don't edit them to rename in place, just read "tapestry"
there as "warptap, before the name change." The on-disk repo directory (`tapestry`) can be
renamed to match whenever convenient; nothing in this plan depends on the directory name.

Status: **final — ready to execute**, with one external coordination call still outstanding
(§0, not something this plan can resolve unilaterally). Produced from a research pass (7
parallel research agents, ~380 primary sources: IEEE-adjacent tutorials, working-group PDFs,
academic papers, and every relevant open-source repo found) plus a direct audit of `openMBIST`
and `faultflow` on this machine. Implements the decisions in `tapestry_handoff.md` /
`jtag_ijtag_ecosystem_v2.md`, corrects a few facts that had drifted, and locks in two decisions
that were previously open (pre-synthesis insertion as the v1 default, §4; the `warptap` name,
above).

---

## 0. Still open: warptap vs. `tapwrap`

`openMBIST/docs/ijtag-handoff.md` (249 lines, written in a separate session, not previously
known to this project) plans a tool with the working name **`tapwrap`** that does *the same
job* warptap does — wrap a generated instrument's control/status ports into a TAP/IJTAG
network and emit ICL — for openMBIST's MBIST/BIRA/BISR output specifically. Its architecture
directly conflicts with warptap's:

| | warptap (this project) | tapwrap (openMBIST's plan) |
|---|---|---|
| Location | Separate pip package, separate repo | `src/tapwrap/` inside the openMBIST repo |
| Consumes | Any design + a spec/manifest | openMBIST's own `autombist.manifest.json` |
| Rationale | faultflow-precedent: don't absorb adjacent stages | "consumer inside producer isn't backwards when it's a one-way JSON boundary in the same repo" |

openMBIST's doc gives an explicit extraction trigger for *when* tapwrap should become a
separate consumer: *"a second independent producer of the manifest, or faultflow shipping a
consumer needing to pin a schema version independently."* warptap, as a general-purpose
JTAG/IJTAG inserter meant to also serve faultflow, arguably **already is** that second
producer/consumer. Three ways to resolve this, needs a call before real work starts:

1. **warptap absorbs the job**: openMBIST emits its manifest (Stage 0 of their plan, not yet
   built), warptap consumes it as one more input format alongside its own spec/manifest.
   `tapwrap` never gets built as separate code; openMBIST's doc gets a pointer to warptap.
2. **tapwrap stays and warptap ignores openMBIST**: warptap only ever targets designs with a
   warptap-native manifest; openMBIST integration (if any) happens on openMBIST's side,
   independently, possibly duplicating warptap's TAP/SIB/ICL logic.
3. **Split by layer**: tapwrap stays as openMBIST's thin generator-specific wrapper codegen,
   but calls into warptap's Python API for the actual TAP/SIB/ICL machinery instead of
   reimplementing it — same relationship warptap already has planned with faultflow (§
   "Integration mechanism" in `tapestry_handoff.md`).

Option 1 or 3 avoid building the ICL/PDL/TAP engine twice. Recommend picking one before
Stage 2 (TAP core) below, since it affects whether warptap's manifest-ingestion layer needs to
read openMBIST's schema natively. This is the one piece of this plan that needs a conversation
with whoever owns the openMBIST side, not just an internal decision.

**openMBIST's own critique of that manifest is worth inheriting regardless of which option is
picked**: its `diagnosis` block (`fail_valid`/`fail_addr`/`fail_bit` as port names) is
confirmed wrong against the actual RTL — `fail_bit` has zero occurrences in any `.sv`/`.v`
file, and `fail_valid` is an internal, single-cycle-combinational wire
(`rtl/march_c/march_c_fsm.sv:88-89`), not a synthesized, JTAG-capturable port. Whatever
consumes that manifest must not assume those fields are real until openMBIST's v1.1 revision
lands.

---

## 1. Facts corrected from the earlier planning docs

- **Project renamed to `warptap`** (was "tapestry" during planning — PyPI name unavailable,
  see naming note above).
- **IEEE 1687.1 does not exist as a ratified standard** — it's a PAR (project authorization
  request) only, no published syntax/semantics. Don't design against it or claim conformance.
- **IEEE 1149.1-2013 and IEEE 1687-2014 are both Inactive-Reserved** (as of 2024/2025, per
  openMBIST's audit), with open revisions in progress. Cite the specific edition, and note
  neither is currently "active" in IEEE's own status sense — doesn't change what to build, but
  changes what warptap can claim about standard conformance.
- **`existPr`'s literal grammar could not be verified from any accessible source** — the real
  text is paywalled inside IEEE 1687-2014. What *is* independently confirmed, from
  Baranowski/Kochte/Wunderlich's Reconfigurable Scan Network (RSN) formalization: dynamic
  reachability is provably a bounded-horizon **SAT problem** (their `Access(n)` CNF
  construction over per-cycle configuration-state variables), not an incremental extension of
  tree-walking retargeting. This validates — with an actual proof, not just intuition — the
  v1 decision to scope it out entirely rather than attempt a partial version.

---

## 2. Confirmed: there is no existing open-source insertion tool

Exhaustive search (GitHub, GitLab, PyPI, academic repos) found **no open-source tool that
inserts boundary-scan/IJTAG infrastructure into a netlist** — the exact problem warptap exists
to solve. What exists instead:

- **Closed/commercial**: XJTAG, Cadence, Synopsys DFTMAX, Siemens Tessent IJTAG — all solve
  this, all proprietary.
- **Runtime drivers** (drive an *already*-JTAG-equipped chain, not insertion): UrJTAG (dual
  GPLv2/MIT, stale since 2022), OpenOCD (GPL-2.0, actively maintained, most complete TAP/SVF
  implementation in existence), openFPGALoader (Apache-2.0, active), libxsvf (ISC, by the
  Yosys author — good semantic reference). None are usable as a Python dependency; all are
  candidates for an optional subprocess integration later, exactly like warptap's existing
  Yosys relationship.
- **Academic prototypes that stop at parsing/retargeting, never insertion**:
  [`Honza255/icl_parser`](https://github.com/Honza255/icl_parser) (MIT, Python, ANTLR4 grammar
  for ICL+PDL, working flat/hierarchical retargeter) is the single closest prior art to
  warptap's own IJTAG-retargeting core. Its own documented "not supported" list — local
  reset/update/capture, parked TMS, data registers, broadcast, dynamic `existPr` — is close to
  a checklist match for warptap's stated v1 exclusions. Treat as unpublished/unversioned
  (6 stars, no PyPI release): study `icl_retargeting.py`, `ijtag.py`, `icl.g4`/`pdl.g4` in
  depth, don't import it.
- **BSDL**: `cyrozap/python-bsdl-parser` (the historically-cited option) is archived and
  admittedly broken on modern Python — use its `bsdl.ebnf` as a grammar reference only.
  `chriesibaum/cb_bsdl_db` (Apache-2.0, actively maintained, on PyPI) is better-maintained but
  scoped around IDCODE→device lookup — verify it exposes full `BOUNDARY_REGISTER` structure
  before depending on it; if not, hand-roll a narrow parser for just the clauses warptap needs
  (`BOUNDARY_REGISTER`, `BOUNDARY_LENGTH`, `INSTRUCTION_OPCODE`, pin map).
- **SVF/STAPL writers**: nothing on PyPI. Only C/C++ *readers/players* exist anywhere
  (libxsvf, OpenOCD, UrJTAG's `svf.c`, Altera/Kontron STAPL players). warptap will be
  originating the first Python SVF/STAPL generator.
- **RTL/netlist insertion precedent that *does* exist**:
  [`AUCOHL/Fault`](https://github.com/AUCOHL/Fault) (Apache-2.0, Pyverilog-based). Its `Chain`
  tool builds a boundary-scan chain by walking a netlist's top-level ports, and a separate
  internal scan chain by walking its flip-flops; its `Tap` tool then wraps the result with a
  full JTAG TAP. Structurally the closest thing to warptap's own insertion job anywhere in
  open source — and it sidesteps the synthesis-preservation problem entirely by inserting
  post-synthesis, on an already gate-level netlist, so no further optimization pass ever runs
  over what it inserted. warptap deliberately does **not** follow that shape (§4) — worth
  rereading Fault's approach anyway, since its scan/chain-walking logic is still a close
  structural analog to warptap's own port-walking insertion pass.

This is a genuine gap warptap fills, not a wheel being reinvented — but it means most of the
engine has to be built from the standard/papers directly, with no library to lean on for the
hard parts (ICL/PDL parsing, SIB retargeting, SVF/STAPL emission).

---

## 3. Core data model

Four structures carry the whole design. Get these right before writing insertion logic.

### 3.1 BSR insertion model (IEEE 1149.1 side)
An ordered list of tuples, directly modeled on BSDL's own `BOUNDARY_REGISTER` attribute so it
doubles as warptap's future BSDL emitter/importer schema:

```
(cell_number, cell_type, port_name, function, safe_value, disable_cell_index)
# function ∈ {input, output3, control, internal, clock, bidir}
```

Built by walking the pre-synthesis netlist's top-level ports (see §4) in a deterministic
order. v1 cell-type coverage: **BC_1** (generic 3-stage: capture-mux, shift-FF,
update-latch — covers the majority of real BSDL usage) and **BC_7** (combined input/output
monitoring cell for bidirectional pins) plus a paired BC_2-style output-enable control cell.
Defer BC_4/8/9/10 explicitly (same "document limitations loudly" discipline as the rest of
v1's scope).

Hard correctness constraints to bake into the codegen/lint pass, not left implicit: Capture-IR
must load a fixed pattern whose two LSBs are `01` (mandated, lets external tooling
auto-detect IR alignment); if IDCODE is implemented, it must also be the default instruction
on reset/Capture-IR.

### 3.2 ICL model (IEEE 1687 side)
Two linked structures, not one:

1. **Physical shift-topology graph** — nodes are `ScanRegister`/SIB/`ScanMux` instances, edges
   come from `ScanInSource`/`Source` fields, terminating at `AccessLink` (the TAP binding).
   Used to compute bit offsets during retargeting.
2. **Module-instantiation tree** — used for dotted addressing (`Chip.SIB1.TDR1.Sensor1`),
   matching PDL's own `iCall`/`iTarget` addressing convention.

A SIB is modeled exactly as the standard's own construction:
```
ScanRegister sr { ScanInSource mux1; ... }
ScanMux mux1 sr { 1'b0: si; 1'b1: fso; }
```
— a 1-bit shift/update register plus a 2:1 mux gating "bypass" vs. "insert nested segment."

No verbatim IEEE 1687-2014 grammar was accessible (paywalled); the ICL syntax above is the
**converged intersection** of three independent secondary sources (Rearick's 2009 pre-standard
proposal, Zadegan et al.'s 2012 tutorial paper, and a 2017 Siemens/Mentor conference deck using
finalized syntax) — treat as de facto reference pending direct access to the standard text.

### 3.3 PDL execution model
`iWrite`/`iRead`/`iRunLoop` are non-executing setup commands mutating a pending-writes dict
keyed by ICL port/register name, scoped by the current `iTarget`/`iScope`. `iApply` is the
**sole** action command: it resolves the unique static SIB-ancestor path to the addressed
instrument via the topology graph (§3.2.1), computes prefix/payload/suffix bit lengths
(including *sibling* segment widths at each hierarchy level — an open SIB exposes everything
at that level, not just the target branch), and emits a two-phase Capture-Shift-Update
sequence: phase 1 asserts every on-path SIB while others stay deasserted, phase 2 shifts the
payload while re-asserting the same select bits.

Even though v1 only ever resolves a static default configuration, give the path-resolution
function an explicit `network_configuration` parameter now — this is what lets a future
SAT-based `existPr` extension (§1, dynamic reachability) slot in later without breaking the
PDL-command-layer API.

### 3.4 TAP-transaction IR (shared by all pattern-format backends)
One internal representation, independent of output format:
```
ShiftIR(bits, tdi: int, tdo: int|None, mask: int|None)
ShiftDR(bits, tdi, tdo, mask)
GotoState(state)
Runtest(count|time, run_state, end_state)
```
Represent scan data as Python arbitrary-precision `int` with explicit `(value, width, mask)`,
not bit-lists — this is what both SVF and STAPL's LSB-first hex conventions want, and reduces
header+payload+trailer composition (from the SIB-tree retargeting result) to shift/OR/mask
arithmetic. SVF and STAPL backends become **stateless pretty-printers over this same IR** —
retargeting logic is never duplicated per output format. This is the single biggest
implementation-simplification finding from this research pass: SVF's `SIR`/`SDR`, STAPL's
`IRSCAN`/`DRSCAN`, and even STIL (with an added FSM bit-banger, see §6) all reduce to walking
the same list.

---

## 4. RTL/netlist insertion architecture

**Decision: pre-synthesis RTL insertion is the v1 default** — matches the original "into the
netlist/RTL" framing from `tapestry_handoff.md`. (An earlier draft of this plan proposed
defaulting to post-synthesis/"late" insertion, mirroring `Fault`'s approach of operating on an
already-gate-level netlist to sidestep dead-code-elimination by construction — that's rejected;
warptap targets RTL/pre-synthesis designs as the primary case. §4.3's synthesis-preservation
discipline is therefore load-bearing, not a fallback for an opt-in mode — get it right, since
it's what stands between the inserted logic and Yosys's optimizer on every real run.)

### 4.1 Pipeline shape
```
Yosys (ingest)  →  pure-Python JSON graph surgery  →  Yosys (full synth, keep-tagged logic preserved)
```
Two Yosys subprocess calls bracket, not eliminate, the pure-Python core:
1. **Ingest**: `read_verilog; hierarchy -top <top>; proc; memory_collect; write_json` — this
   step is *mandatory*, not optional: `write_json` rejects modules that still contain unlowered
   behavioral processes (`$proc`/`always` blocks), so there's no way to hand raw RTL text to
   pure Python without one Yosys pass first.
2. **Surgery**: all TAP/BSR/SIB insertion happens as pure-Python edits to the JSON dict, with
   `(* keep *)`/`(* blackbox *)` attributes attached at insertion time (§4.3).
3. **Full synth**: a second Yosys invocation runs the design's normal synthesis script
   (whatever the target flow already uses) plus warptap's own locked/preserving script
   fragment — the inserted logic goes through the same optimization pass as everything else,
   protected by the §4.3 discipline rather than by never being exposed to it.

### 4.2 Yosys JSON schema (what the surgery pass actually edits)
`{modules: {<name>: {attributes, ports, cells, memories, netnames}}}`. Every module has its
**own flat namespace of integer bit IDs** — every port/cell connection references bits by
plain JSON integers, unique per module; a constant bit is instead the *string* `"0"`/`"1"`/
`"x"`/`"z"` (never collides with a real integer ID). To insert a wire: allocate unused integers
above the current max and thread them through new cells' `connections`. Do **not** hand-edit
this nested structure ad hoc — build a thin object-model layer first (mirroring
[`SpyDrNet`](https://github.com/byuccl/spydrnet)'s `Netlist`/`Module`/`Cell`/`Port`/`Net`
shape, BSD-3, mature but not itself Yosys-JSON-aware) with primitives like `alloc_bit(module)`,
`new_wire(module, width)`, `add_cell(module, type, attrs, port_conn)`, `connect(cell, port,
bits)`.

For the TAP controller and SIB cell primitives themselves: hand-author them once as minimal
Verilog, run `read_verilog; proc; write_json` **once** to capture a JSON "template" fragment,
then clone/rename/re-ID that fragment per SIB/instrument in pure Python. This avoids ever
needing a general Verilog parser (Pyverilog/hdlConvertor) or an HDL-authoring framework
(amaranth/migen — both are for *building new designs*, not editing arbitrary existing
netlists, so neither fits the core insertion job; migen's `litex/soc/cores/jtag.py` is still
worth reading for its `Instance()`-blackbox + explicit-`keep` patterns, see §4.3).

### 4.3 Synthesis preservation (load-bearing for every v1 run — this is what protects the
inserted logic through the design's real synthesis pass)
- Apply `(* keep *)` at the **wire** level on every TAP/SIB state-holding signal — Yosys's own
  documented idiom for creating checkpoints that survive ABC technology mapping, when applied
  to flip-flop *output wires* specifically.
- `keep_hierarchy` is not enough on its own: it only blocks the `flatten` pass, **not** interior
  `opt_expr`/`opt_clean`, and does **not** block `opt_hier`'s cross-module constant/tie
  propagation — a "protected" module can still be gutted if any control input (TMS/TCK/TDI/
  select/shift/capture/update) is provably constant from outside. Never tie any of these to a
  parameter/localparam anywhere in the flow, including in generated wrapper defaults — wire
  them end-to-end to genuine top-level ports.
- `opt_merge` is a *distinct* hazard from deletion, documented in
  [yosys#855](https://github.com/YosysHQ/yosys/issues/855): structurally-identical repeated
  cells (a bank of SIBs) can be silently collapsed into one shared instance once
  technology-remapping changes cell identity, even with `keep` set. Mitigation: keep-tag every
  SIB individually **and** add a post-synthesis structural check that counts instantiated
  TAP/SIB cells against the expected count from the insertion spec — fail loudly on mismatch,
  don't trust attributes alone.
- Ordinary `opt`/`opt_clean` never delete a module's declared **ports** (only the separate
  `rmports` pass does) — so top-level TDI/TDO/TCK/TMS survive by construction, but that
  protects nothing behind them.
- Shielding the TAP FSM's internal encoding from ABC's remapping: mark `(* blackbox *)` during
  RTL-facing insertion and merge in the real gate-level implementation at the JSON level after
  the rest of the design is optimized — the same pattern LiteX/Migen use for vendor JTAG
  primitives (BSCANE2 etc., wired as opaque `Instance()` blocks).
- Add a golden-file round-trip regression test **before** writing any real insertion logic:
  `write_json` of an unmodified design → warptap no-op pass → `read_json` → `write_verilog`,
  diffed against reference. The schema has real edge cases (behavioral-process rejection,
  `$mem` representation, per-module bit-ID scoping) that fail silently if violated.

---

## 5. Interfaces to build against (concrete, from direct repo audit)

### 5.1 faultflow — real today, usable now
- **Pattern export** (`sim --scan --export-patterns PATH`, requires `--scan`): flat JSON array,
  each element `{load_seqs: {"<chain>": [bool...]}, capture_pi_values: {<port>: bool},
  expected_unload: {"<chain>": [bool...]}}` (`faultflow/scan/pattern_export.py`). This is
  warptap's input for retargeting-problem-3c (translate a faultflow chain-bit pattern into a
  TAP-pin-protocol sequence).
- **`faultflow_soc_access_v1`** (from `retarget --soc-access`): chain topology + a
  `wrapper_instruction` map (`{block: "INTEST"|"BYPASS"}`) — the closest existing model for a
  TAP-side "which segment is currently selected" manifest; mirror its shape rather than
  inventing a new one.
- **Cell-map JSON** (`cells/sky130/*.json`, `cells/osu/*.json`): `node_type` ∈
  `{GATE,FF,LATCH,CONST,TBUF,ICG}`, `gate_type` (free string, must match the real C++
  `GateType` enum in `src/core/common/types.hpp`), ordered `inputs`, `outputs`, `ff` block. A
  plain non-inverting 2:1 SIB-select mux maps cleanly onto the existing `MUX2_NI` GateType
  (`S ? A1 : A0`); pick the wrong one of `MUX2`/`MUX2_NI`/`MUX2I` and SIB select polarity
  silently inverts. The TAP FSM itself has **no ready-made primitive** in either cell map —
  synthesize it down to existing INV/AND/OR/MUX/DFF primitives, or follow faultflow's own
  precedent for its injected WBR cells (`$wbc_in_faultflow` etc. — `$name`/`\$name` pairs
  carrying a custom metadata sub-object) if warptap wants faultflow to reason about TAP state
  directly.
- **Golden-reference simulator, reusable as a library**: `simulate_scan_pattern()`
  (pybind11-exposed, `src/core/bindings/python_bindings.cpp:1075-1086`) drives a full
  shift-load/capture/shift-unload sequence functionally, no fault model involved — plausibly
  reusable for warptap's own PDL-verification milestone, provided warptap pre-computes the TAP
  FSM's TMS-driven state sequencing itself (this function only has a flat shift-enable
  concept, no native TMS awareness).
- **OpenTestability precedent** (`faultflow/testpoint/opentest_driver.py`,
  `faultflow/service/oracle.py`): subprocess + JSON/stdout report, no daemon, no shared memory
  — the integration shape warptap's own external-tool boundaries (e.g. a future OpenOCD
  hardware-in-the-loop check) should copy.

### 5.2 openMBIST — not yet real (see §0)
Nothing consumable exists today: no manifest code, no `tapwrap` code (verified by directory
search). `autombist.manifest.json` v1.0 is documented but unbuilt, and its `diagnosis` block
is known-wrong. Until openMBIST's Stage 0 lands (or the §0 decision routes around it),
warptap's only options for openMBIST-generated designs are (a) wait, or (b) reverse-engineer
structure directly from the generated `.sv` / `config.yml`.

---

## 6. Pattern export formats — confirmed easier than expected

Read in full from primary sources (ASSET InterTech SVF spec Rev E; JEDEC JESD71 STAPL; STIL
reference guide), not from memory. Confirms the original SVF-over-STIL reasoning from
`tapestry_handoff.md`, with one added simplification:

- **SVF and STAPL expose near-identical JTAG primitives** despite STAPL being a real
  procedural language: `SIR`/`SDR` = `IRSCAN`/`DRSCAN`, `STATE` = `STATE`,
  `HDR`/`HIR`/`TDR`/`TIR` = `PREDR`/`PREIR`/`POSTDR`/`POSTIR`. STAPL's control-flow machinery
  (`FOR`/`IF`/`CALL`/`PUSH`/`POP`) is **entirely optional** — a single-`ACTION`/
  single-`PROCEDURE` file with an unrolled, straight-line statement list is fully spec-legal
  STAPL. So both backends can be stateless pretty-printers over the one TAP-transaction IR
  (§3.4), differing only in keyword spelling and a fixed boilerplate skeleton.
- **STAPL's one genuinely fiddly piece**: a CRC16-CCITT checksum (poly mask `0x8408`, seed
  `0xFFFF`, complemented, computed over the whole file body up to the CRC statement) is
  mandatory. JESD71 Annex B gives the algorithm verbatim (~15 lines of C) and Annex A gives a
  full worked example with expected CRC `3759` — implement once as a pure function and
  unit-test directly against that oracle.
- **STIL has no JTAG-aware primitive at all** — it's a generic per-pin, per-cycle waveform
  language. Emitting it requires warptap to run its own TAP-FSM bit-banger (expand every
  retargeted op into explicit per-TCK-cycle TMS/TDI/TDO triples) and dump the trace as
  `Pattern` V-statements under a `WaveformTable`. Confirms STIL is legitimately secondary —
  keep it out of v1 scope entirely unless a concrete downstream need appears (matching the
  existing "offer STIL only if..." framing), and if it's ever added, build it as a lowering
  pass over the *same* internal FSM simulator warptap needs anyway for self-verification, not
  a separate code path.
- **No parser needed in v1** for any of the three — validate emitted output by shelling out to
  an existing consumer (OpenOCD's `svf` command, a STAPL/Jam player) rather than round-tripping
  through a self-written parser. Matches warptap's stated scope: it emits patterns, it doesn't
  consume them.

---

## 7. Phased plan

Numbered to track dependencies, not calendar time — no estimates given here since engineering
velocity is unknown for this specific team; openMBIST's own 9-stage `tapwrap` plan (for a
narrower, single-instrument-type scope) quoted 76–108 engineer-days with a "scope-realism
critique" revising that to 130–185, which is at least a size signal for a comparably-scoped
effort.

**Stage 1 — Ingest/surgery/re-synth pipeline skeleton.** Yosys ingest invocation (§4.1 step 1)
+ thin JSON object-model layer (§4.2) + golden-file no-op round-trip test (§4.3, last bullet).
No JTAG logic yet — this is pure plumbing, and it's the thing every later stage depends on.
Ship the pipeline against a trivial fixture design before touching real JTAG structures.

**Stage 2 — TAP FSM.** Table-driven 16-state transition function, written *once* and shared
between (a) the RTL/gate-level codegen backend and (b) an internal Python behavioral model used
for later PDL verification — a single canonical table avoids the two drifting apart (an
easy bug class: e.g. Exit2-DR/IR with TMS=0 returns to Shift, not Capture). Mandatory
instructions only: BYPASS, SAMPLE/PRELOAD, EXTEST, IDCODE if included (with the
default-on-reset rule enforced).

**Stage 3 — BSR insertion.** Walk top-level ports (pre-synthesis netlist per §4) to build the
`(cell_number, cell_type, port_name, function, safe_value, disable_cell_index)` list (§3.1),
stitch BC_1/BC_7 cells, wire into the TAP's DR mux. First real end-to-end proof: insert into a
trivial design, verify via the structural cell-count check (§4.3) that synthesis didn't eat
anything.

**Stage 4 — ICL model + SIB primitive.** Physical shift-topology graph + module-instantiation
tree (§3.2). Hand-authored SIB Verilog template, cloned per instance (§4.2). Flat/static
network only — no nested SIB-gating-SIB trees yet (confirmed-correct scoping per §1's SAT
finding).

**Stage 5 — PDL interpreter + static retargeting.** `iWrite`/`iRead`/`iApply` (§3.3), unique
ancestor-chain walk, two-phase CSU sequence emission. Validate against
[`gitlab.com/IJTAG/benchmarks`](https://gitlab.com/IJTAG/benchmarks) (confirm redistribution
license before vendoring fixtures) — start with its "Basic" category, which should match v1's
flat-tree scope. Note: this is also where the §0 decision needs to be resolved, since it
determines whether the manifest/spec-ingestion layer needs to speak openMBIST's schema.

**Stage 6 — ICL connectivity check.** The "over-shifting" technique, attributed to Dr. Martin
Keim (Mentor Graphics/Siemens EDA), "Automated Debugging of IJTAG Networks," Nordic Test
Forum, Nov 2017 (a vendor conference-talk deck, not the IEEE 1687 standard text or a
peer-reviewed paper — the only source found using this exact technique by name; the earlier
`(§ ICL self-check)` cross-reference in this paragraph was dangling and has been removed):
flush a bit pattern longer than the currently-configured scan-path length through TDI, verify
arrival against ICL-declared register widths at TDO. warptap's implementation
(`src/warptap/sib_overshift.py`) upgrades this from Keim's arrival-timing-only comparison to
exact bit-for-bit stream comparison against a fresh `TapModel`/`SibNetworkRegister` oracle,
since warptap already owns a full behavioral model — stronger (also catches content-only
defects), but empirically found to change what `margin` guarantees: with an exact-comparison
oracle, most structural defects are caught even at `margin=0`, and margin's real role is
improving *reliability* against defects whose content happens to coincidentally alias at a
given probe length, not providing a hard `margin >= delta` guarantee the way Keim's own
narrower technique implies (see `sib_overshift.py`'s module docstring and
`tests/test_sib_overshift.py`'s margin-sweep tests for the full, execution-verified finding).
Cheap, simulator-free — do this before investing in a full functional simulator.

**Stage 7 — TAP-transaction IR + SVF/STAPL emitters.** §3.4 + §6. SVF first (simplest: always
emit every field, no sticky-default optimization yet). STAPL second, reusing the same IR walk.
Validate by shelling out to an existing player (OpenOCD `svf`, etc.), not a self-written
parser. **Live OpenOCD validation done** (`svf_openocd_check.py`,
`tests/test_svf_openocd_validation.py`) — real ops from `PDLInterpreter` (SIR/SDR/RUNTEST,
both write-only and read/TDO-MASK) run through a real `openocd svf` command against the
`dummy` adapter driver (a software-only JTAG interface, no hardware needed, so no chain
auto-probe noise once one TAP is explicitly declared). This validates grammar, not content:
the dummy driver has no real chip, so a TDO/MASK op reports a content ("tdo check error")
mismatch, but OpenOCD's own independently-decoded `WANT`/`MASK` hex values match warptap's
emitted literals exactly, confirmed byte-for-byte. **STAPL's `COMPARE` clause done** (was a
documented scope gap; `to_stapl()` now emits it), and **live STAPL validation done** too, via
an independently-built copy of Altera's real "Jam STAPL Player" reference C interpreter
(`jamplayer_check.py`, `tests/test_stapl_jamplayer_validation.py`) — no off-the-shelf package
exists for this the way OpenOCD's does, so it's a from-source build (`PORT UNIX`, which makes
the player's own real-hardware JTAG I/O function fall through to a built-in `tdo = 0`
software-only stub with zero code changes, mirroring OpenOCD's `dummy` driver); see
`jamplayer_check.py`'s module docstring for the exact build steps. **This live validation
caught a real, previously-shipped bug**: `to_stapl()`'s `CRC` statement was computed one
newline byte short of what JESD71 Annex B (and the reference player's own `jamcrc.c`)
require — self-consistent against this project's own prior unit tests (which re-derived the
same wrong boundary independently rather than checking it against ground truth), but a real
`CRC mismatch` against the reference player until fixed. The `COMPARE` clause's own grammar
(`DRSCAN`/`IRSCAN <length>, <data> [,COMPARE <compare>,<mask>,<result>];`, a previously-declared
scalar `BOOLEAN` result variable, no separate `MASK` keyword) was confirmed by five
independently-converging sources: the JESD71 spec text itself, real vendor-generated `.jam`/
`.stp` files (Altera, Microsemi), and two independent copies of that same reference player's
source. Because `COMPARE` doesn't abort STAPL execution by itself (unlike SVF's `TDO`/`MASK`,
which a real player enforces automatically), `to_stapl()` also emits an enforcement
`IF <result> == 0 THEN EXIT (1);` after each `COMPARE` — proven at runtime against the real
player, both the passing and the enforcement-firing path.

**Stage 8 (v1.x, gated on §0 and on faultflow demand) — Pattern retargeting (problem 3c).**
Consume faultflow's `--export-patterns` JSON (§5.1), remap through warptap's own ICL/TAP
network model instead of a SoC chain-offset table, emit via the Stage 7 backends. This is
where the faultflow-side optional-dependency wrapper (`ff.py jtag`/`ijtag` subcommands,
already speced in `tapestry_handoff.md`) becomes real.

**Stage 9 (built) — real functional instruments + PDL functional verification.** Superseded
both options originally sketched above once actually built, per an explicit user decision to
verify against real observed behavior on a real external design
(`openMBIST/flow/multimem/mbist/mem_subsystem_mbist.sv`) rather than warptap's own predictions.
Two parts, deliberately separate:

- A genuinely new RTL primitive, `rtl/instrument_write.v` — a real (non-stub) write-target
  TDR cell with an update latch permanently wired to a real host signal (`icl_model.
  InstrumentDirection`/`SignalBinding`, `sib_insert.py`'s direction-branched wiring). Discovered
  during implementation: the naive gate ("commit only while the gating SIB is open," i.e. its
  `po`) is insufficient — phase 1's "don't-care" 0-fill (`sib_layout.compose_bits`, safe only
  for a cell with no update latch) would still clobber a WRITE instrument on the very edge that
  opens *or* closes its SIB. The fix redefines `sib_cell.v`'s previously-unconsumed
  `nested_select` as `po & shift_ff` (open both before *and* after the edge) — the correct,
  general form its own "future nested-SIB use" framing anticipated. A narrower, documented v1
  limitation remains: retargeting to the *same* still-open WRITE instrument twice with no
  intervening different target clobbers it (see `pdl_interpreter.py`'s `iApply` docstring).
- `pdl_verify.py` (`check_reads`/`correlate_observed`): compares a real Icarus-observed TDO
  trace against `PDLInterpreter`'s own PDL-declared `iRead` expectations — no fault model, no
  second Python-side functional prediction. Proven both generically (`real_signal.v`, Tier 1)
  and against the real target (`mem_subsystem_mbist`, Tier 2: write `self_repair_start`, confirm
  `self_repair_busy` reads back 1 from the real march-C/on-chip-BISR FSM).

**Explicitly not in this plan** (matches existing v1 scope, reconfirmed by this research):
nested/hierarchical SIB trees, dynamic `existPr` reachability, STIL emission, hierarchical
multi-instrument networks, any conformance claim against a specific IEEE standard edition.

---

## 8. Testing/validation strategy

- **Golden-file round-trip** (Yosys JSON in/out, no-op) before any insertion logic — catches
  schema edge cases early (§4.3).
- **Structural post-synth cell-count check** — count TAP/SIB cells by marker attribute after
  synthesis, fail loudly on mismatch (guards against both deletion and `opt_merge` collapse).
- **TAP FSM table cross-check** against at least two independent references (OpenCores
  `tap_top.v`, one other) — off-by-one edge transitions are the realistic bug class here.
- **ICL/PDL conformance** against `gitlab.com/IJTAG/benchmarks`' Basic category.
- **STAPL CRC16** unit-tested directly against JESD71 Annex A's worked example (expected CRC
  `3759`) — a free, spec-provided oracle.
- **SVF/STAPL emission** validated by feeding output through an existing consumer (OpenOCD
  `svf` command for SVF, an independently-built copy of Altera's real Jam STAPL Player
  reference interpreter for STAPL), not a self-written parser. **Done for both** (Stage 7) —
  the STAPL live validation caught a real CRC-boundary bug this project's own unit tests had
  missed (see Stage 7's own writeup above).
- **PDL functional verification** (Stage 9): replay a retargeted sequence, compare against the
  PDL-declared expected value — no fault model involved, this is warptap checking its own
  output, not faultflow's job. Built as `pdl_verify.check_reads()`, proven against a real
  external design's real FSM behavior (`mem_subsystem_mbist`'s self-repair controller), not
  just warptap's own stub predictions.

---

## 9. Packaging

- Yosys stays an external subprocess dependency (same as faultflow's own Yosys usage) —
  document clearly, `pip install` cannot bundle it.
- **PyPI distribution name: `warptap`** (decided, §1). Python import name and CLI command match
  (`import warptap`, `warptap <subcommand>`). GitHub repo directory can stay `tapestry` or be
  renamed to match — cosmetic, doesn't block anything in this plan.
- faultflow integration stays optional/lazily-imported per `tapestry_handoff.md`'s existing
  plan — nothing in this research changes that mechanism, only confirms concrete data shapes
  to build the wrapper against (§5.1).

---

## 10. Open questions carried forward

From the existing docs, still unresolved:
- Exact shape of faultflow's pattern-export JSON extension needed for Stage 8 — current
  `--export-patterns` format (§5.1) may already be sufficient as an opaque payload; confirm
  once Stage 8 is actually being built.
- Whether/when to formalize faultflow's own X-state/latches exclusions in its `CLAUDE.md` —
  not warptap's call, listed only so it isn't lost.

From this research pass, still open:
- **§0**: warptap vs. `tapwrap` architecture — needs an external decision (coordination with
  openMBIST), not just internal awareness. The one blocker on this plan being fully
  actionable end to end.
- Whether `chriesibaum/cb_bsdl_db` exposes full `BOUNDARY_REGISTER` structure or only
  IDCODE-level lookup (§2) — check before deciding to depend on it vs. hand-rolling a BSDL
  parser.
- License/redistribution terms for `gitlab.com/IJTAG/benchmarks` before vendoring fixtures
  into warptap's own test suite (§7 Stage 5).
- `gitlab.com/IJTAG` and TIMA Grenoble's `mast-opensource` project were both blocked by
  bot-protection during this research pass and could plausibly be the most substantial open
  academic IJTAG toolchain in existence — worth a manual (non-automated) follow-up visit
  before assuming they're dead ends.

Decided, no longer open:
- ~~Post-synthesis vs. pre-synthesis insertion default~~ → pre-synthesis RTL insertion (§4).
- ~~PyPI/package name~~ → `warptap` (§1).
