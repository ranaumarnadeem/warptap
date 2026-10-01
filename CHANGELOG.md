# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/), one entry per
implementation stage.

## [Unreleased]

### Added

- **`capture_sync` on a READ instrument** (`InstrumentSpec(..., capture_sync=True)`): its
  bits capture through a two-TCK-flop synchronizer (`rtl/bc1_shift_only_sync.v`, reset by
  `trst_n`), for a signal from another clock domain. A capture shows the signal as it was two
  TCK edges earlier, never a value caught changing. Each bit has its own synchronizer, so a
  multi-bit value is coherent only if it holds still for those two edges, as a settled status
  does. Off by default: a network without it is inserted byte for byte as before. A WRITE
  instrument can't ask for it.
- **`chip_reset` on `insert_sib_network` / `insert_test_access`**: every WRITE instrument also
  clears on the chip reset input (active low unless `chip_reset_active_low=False`), not only
  on `trst_n`, so the signals it drives come up deasserted after a chip reset even when TRST
  never pulsed. Its cells are `rtl/instrument_write_clr.v`, cleared through
  `rtl/tck_reset_sync.v`, which asserts with the chip reset and releases two TCK edges after
  it: the release is a TCK-domain path, never an asynchronous one near a TCK edge. An
  Update-DR within those two edges is lost, and with TCK stopped the clear holds. The model
  doesn't simulate a chip reset: a program must leave it inactive after its TRST lead-in.
  Off by default, byte for byte as before.

## [0.0.3] - 2026-09-29

### Behavior changes

Upgrading from 0.0.2 changes what the inserted TAP does. Check these first.

- **The IJTAG network moves only while EXTEST (`0000`) is loaded.** In 0.0.2 every top-level
  SIB's `select` was tied to 1, so the network captured, shifted, opened and committed on a DR
  scan under any instruction: SAMPLE/PRELOAD, IDCODE (loaded by every reset) and BYPASS
  included. Anything that relied on SAMPLE/PRELOAD, IDCODE or BYPASS moving the network will
  break; load EXTEST first. Under SAMPLE/PRELOAD the network's tail still reaches TDO, but the
  network holds still.
- **A pattern with no instruction load no longer reaches the network.** `PDLInterpreter.program`
  and the faultflow retargeting functions emit DR scans only. Played straight after a reset
  (IDCODE loaded), a write-only program took effect by accident in 0.0.2 and now does nothing:
  prepend `select_instruction(OPCODE_EXTEST)`. No warptap test, fixture or library code path
  relied on the old behavior; `docs/quickstart.md` and the faultflow guide omitted the EXTEST
  load and now include it, and the `PDLInterpreter` docstring says it is required.
- **Entering Test-Logic-Reset through TMS reloads IDCODE.** In 0.0.2 only `trst_n` reset the
  instruction, so a 5xTMS=1 reset left the previous instruction (e.g. EXTEST) loaded. Anything
  that relied on the instruction surviving a TMS reset will break.

### Added

- **Stage 24 — faultflow compression/compaction-aware pattern retargeting.** Closes a gap
  `faultflow_retarget.py`'s (Stage 14) naive `chain_to_instrument` model couldn't express: a
  faultflow design with `[compression]` and/or `[compaction]` enabled has its raw `scan_in_N`/
  `scan_out_N` chain ports become internal wires on the composed netlist, with the real
  external pins becoming a K-wide `tdi`/`tdo` channel bus feeding a Galois-form LFSR ring
  generator (load side) or a registerless XOR-tree (unload side). New modules
  `faultflow_compression.py`/`faultflow_compaction.py` add
  `retarget_compressed_faultflow_patterns`/`retarget_compacted_faultflow_patterns`, each
  replacing only the side its own transform actually touches (the other side still goes
  through the existing per-chain `chain_to_instrument` mechanism unchanged). Compression's own
  GF(2) math (`care_bit_rows`, the Gauss-Jordan `solve_xor_broadcast`, the curated LFSR
  polynomial table) is natively ported from real faultflow source, per this project's own
  "port understanding, not code" boundary with that project -- never imported or called.
  `Module.rename_port` (`netlist.py`) plus a new `port_renames` parameter on
  `insert_test_access` (`pipeline.py`) fix a real naming collision: warptap's own hardcoded
  `tdi`/`tdo` TAP pin names would otherwise crash against a compression/compaction composed
  netlist's own channel ports of the same name. Unit-tested (hand-derived GF(2) traces, op
  sequence assertions) plus a skip-if-absent cross-check against faultflow's own real
  polynomial table, PLUS a real cross-sim tier: both retargeting functions driven through real
  Icarus Verilog against a real `insert_compression`/`insert_compaction`-produced, warptap-SIB-
  inserted netlist, sky130 gates and all. Caught two real gaps in the test fixtures themselves
  (not the retargeting code) along the way: the real sky130 behavioral models need `` `define
  FUNCTIONAL`` to bypass their own `specify`/timing-check blocks under a crude bit-banged
  testbench (mirrors `faultflow/verify/gate.py`'s own `-DFUNCTIONAL`), and a scan chain's FF
  needs genuine scan-enable-gated hold behavior (a plain `dfxtp` re-samples the ring
  generator's still-evolving output on the retargeting function's own capture edge and silently
  clobbers the just-loaded value) -- both fixed in the fixtures, zero changes needed to
  `faultflow_compression.py`/`faultflow_compaction.py` themselves. A later faultflow change
  (commit `26e1f96`, branch `compress`) closed the curated-polynomial-table drift risk directly:
  `manifest["compression"]["polynomial"]` now serializes the campaign's real LFSR polynomial, so
  `faultflow_compression.py`'s new `polynomial_from_manifest` reads it straight from the manifest
  instead of guessing via the curated table, which now survives only as a fallback for a
  manifest captured before that field existed; extended
  `test_faultflow_compression_polynomial_table.py` covers the new path with a hand-built
  manifest, independent of whether a faultflow checkout is present. A second later faultflow
  change (commit `9b670df`, branch `compress`) closed this module's other flagged limitation:
  an exported pattern now carries `load_care` (the exact `(chain_id, cycle)` positions ATPG
  proved necessary for detection), so `solve_pattern_seed`'s new optional `load_care` parameter
  solves against only those positions instead of every specified `load_seqs` position, letting a
  genuine don't-care position's arbitrary fill value stop causing false "unsatisfiable" results;
  falls back to the original every-position behavior when a manifest has no `load_care` (older
  faultflow, random-fill patterns, non-compression campaigns).

- **Stage 25 — `insert_test_access()` accepts `HierarchySpec`.** Its `specs` parameter was typed
  `List[InstrumentSpec]` since the Library-quality pass (0.0.1), predating this project's own
  nested-SIB/ScanMux work (Stage 4's `SibNode.nested`, and the mux-arm generalization that
  followed) -- purely stale, not a deliberate restriction: the function's own body already
  forwarded `specs` straight to `build_sib_plan()` unchanged, which has accepted a
  `HierarchySpec` in the mix since that same nested-network work landed. Widened to a new public
  `TestAccessSpec = Union[InstrumentSpec, HierarchySpec]` alias (re-exported from
  `warptap/__init__.py` alongside `insert_test_access`), docstring corrected to stop calling the
  built network "flat", and a new test
  (`tests/test_pipeline.py::test_insert_test_access_accepts_a_hierarchy_spec`) proves the
  *pipeline* entry point specifically -- not just `build_sib_plan`/`insert_sib_network` directly,
  which is all the existing nested-SIB test suite had ever exercised -- correctly threads a
  nested `HierarchySpec` end to end (structure, flat sibling addressing, and a working
  `PDLInterpreter.iApply()` against the nested leaf). No RTL or behavioral change: this closes a
  gap in what the convenience wrapper's own type/docs *claimed* it could do, not in what the
  underlying insertion machinery could already do.

- **Stage 26 — BSDL emitter for the TAP.** `to_bsdl(entity_name, *, tck_max_freq_hz,
  idcode_value=IDCODE_VALUE)`
  (`bsdl_emit.py`, exported with `BsdlEmitError`) writes a BSDL file for the TAP warptap inserts:
  the entity, the five TAP pins, the `TAP_SCAN_*` attributes, `INSTRUCTION_LENGTH`/`OPCODE`/
  `CAPTURE`, `IDCODE_REGISTER`, `REGISTER_ACCESS` and a `DESIGN_WARNING`. Every value comes from
  `tap_model`/`tap_ports`; none is duplicated. It is TAP-only: it declares no `BOUNDARY_LENGTH` or
  `BOUNDARY_REGISTER`, so it is not a chip-level BSDL and a tool that requires a boundary register
  will reject it (the `DESIGN_WARNING` says so). EXTEST (0000) and SAMPLE/PRELOAD (0010) are
  declared as usual; the warning states that in a SIB design neither selects a boundary register:
  both put the IJTAG network's tail on TDO (`rtl/tap_core.v` routes it for every instruction
  other than IDCODE and BYPASS), and only EXTEST moves the network (see the behavior changes
  above). `tap_model.NETWORK_ACCESS_INSTRUCTION` (EXTEST) names
  the instruction a tester loads to reach the network; `to_icl` and `to_bsdl` both read it from
  there. The emitted text is read back and compared with `tap_model`, and every behavioral claim is
  checked on the real `tap_core.v` under Icarus Verilog: capture pattern, IR length, IDCODE value
  and width, BYPASS depth, EXTEST/SAMPLE/PRELOAD routing, undeclared opcodes acting as BYPASS, and
  the `DESIGN_WARNING` claims (a TMS reset and `trst_n` both select IDCODE, TDO is low outside
  the shift states and changes only on the rising edge of TCK). These tests were
  mutation-checked against wrong BSDL claims and RTL bugs. No independent BSDL parser validates
  the output. Three were installed and run: UrJTAG 0.10 (Ubuntu apt) fails on every input,
  valid or not; `bsdl-parser` can't import on Python 3.10+ (`grako`); `cb_bsdl_parser` raises
  on every input, valid or not (evidence in `tests/test_bsdl_emit.py`).
  `to_icl(..., include_access_link=False)` output is now pinned byte-for-byte by golden files
  (`.gitattributes` marks them `-text` so `core.autocrlf` cannot alter them).

- **A design's own IDCODE: `idcode_value`.** `insert_test_access`, `insert_sib_network` and
  `to_bsdl` take `idcode_value` (default `tap_model.IDCODE_VALUE`, the placeholder). The
  inserted `tap_core` gets the value baked into its `IDCODE_VALUE` parameter, imported through
  Yosys `chparam` as `scan_mux_cell` already is. The default keeps the previous import path,
  because `chparam` changes Yosys's output even at the default value: default output is
  byte-identical to before (the inserted Verilog for `real_signal.v` has the same sha256; the
  BSDL and ICL golden files still match). `tap_model.idcode_value_error` enforces IEEE 1149.1's
  32 bits with bit 0 set, and `TapModel` (`TapModelError`), `TapConfig` (`TapIntegrityError`),
  `to_bsdl` (`BsdlEmitError`) and both insertion functions (`SibInsertError`, before anything
  is ingested) reject anything else; Yosys would otherwise silently truncate a wider value.
  `to_bsdl` calls only the default value a placeholder. Cross-simulated on real inserted RTL:
  a custom value shifted out after TRST equals the BSDL's `IDCODE_REGISTER`, and
  `build_integrity_program(tap=TapConfig(idcode_value=V))` passes on that RTL while a program for
  the default fails at `reset_instruction`; the test fails if the value is ignored.

- **TAP and IJTAG network-integrity patterns** (`warptap.tap_integrity`).
  `build_integrity_program(graph, root)` builds the TCK-level program a production flow
  plays to test the TAP and the network through TCK, with the TDO it expects from
  `TapModel` + `SibNetworkRegister`: IDCODE after TRST, the IR's capture pattern and length,
  BYPASS, every unimplemented opcode (`exhaustive_opcodes`), an explicit IDCODE load, EXTEST
  with every SIB closed and then each SIB and ScanMux arm opened alone (over-shift probes,
  each from an all-closed network), each WRITE instrument written with P, ~P and 0 and read
  back (`write_readback`), and a TMS reset. READ instruments bound to design signals are
  don't-care wherever their captured value reaches TDO (two lockstep models, capturing all 0s
  and all 1s; `live_values` pins them). `check_integrity(program, observed)` names the first
  failing test; `IntegrityProgram.to_json()`/`from_json()` carry the program as a
  self-contained file (`warptap-tck-program` v1). Cross-simulated against real inserted RTL
  (flat with a live instrument, nested, ScanMux), including programs built for the wrong
  network or TAP, which fail in the tests that see the difference; on the RTL from before
  the fixes below, the program fails `network_closed` and `tms_reset`.
- **The integrity program tests the TAP's state machine and the network holding still.**
  `build_integrity_program` gains two test groups, both on by default: `tap_paths` (raw TCK
  sequences taking every one of the 32 TAP state transitions -- Pause-DR/IR, Exit2, Update ->
  Select-DR, Capture -> Exit1 without a shift, Select-IR -> Test-Logic-Reset -- with IDCODE,
  BYPASS and the IR each held in Pause at 0 and at 1 in every bit, and IDCODE and BYPASS
  captured over their complement) and `network_hold` (SAMPLE/PRELOAD, whose TDO is the
  network's tail while the network holds still, then one EXTEST scan through the open network
  paused holding an alternating pattern and its complement, ending with every SIB closed).
  `SibNetworkRegister` gains `tail()` and `capture_unselected()`: every instrument leaf
  captures on every Capture-DR, whatever the instruction, which cross-simulating SAMPLE/PRELOAD
  on a ScanMux network showed (its tail is a leaf's bit). Graded by FaultFlow's fault
  simulator (stuck-at, sky130 gate level) on autoMBIST's JTAG-wrapped designs, the program
  alone now detects 95.6% of the TAP's faults instead of 72.7% (dedicated; 95.7% instead of
  72.5% on self-repair) and 94.6% of the IJTAG TDRs' instead of 86.5% (92.8% instead of 82.0%);
  an 8000-cycle random TCK walk on top of it finds 12 more faults on the dedicated design. What
  stays undetected is almost all out of TCK's reach: a register's behaviour under another
  instruction (every DR read starts with a capture), decodes of opcodes Update-IR never latches,
  TDO outside the shift states, and most of a SIB's capture logic.

### Fixed

- **The IJTAG network moved on every DR scan, not just under EXTEST.** `insert_sib_network`
  tied every top-level SIB's `select` to 1, and `tap_core`'s capture/shift/update strobes fire
  for every instruction's DR scan. So the network shifted, opened and committed under IDCODE
  (the instruction after every reset) and BYPASS (a board chain passing through): two
  all-ones DR scans drove every WRITE instrument (`test_mode`, `bist_start`, ...) to 1. A
  `$eq` decode (`warptap_ijtag_extest_decode`, like `insert_bsr`'s `extest_mode`) now selects
  the network only while EXTEST is loaded, matching the Python model, which already drove it
  only under EXTEST. `insert_sib_network` gains `opcode_extest` (default `0b0000`, which must
  match `tap_core`). Consequence: in a SIB-only design, SAMPLE/PRELOAD still puts the network's
  tail on TDO but no longer moves the network (it has no data register there; the model already
  raised for it). The BSDL `DESIGN_WARNING`, `NETWORK_ACCESS_INSTRUCTION`'s comment and the
  `bsdl_emit`/`icl_emit` docstrings, which said SAMPLE/PRELOAD reaches the network, say so.
- **Test-Logic-Reset didn't reload the instruction.** IEEE 1149.1 requires Test-Logic-Reset to
  load IDCODE (BYPASS without one), so five TMS=1 cycles deselect a test-mode instruction
  without a TRST pin; `tap_core` only did it on `trst_n`. `tap_core.v` and `TapModel` now both
  reload it on every TCK edge taken in Test-Logic-Reset. The BSDL `DESIGN_WARNING` no longer
  lists this deviation, and `test_bsdl_emit_cross_sim`, which pinned it, checks the reload.
- **`tap_core`'s IDCODE/BYPASS shift registers had no reset.** Invisible at TDO (Capture-DR
  always loads them first), but they started unknown and fed the TDO mux, so a gate-level X
  check couldn't clear `tdo`. Both now clear on `trst_n`; `TapModel.reset()` clears its
  built-in BYPASS/IDCODE registers to match (`BypassRegister`/`IdcodeRegister` gain `reset()`).
- **`to_icl`'s `AccessLink` named an instruction the TAP does not have.** The block used the
  instruction name `wdr_select`, copied from the IJTAG benchmark set's example chip, whose
  instruction blocks are named `wir_select`/`wdr_select`/`clk_select`; it is not part of ICL's
  syntax, and the docstrings wrongly said the block named `EXTEST`. It now names `EXTEST`, which
  `to_bsdl` declares, so the ICL and the BSDL agree (checked by `tests/test_icl_bsdl_agreement.py`).
  `to_icl` gains `bsdl_entity_name` (default `root.name`) so `BSDLEntity` can match the BSDL file's
  entity, and `include_access_link=True` with an empty chain now raises `IclEmitError` instead of
  emitting an `AccessLink` with no instruction block, which the grammar rejects.
  `include_access_link=False` output is unchanged. The syntax was confirmed from the vendored
  grammar (`icl.g4`), the IJTAG benchmark set (`E30.icl`, from IEEE 1687 Annex E example E.30),
  MAST's ICL grammar and test files, and ASSET InterTech's IJTAG article; the normative IEEE 1687
  text is paywalled and was not read. The vendored parser cannot parse any ordinarily formatted
  `AccessLink`, published ones included, because `AccessLinkGeneric_def` is a lexer rule in
  `icl.g4`, so the earlier claim that the emitted block was "grammatically valid" had never been
  checked by that tool. `tests/test_icl_emit_access_link_syntax.py` now checks it through the
  tool's raw ANTLR parser with the header's whitespace rewritten around that bug, with positive
  and negative controls.
- **`IDCODE_VALUE`'s comment was wrong.** `tap_model.py` said the placeholder was not a real
  registered JEDEC manufacturer ID; its manufacturer field decodes to JEP106 bank 1, code 0x01,
  which is an assigned code as far as we know (the JEP106 table was not checked). The value is
  unchanged, and `tap_core.v`'s default must move with it if it is ever replaced.
- **Tests backed by the vendored `icl_parser` skipped silently without it.** A checkout without
  the `third_party/icl_parser` submodule (every `git worktree`, until initialized) skipped 77
  grammar, ICL and PDL tests. They now fail with the fix-it command (`git submodule update
  --init third_party/icl_parser`); `WARPTAP_ALLOW_MISSING_ICL_PARSER=1` restores the skip. The
  `dev` extra now installs the parser's packages (`antlr4-python3-runtime==4.7.2`, `z3-solver`,
  `sympy`, `networkx`), the README documents the setup, and the ICL/PDL import guide points at
  the fork the submodule pins (upstream lacks the compiled PDL parser `import_pdl` needs).

### Explicitly out of scope

- A boundary-scan register in the BSDL: designs with one, from `bsr_insert.py`, get no
  `BOUNDARY_REGISTER` description, and `insert_bsr` does not take `idcode_value`.
- A dedicated instruction for the IJTAG network: it still shares opcode 0000 with EXTEST.
- The `AccessLink`'s `ScanInterface` list still names only the chain's first top-level slot, as a
  bare instance name. Reading the MAST project's retargeter source (not running it) indicates it
  needs exactly one entry, the chain's *last* slot as `<instance>.client`, to reach the whole
  chain; that has not been verified by running a retargeter, so the emitter is unchanged.
- Width>1 instrument registers in the emitted ICL source scan-out from the bit scan-in enters
  (`ScanRegister DR[w-1:0]` with `Source DR[w-1]`), which the published ICL convention and the
  vendored parser read as disconnecting the register's other bits. The RTL is unaffected; the
  ICL description of it is suspect. Not yet verified with a retargeter, so unchanged.
- The IEEE 1149.1 deviations in `rtl/tap_core.v` the `DESIGN_WARNING` reports: TDO changes on
  the rising edge of TCK and is driven low, not tri-stated, outside the shift states.

## [0.0.2] - 2026-09-16

### Added

- **Stage 15 — Field/alias addressing.** Real PDL/ICL named sub-field addressing (`Alias`)
  inside `iWrite`/`iRead`, letting a caller target a bit-range within an instrument by name
  instead of the whole register; `iRunLoop`'s `-sck` selector for pulsing a real system-clock
  port independent of TCK's own timing domain.
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
- **Fixed: `iWrite`/`iRead`'s own `field=` (named sub-field / `Alias`) resolution crashed or
  silently misresolved for any instrument not gated by a plain top-level `SibNode`.**
  `_resolve_field` (`pdl_interpreter.py`) resolved `instrument_name` via `_instrument_for`, a
  shallow, top-level-only walk of `graph.chain` that unconditionally accessed `node.instrument`
  on every entry — a real `ScanMuxNode` has no such attribute at all, so a `field=` lookup
  against any graph containing one anywhere raised a bare, unhelpful `AttributeError:
  'ScanMuxNode' object has no attribute 'instrument'`, regardless of whether the mux itself
  gated the target. For a nested (`HierarchySpec`/`SibNode.nested`) instrument, the same
  top-level-only walk instead silently returned `None`, misreporting a real, existing
  instrument as `PDLError("no instrument named ... in this network")`. A real, previously
  documented gap — `_instrument_for`'s own docstring (added alongside the write-instrument-
  payload-defaulting fix) explicitly named both failure modes and deliberately deferred fixing
  them, pre-identifying the fix as routing through the module's own already-correct, properly
  recursive `_find_instrument` instead (added that same session, previously used only by
  `iApply`) rather than conflating two unrelated fixes. Confirmed no existing test combined
  `field=` with either a mux-gated or nested instrument. Fixed exactly as pre-identified:
  `_instrument_for` deleted outright (zero other callers), `_resolve_field` now calls
  `_find_instrument`. New pure-Python regression tests confirm `field=` now resolves correctly
  through both a `ScanMuxNode` arm (`tests/test_pdl_interpreter_scan_mux.py`) and a nested
  `HierarchySpec` instrument (`tests/test_pdl_interpreter.py`).

### Explicitly out of scope

Dynamic `existPr`-conditional PDL reachability, any conformance claim against a specific IEEE
standard edition, and PDL import for anything beyond `to_pdl()`'s own output shape (arbitrary
hand-authored `.pdl` files — multiple statements per line, statements spanning lines, real
`iProc` definitions — see `pdl_import.py`'s own module docstring for the exact boundary). No
independent oracle exists for PDL *retargeting semantics* (as opposed to grammar) — confirmed
by a thorough external search (implementation_plan.md's own Stage 23), not just the one
vendored tool: every real, accessible, maintained open IJTAG tool found deliberately excludes
retargeting semantics from its own scope.

## [0.0.1] - 2026-09-07

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
standard edition.
