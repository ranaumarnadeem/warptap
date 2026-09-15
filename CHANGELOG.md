# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/). No version has been
cut yet (`__version__` is still `0.0.1`) — everything below lives under `[Unreleased]`, one
entry per implementation stage.

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
  `PDLInterpreter.history` record; originally validated self-consistently only (no independent
  PDL parser existed anywhere), later given real independent grammar validation by compiling
  the vendored `icl_parser`'s own bundled-but-never-wired PDL grammar — see "PDL grammar
  validation" below.
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
- **Stage 15 — Field/alias addressing.** Real PDL/ICL named sub-field addressing (`Alias`)
  inside `iWrite`/`iRead`, letting a caller target a bit-range within an instrument by name
  instead of the whole register; `iRunLoop`'s `-sck` selector for pulsing a real system-clock
  port independent of TCK's own timing domain.
- **Library-quality pass.** A shared `WarptapError` exception base (every specific error now
  subclasses it, fully backward compatible), a PEP 561 `py.typed` marker, a curated top-level
  `warptap` public API (`warptap/__init__.py`), and `insert_test_access()` — a single
  convenience function for the ingest → build-network → insert → re-emit sequence every
  cross-sim test previously hand-rolled.
- **Nested/hierarchical SIB networks.** A SIB gating a nested sub-network (not just a leaf
  instrument) at arbitrary depth — layout/retargeting, the Python cross-sim oracle, real RTL
  insertion, PDL retargeting, and ICL emit/import all generalized to recurse; validated end to
  end with a 3-level-deep integration scenario against real RTL and the vendored ICL parser.
- **Multi-arm ScanMux.** Real IEEE 1687 N-arm, multi-bit-select `ScanMux` support (not just the
  binary self-select SIB case) — a new RTL primitive, per-instance-parameterized Yosys
  insertion, a genuine direct arm-to-arm switch in PDL retargeting, and ICL emit/import;
  validated end to end including a network mixing SIB-nests-mux and mux-nests-SIB in one
  real-RTL-validated network.
- **PDL grammar validation.** The vendored `icl_parser` submodule bundled a real PDL grammar
  (`pdl_parser/pdl.g4`, a "PDL0 grammar v20130806") that had never been compiled into a working
  parser — fixed 4 real bugs in it (an unterminated rule, an undefined rule reference, two rule
  names colliding with Python builtins, a missing token) and compiled it via ANTLR 4.7.2 (fix
  landed in the vendored fork, upstream PR opened). Immediately caught a real bug in Stage 11's
  own PDL emission: `-sck` was rendered as a bare flag with no port name, which the real
  grammar requires — now renders `-sck <port>`. Proves PDL emission is valid *syntax*, not
  retargeting-*semantics* correctness (confirmed separately that the vendored parser's own
  retargeting engine doesn't support the register kind a WRITE instrument uses, so it can't
  serve as an oracle for that either).
- **Fixed: width>1 instrument/ScanMux emission used a non-standard scan-port convention.**
  `render_instrument_module()` (since Stage 10) and `render_scan_mux_module()` (this session's
  own ScanMux work) both widened their own `ScanInPort`/`ScanOutPort`/`ScanInSource` to match
  their underlying register's width. Confirmed real (checked every `ScanRegister`-sourced
  `ScanOutPort`/`ScanInSource` in the vendored `icl_parser`'s own fixture corpus, 5-for-5 plus
  all 29 `ScanInSource` clauses, zero exceptions — including a 32-bit `IDCODE` register fed by
  a 1-bit source): real ICL keeps these ports scalar always, letting the register's own
  internal shift chain move other bits into position — a real upstream maintainer's own PR
  review caught this, not this project's own live-validation (the vendored checker's own
  `port_size == source_size` assertion is too permissive to catch it). Fixed to source exactly
  one bit each, confirmed against each construct's own real RTL wiring rather than assumed by
  convention (the instrument case sources the MSB, matching `sib_insert.py`'s own last-chained-
  cell; the ScanMux case sources bit 0, matching `scan_mux_cell.v`'s own `shift_ff[0]` — two
  different real answers, not one rule). `CaptureSource`/`WriteDataSource`/`DataOutPort`
  (parallel, functional-level connections, not the bit-serial scan path) were already correct
  and are unaffected. This also resolved the retargeting-graph crash a since-questioned
  upstream PR had been working around — confirmed the crash never happens for genuinely
  standard-shaped ICL, only for the non-standard wide-port shape this fix removes.
- **PDL import.** `to_pdl()`'s own natural companion, the "other direction" `icl_import.py`
  already plays for ICL — but procedural, not declarative: replays real PDL text against a
  live `PDLInterpreter`'s own public `iTarget`/`iWrite`/`iRead`/`iApply`/`iRunLoop` methods, in
  order, rather than reconstructing a new object. `iTarget` is hand-recognized (the compiled
  PDL0 grammar has no such construct at all, see "PDL grammar validation" above); every other
  supported statement is genuinely grammar-parsed, walking the real ANTLR parse tree. v1 scope
  is the 5 statement kinds `to_pdl()` actually emits — everything else the real grammar
  recognizes (`iProc`/`iCall`, `iScan`, `iState`, etc.) is rejected with a specific, named
  `PdlImportError`, matching `icl_import.py`'s own discipline. Round-trips real, multi-
  statement histories (write, settle via both `-tck` and `-sck`, read, write again; alias-
  addressed fields) cleanly on the first pass.
- **`OneHotDataGroup` (address-decoded register-file bus).** Closes the "indirect/paged
  addressing" gap — but with real ICL's own genuinely different shape, not the originally-
  assumed JTAG-window-into-memory pattern (that pattern turns out to need no new ICL vocabulary
  at all). `OneHotDataGroup` is a parallel, non-scan, address-decoded register-file bus: a
  bounded set of individually-addressed `DataRegister`s sharing one `AddressPort`/`WriteEnPort`/
  `ReadEnPort`/`DataInPort`/`DataOutPort` quintet. Scope is ICL emit/import only — never touches
  the scan chain, RTL insertion, or retargeting. No real fixture exists anywhere in the vendored
  `icl_parser`'s own test corpus for this construct, unlike every other ICL feature this project
  has built — closed with a permanent, gating live-validation spike before any production code,
  confirming (among other things) that exactly one `OneHotDataGroup` per Module is a hard v1
  constraint, and that `writable`/`readable` are a whole-group property in real ICL, not a
  per-register one (an early design draft got this wrong, caught the same way — a live-rendered
  example checked against the real vendored `Ijtag`, not just reasoned from the grammar). Live-
  validated at both this project's usual tiers (pure-Python shape assertions, and the real
  vendored checker with `build_register_model=False`, required because this construct is
  scan-free by construction and the checker's default crashes unconditionally for that shape),
  plus a full emit→import round trip.
- **Retargeting shift-length optimization.** `sib_retarget.stage_open_sequence` never looked at
  what's already open — every `PDLInterpreter.iApply` call recomputed a full cold-start staged-
  opening sequence sized to the new target's own tree depth, even when a prefix of that path was
  already open from the previous call. Confirmed empirically: two sibling instruments 2 levels
  deep under a shared hierarchy, targeted back to back, used to cost 4+4 total `ShiftDR` rounds —
  now 4+2, since the shared ancestor prefix is reused. Adds a keyword-only `currently_open`
  parameter, defaulting to reproduce every existing caller's exact output; wired into both real
  callers (`PDLInterpreter.iApply` and `sib_overshift.build_overshift_ops`). As a direct, proven
  side effect, also fixes a previously-documented v1 footgun: retargeting to the exact same
  still-open WRITE instrument twice in a row (no intervening different target) used to clobber
  the value via a redundant round's own zero-fill commit — confirmed as a real bug on real RTL
  before this fix, now a permanent passing regression test. An earlier memory entry had
  mischaracterized this gap as needing "Keim's ring-by-ring hierarchical search" — that
  technique (real primary source: Dr. Martin Keim, Nordic Test Forum 2017) is for fault
  localization in an *unknown*-structure network, already correctly ruled out as inapplicable by
  this project's own earlier `sib_overshift.py` research; not chased here.
- **Fixed: a `WRITE`-direction instrument gated by a `ScanMuxNode` arm didn't correctly
  round-trip its own value on readback.** Found as a side effect of strengthening a previously-
  weak test assertion (it only ever checked shift-op *count*, never the actual value, despite a
  comment falsely claiming otherwise), and initially mischaracterized (in this file and the
  project's own memory) as a `sib_model.py` bug — direct re-investigation found its own shift/
  capture/update simulation matches real RTL exactly, including a `_read`-time redirect a
  blanket-removal patch confirmed is load-bearing (broke 4 real, previously-passing tests, 2 of
  them real RTL). The real bug: `PDLInterpreter._target_layout`/`iApply`'s pending-read block
  computed the *expected* comparison value by reusing `compose_bits`' position-order-then-
  reversed layout, which assumes a uniform linear cascade through the whole `width +
  select_width` block — wrong for a matched mux arm. Confirmed by reading `rtl/scan_mux_cell.v`
  directly and a dedicated RTL spike (`tests/test_scan_mux_write_arm_readback_cross_sim.py`,
  using a real multi-bit host port so MSB-first vs LSB-first is actually distinguishable, unlike
  every existing 1-bit fixture): while an arm stays matched, `so` is a *combinational
  passthrough* of the matched arm's own `so` for the round's entire duration (`matched_old_c`
  can't change mid-round), so its content surfaces on the round's own first `width`
  chronological cycles, MSB-first — not wherever the reused linear layout would place it. Fixed
  with a dedicated `_mux_arm_read_chronological_bits`, confirmed on real RTL in the two tests
  that had deliberately documented-but-skipped this exact check
  (`test_sib_insert_scan_mux_cross_sim.py`). The plain-`SibNode` case was never affected.
- **Fixed: a read-only `iApply` of an already-open `WRITE` instrument silently zeroed its own
  stored value for any later read.** Broader than the "same instrument twice with no
  intervening target" footgun the retargeting shift-length optimization plan already fixed —
  phase 2 always delivers `payload_value` (defaulting to `0` absent a fresh `iWrite`), and
  `stays_matched`/`stays_open` naturally holds for a round that doesn't change what's selected,
  so that `0` committed via Update-DR regardless of mux involvement or intervening targets. Not
  mux-specific — the plain-`SibNode` case was affected too. A second, closely related bug with
  the same root cause, found while fixing the first: `iWrite`'s own sub-field (`field=`) merge
  had an identical gap — writing one field in a *fresh* apply cycle (no other field of the same
  instrument also queued that batch) silently zeroed every other field instead of preserving
  its own real last-committed value. Fixed with one new piece of state,
  `PDLInterpreter._committed_writes`, tracking each `WRITE`-direction instrument's own
  last-known committed value across `iApply` calls — both `iApply`'s own phase-2 payload
  selection and `iWrite`'s own sub-field merge fall back to it instead of a bare `0`. Confirmed
  as real bugs on real RTL before the fix (both plain-`SibNode` and `ScanMuxNode`-arm cases),
  now permanent passing regression tests.
- **Second real-external-design validation: `alexforencich/verilog-uart`'s `uart_tx.v`.**
  Closes the "only ever proven against one real external design" gap (`openMBIST`'s
  `mem_subsystem_mbist`) with a second, unrelated, MIT-licensed real design (583-star,
  actively maintained), read live from a sibling checkout exactly like openMBIST already is
  (`verilog_uart_dir` fixture, mirroring `openmbist_dir`'s own convention). Unlike every
  `mem_subsystem_mbist` test, `uart_tx.v` needs its own functional clock pulsed a real,
  precise number of times (81 cycles for a full byte at `prescale=1`) with no useful
  relationship to how many TCK cycles JTAG shifting happens to take — the first real use of
  `PDLInterpreter.iRunLoop`'s `sck_port` parameter (Stage 15's own `-sck` selector) end to end,
  rather than the lower-level `retarget_faultflow_patterns` API Stage 14's own cross-sim tests
  used to prove the same underlying `PulsePin` primitive. One `WRITE` instrument drives
  `s_axis_tdata`+`s_axis_tvalid` together (9 bits, one atomic `iWrite`/`iApply`); a second sets
  `prescale`; a `READ` instrument confirms `busy` toggles 0→1→0 across a transmission — and,
  the strong end-to-end proof, a new testbench (`tb_uart_tx.v`) traces `txd` directly every
  functional-clock pulse, never through JTAG, letting the test decode the actual transmitted
  byte in Python and confirm it matches what was written via JTAG. Two real findings surfaced
  independently verifying the RTL before writing any warptap-side code (a standalone spike,
  not trusted from a web summary): the stop-bit bit-time is 9 cycles, one longer than every
  other bit (`bit_cnt==1`'s own branch sets `prescale_reg <= (prescale<<3)` with no `-1`), and
  `busy` never visibly dips between back-to-back transmissions unless `s_axis_tvalid` is
  explicitly deasserted before the frame completes (`tready` pulses for exactly one cycle at
  frame-end with no separate same-cycle check gating a fresh start). Also confirmed, the hard
  way (a same-session `ValueError` on real `x`-valued trace output): `uart_tx.v`'s own
  `reg = <literal>` initial-value declarations do not survive this project's Yosys JSON
  netlist round-trip, and its reset is synchronous-only (unlike every prior fixture's DUT
  reset) — so the new testbench's own reset lead-in needed a real pulse row, not just a held
  level, to commit a known state before anything is sampled.

### Explicitly out of scope

Dynamic `existPr`-conditional PDL reachability, any conformance claim against a specific IEEE
standard edition, and PDL import for anything beyond `to_pdl()`'s own output shape (arbitrary
hand-authored `.pdl` files — multiple statements per line, statements spanning lines, real
`iProc` definitions — see `pdl_import.py`'s own module docstring for the exact boundary).
