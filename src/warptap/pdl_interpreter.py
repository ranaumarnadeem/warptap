"""PDL (Procedural Description Language) interpreter and static SIB-network retargeting
(implementation_plan.md §7 Stage 5, §3.3). ``iWrite``/``iRead``/``iRunLoop`` are non-executing
setup commands mutating pending state; ``iApply`` is the sole action command, resolving the
SIB path to the current ``iTarget`` scope and emitting a two-phase Capture-Shift-Update
sequence as :mod:`warptap.tap_ir` ops -- pure data, no simulation side effect. A downstream
backend (``tap_ir_play.play()`` for cross-sim testing; a future Stage 7 SVF/STAPL emitter)
walks the same ops list, demonstrating the "stateless pretty-printers over one IR" property
§3.4 promises.

Never touches the netlist/insertion layer -- constructed directly from
``sib_plan.build_sib_plan``'s two return values, mirroring ``SibNetworkRegister(graph)``'s own
dependency shape, matching Stage 4's "keep it fully independent" precedent.

Stage 11 adds ``self.history`` (:mod:`warptap.pdl_history`) alongside ``self.program``: a
parallel, additive-only record of each successful command call in call order, needed because
which instrument/value a given ``iApply`` actually targeted is provably unrecoverable from the
low-level ``ShiftDR`` ops alone (see ``pdl_history``'s own module docstring). Nothing about
``self.program``'s own deferred/batched lowering changes.
"""

from __future__ import annotations

from typing import Optional, Union

from warptap.errors import WarptapError
from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanMuxNode,
    resolve_dotted_address,
)
from warptap.pdl_history import (
    PdlApplyStmt,
    PdlReadStmt,
    PdlRunLoopStmt,
    PdlStatement,
    PdlTargetStmt,
    PdlWriteStmt,
)
from warptap.sib_layout import compose_bits, layout_bit_length
from warptap.sib_retarget import open_path_to, stage_open_sequence
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, bits_to_int


class PDLError(WarptapError):
    """PDL-command-layer misuse not already covered by a lower-layer exception
    (``ICLAddressError`` for a bad ``iTarget`` path, ``SibRetargetError`` for an unreachable
    instrument) -- e.g. ``iWrite``/``iRead``/``iApply`` called before any ``iTarget``."""


def _target_layout(
    graph: PhysicalGraph, opened: dict[str, int], target_sib: str
) -> tuple[int, int, bool]:
    """(offset, width, is_mux_arm) of ``target_sib``'s own instrument-content bits within
    ``compose_bits``'s position-ordered output for the ``opened`` configuration -- offset
    counts from index 0 (nearest TDI), the same convention ``compose_bits`` itself uses.
    Recurses into an open hierarchy slot's own nested sub-chain, matching sib_layout.py's
    own recursive walk (if the target isn't inside a given nested sub-chain, its full
    contribution -- via layout_bit_length, reused rather than re-derived -- is skipped over
    to keep counting the remaining siblings correctly).

    ``opened`` is always ``dict[str, int]`` here -- ``iApply``'s own ``self._currently_open``,
    which ``sib_retarget.stage_open_sequence`` always returns as a dict, even for a pure-SibNode
    network (implicit value ``1``). For a :class:`~warptap.icl_model.ScanMuxNode`, ``target_sib``
    naming the mux itself means its *currently matched* arm's own instrument is the real
    target -- mirrors ``compose_bits``'s own ``node.mux_name == target_sib`` check exactly (an
    ``open_path_to``-derived ``target_sib`` only ever equals a mux's name when that mux's own
    matched arm directly gates the target instrument, never a bare hierarchy node -- the same
    invariant that already lets the plain-SIB branch below assume ``node.instrument`` is set
    whenever ``node.sib_name == target_sib``).

    ``is_mux_arm`` is ``True`` exactly for that ``ScanMuxNode`` case -- ``iApply``'s own pending-
    read block needs it to pick :func:`_mux_arm_read_chronological_bits` over
    :func:`_read_mask_bits`'s usual position-order-then-reverse construction, which is wrong
    for a matched mux arm's own content (see :func:`_mux_arm_read_chronological_bits`'s own
    docstring, and the mux-arm-readback bug fix plan's own Context, for why ``offset`` itself
    is never meaningful for that case -- it's returned anyway, unused by that caller, only to
    keep this function's own return shape uniform)."""

    def _walk(chain: tuple, base_offset: int) -> Optional[tuple[int, int, bool]]:
        offset = base_offset
        for node in chain:
            if isinstance(node, ScanMuxNode):
                value = opened.get(node.mux_name)
                arm = next((a for a in node.arms if value in a.values), None) if value is not None else None
                if node.mux_name == target_sib:
                    return offset, arm.instrument.width, True
                if arm is not None:
                    if arm.nested:
                        found = _walk(arm.nested, offset)
                        if found is not None:
                            return found
                        offset += layout_bit_length(PhysicalGraph(chain=arm.nested), opened)
                    else:
                        offset += arm.instrument.width
                offset += node.select_width
            else:
                if node.sib_name == target_sib:
                    return offset, node.instrument.width, False
                if node.sib_name in opened:
                    if node.nested:
                        found = _walk(node.nested, offset)
                        if found is not None:
                            return found
                        offset += layout_bit_length(PhysicalGraph(chain=node.nested), opened)
                    else:
                        offset += node.instrument.width
                offset += 1
        return None

    found = _walk(graph.chain, 0)
    if found is None:
        raise AssertionError(f"{target_sib!r} not found in graph.chain")
    return found


def _read_mask_bits(total_bits: int, offset: int, low: int, high: int) -> list[int]:
    """Position-ordered mask: 1 at exactly ``[offset + low, offset + high]``, 0 everywhere
    else -- generalizes the original whole-instrument mask (Stage 5) to support a named
    sub-field ``iRead`` (Stage 15) addressing less than the full instrument: ``(low, high) =
    (0, width - 1)`` reproduces the original mask exactly, an iRead's whole-instrument
    expectation never being about any other slot's structural select bits, only the target's
    own content."""
    bits = [0] * total_bits
    for k in range(low, high + 1):
        bits[offset + k] = 1
    return bits


def _mux_arm_read_chronological_bits(
    total_bits: int, width: int, low: int, high: int, payload_value: int
) -> tuple[int, int]:
    """``(tdo, mask)`` ints for an ``iRead`` resolved to a :class:`~warptap.icl_model.
    ScanMuxNode`'s own currently-matched arm -- real, RTL-confirmed (``tests/test_scan_mux_
    write_arm_readback_cross_sim.py``, mux-arm-readback bug fix plan Phase 0) behavior that
    ``_read_mask_bits``'s usual position-order-then-``reversed()`` construction gets wrong:
    the matched arm's own content occupies the *first* ``width`` chronological cycles of the
    round, MSB-first (chronological cycle 0 = content bit ``width - 1``, ..., cycle
    ``width - 1`` = content bit 0) -- **not** wherever its own position-ordered ``offset``
    would place it after a uniform reversal.

    Root cause (confirmed against ``rtl/scan_mux_cell.v`` directly, not assumed): while an arm
    stays matched -- true for a round's *entire* duration once true, since ``matched_old_c`` is
    based on the OLD, pre-edge ``po`` and can't change until this round's own Update-DR --
    ``assign so = matched_old_c ? relay_so_c : shift_ff[0];`` makes the mux's own external
    ``so`` a *combinational passthrough* of the matched arm's own ``so``, never the mux's own
    ``select_width``-bit internal register (which, while matched, is just silently mirroring
    the arm's own ``so`` into itself each cycle, purely so it holds a sensible value if a later
    round switches away). ``compose_bits``/``_read_mask_bits`` reused for a comparison-value
    prediction assume a single uniform linear cascade through the whole ``width +
    select_width`` layout -- correct for constructing the *real* payload (which ``sib_model.
    py``'s own redirect-aware simulator processes correctly regardless of this function's own
    assumptions, confirmed separately), wrong for *predicting* what chronological cycle a given
    content bit surfaces on.

    ``low``/``high``/``payload_value`` match ``iApply``'s own existing convention exactly
    (``payload_value`` is the caller's already-``<< low``-shifted expected value, the same
    value already passed to ``compose_bits`` for the non-mux-arm case) -- this function only
    differs in *where* it places bits, not in the value/sub-field semantics themselves."""
    tdo_bits = [0] * total_bits
    mask_bits = [0] * total_bits
    for k in range(low, high + 1):
        chronological = (width - 1) - k
        tdo_bits[chronological] = (payload_value >> k) & 1
        mask_bits[chronological] = 1
    return bits_to_int(tdo_bits), bits_to_int(mask_bits)


def _find_instrument(chain: tuple, instrument_name: str) -> Optional[InstrumentNode]:
    """The :class:`~warptap.icl_model.InstrumentNode` named ``instrument_name`` anywhere in
    ``chain``, recursing into a nested ``SibNode.nested`` sub-chain or a
    :class:`~warptap.icl_model.ScanMuxNode`'s own arms (nested or leaf) -- properly recursive,
    mirroring :func:`~warptap.icl_emit._collect_instruments`'s own established traversal shape
    (single-target/early-return here instead of collecting every instrument into a ``seen``
    dict). Two real callers need this: ``iApply``'s own write-instrument-payload-defaulting fix
    needs a target's real ``direction`` regardless of nesting depth or mux-gating, and
    ``_resolve_field`` needs a target's declared ``aliases`` for ``iWrite``/``iRead``'s own
    named-sub-field resolution (Stage 15) -- the latter used to go through a since-deleted,
    top-level-only ``_instrument_for``, which silently returned ``None`` for a nested instrument
    (a hierarchy SIB's own ``.instrument`` is legitimately ``None``) and raised a bare
    ``AttributeError`` for any graph containing a ``ScanMuxNode`` at all (it unconditionally
    accessed ``node.instrument``, which ``ScanMuxNode`` has no such attribute for) -- fixed by
    routing both callers through this one, already-correct traversal instead."""
    for node in chain:
        if isinstance(node, ScanMuxNode):
            for arm in node.arms:
                if arm.instrument is not None and arm.instrument.name == instrument_name:
                    return arm.instrument
                if arm.nested:
                    found = _find_instrument(arm.nested, instrument_name)
                    if found is not None:
                        return found
        else:
            if node.instrument is not None and node.instrument.name == instrument_name:
                return node.instrument
            if node.nested:
                found = _find_instrument(node.nested, instrument_name)
                if found is not None:
                    return found
    return None


def _resolve_field(graph: PhysicalGraph, instrument_name: str, field: str) -> tuple[int, int]:
    """``(low_bit, high_bit)`` for ``field`` -- a declared :class:`~warptap.icl_model.Alias`
    name -- on ``instrument_name``'s own instrument. ``instrument_name`` is resolved via
    :func:`_find_instrument`, so a nested (``SibNode.nested``) or
    :class:`~warptap.icl_model.ScanMuxNode`-arm-gated instrument resolves correctly too, not
    just a top-level one. Raises :class:`PDLError` naming the bad instrument/field rather than
    a bare ``KeyError``, matching this project's own "name the exact bad input" convention
    (``ICLAddressError``, ``SibRetargetError``)."""
    instrument = _find_instrument(graph.chain, instrument_name)
    if instrument is None:
        raise PDLError(f"no instrument named {instrument_name!r} in this network")
    for alias in instrument.aliases:
        if alias.name == field:
            return alias.low_bit, alias.high_bit
    raise PDLError(
        f"{field!r} is not a declared Alias on instrument {instrument_name!r} "
        f"(known aliases: {[a.name for a in instrument.aliases]})"
    )


def _set_bit_range(current: int, low: int, high: int, value: int) -> int:
    """Merge ``value`` into ``current`` at bit positions ``[low, high]`` (inclusive),
    preserving every other bit of ``current`` untouched -- so a named-sub-field ``iWrite``
    (Stage 15) doesn't clobber a previously-queued write to a *different* field of the same
    instrument."""
    width = high - low + 1
    mask = ((1 << width) - 1) << low
    return (current & ~mask) | ((value << low) & mask)


class PDLInterpreter:
    """Drives ``iTarget``/``iWrite``/``iRead``/``iRunLoop``/``iApply`` against a single SIB
    network (implementation_plan.md §7 Stage 4), flat or nested."""

    def __init__(self, graph: PhysicalGraph, root: ModuleInstance) -> None:
        self._graph = graph
        self._root = root
        self._scope: Optional[ModuleInstance] = None
        self._pending_writes: dict[str, int] = {}
        # Last-known committed value per WRITE-direction instrument, across separate iApply
        # calls -- empty here matches real hardware's own guaranteed post-reset-to-0 state
        # (instrument_write.v's own `po <= 1'b0` on trst_n), recovered via .get(name, 0). See
        # iApply's own phase-2 payload selection and iWrite's own sub-field merge, both of
        # which fall back to this instead of a bare 0 (a real, confirmed bug fix -- see
        # iApply's own docstring for the full mechanism).
        self._committed_writes: dict[str, int] = {}
        # (expected, low_bit, high_bit) -- (low_bit, high_bit) = (None, None) means "whole
        # instrument," resolved against the target's real width in iApply itself.
        self._pending_reads: dict[str, tuple[int, Optional[int], Optional[int]]] = {}
        # dict[str, int] once any iApply has run (sib_retarget.stage_open_sequence always
        # returns dict[str, int] rounds, even for a pure-SibNode network -- implicit value 1)
        # -- kept as the empty frozenset literal only for its own falsy/empty-collection
        # convenience before the first iApply, since layout_bit_length/compose_bits/
        # _target_layout all accept either shape transparently either way.
        self._currently_open: frozenset[str] = frozenset()
        self.program: list[Union[ShiftDR, GotoState, Runtest, PulsePin]] = []
        self.history: list[PdlStatement] = []

    def iTarget(self, dotted: str) -> None:
        """Resolve ``dotted`` against the module-instantiation tree, becoming the scope
        ``iWrite``/``iRead`` address. Lets ``ICLAddressError`` propagate unwrapped for a bad
        path -- it already names the problem precisely. Recorded to ``self.history`` only once
        resolution succeeds, matching every other command's own error-tolerant semantics
        (nothing else here mutates state on a failed call either)."""
        self._scope = resolve_dotted_address(self._root, dotted)
        self.history.append(PdlTargetStmt(dotted))

    def _require_scope(self, command: str) -> ModuleInstance:
        if self._scope is None:
            raise PDLError(f"{command} called with no iTarget scope set")
        return self._scope

    def iWrite(self, value: int, field: Optional[str] = None) -> None:
        """Queue ``value`` to be written to the current scope on the next ``iApply``. With no
        ``field`` (the default -- byte-identical to pre-Stage-15 behavior), addresses the
        whole instrument, as before. With ``field`` naming a declared
        :class:`~warptap.icl_model.Alias` on the current scope's instrument, merges ``value``
        into just that sub-range of the pending value via :func:`_set_bit_range`, preserving
        whatever else is already queued for this instrument -- real PDL's named sub-field
        addressing (``TDR_bit``/``UCreg``/ICL ``Alias``, confirmed real by independent
        research sources), closing what was previously a documented, stated gap rather than a
        silent one.

        ``current`` (the sub-field merge's own starting point) prefers whatever's *already
        pending* for this instrument in the current, not-yet-applied batch (an earlier
        ``iWrite`` call since the last ``iApply``, matching the field-merge precedent this
        docstring already describes) -- falling back to ``self._committed_writes``, the
        instrument's own last-known-committed value from a *previous* ``iApply``, only when
        nothing is pending yet this batch. A real, confirmed bug fix: without this fallback, a
        sub-field write in a fresh apply cycle (no other field of the same instrument also
        queued this batch) silently clobbered every *other* field to ``0`` instead of
        preserving its own real last-committed value -- see :func:`PDLInterpreter.iApply`'s
        own docstring for the full mechanism, shared with a closely related phase-2 payload
        bug this same ``self._committed_writes`` fixes. Raises :class:`PDLError` for an
        unknown ``field``."""
        scope = self._require_scope("iWrite")
        if field is None:
            self._pending_writes[scope.name] = value
        else:
            low, high = _resolve_field(self._graph, scope.name, field)
            current = self._pending_writes.get(scope.name, self._committed_writes.get(scope.name, 0))
            self._pending_writes[scope.name] = _set_bit_range(current, low, high, value)
        self.history.append(PdlWriteStmt(field if field is not None else scope.name, value))

    def iRead(self, expected: int, field: Optional[str] = None) -> None:
        """Queue an expected value for the current scope's next ``iApply`` -- populates
        the emitted ``ShiftDR``'s ``tdo``/``mask`` fields (§ note in ``iApply``), not
        evaluated here. With no ``field`` (the default), the whole instrument's content is
        compared, exactly as before -- resolution of *which* bits that means is deferred to
        ``iApply`` (which already looks up the target's width via ``_target_layout`` anyway),
        not resolved eagerly here, so this path adds no new lookup/failure mode for existing
        callers. With ``field`` naming a declared :class:`~warptap.icl_model.Alias`, only that
        sub-range is masked in; the rest of the instrument's content is don't-care for this
        read -- real PDL's named sub-field addressing, resolved eagerly here since it needs
        the instrument's declared aliases, unlike the whole-instrument case."""
        scope = self._require_scope("iRead")
        if field is None:
            self._pending_reads[scope.name] = (expected, None, None)
        else:
            low, high = _resolve_field(self._graph, scope.name, field)
            self._pending_reads[scope.name] = (expected, low, high)
        self.history.append(PdlReadStmt(field if field is not None else scope.name, expected))

    def iRunLoop(self, count: int, *, sck_port: Optional[str] = None) -> None:
        """Not deferred like ``iWrite``/``iRead``: a "wait N cycles" has no pending value
        to overwrite before some later flush, so this appends directly to ``self.program``
        at the position encountered. With no ``sck_port`` (the default -- byte-identical to
        pre-Stage-15 behavior), cycles TCK in Run-Test-Idle exactly as before -- real PDL's
        ``-tck`` selector. With ``sck_port`` naming a real functional-clock port, appends a
        :class:`~warptap.tap_ir.PulsePin` instead of a :class:`~warptap.tap_ir.Runtest`  --
        real PDL's ``-sck`` (system clock) selector, reusing the exact primitive Stage 13/14
        already built and real-cross-sim-validated for "pulse a named signal independent of
        TCK's own timing domain," rather than inventing a second, parallel concept. ICL has no
        confirmed mechanism for declaring a port as "the system clock" (unlike TCK, which is a
        real, named port kind), so ``sck_port`` is a Python-side-only parameter, never bound to
        an ICL declaration -- the same "caller must separately supply the real clock's name"
        pattern ``faultflow_retarget.retarget_faultflow_patterns``'s own ``clock_port``
        parameter already established. PDL text itself, unlike ICL, does have a real place for
        this name -- ``pdl_emit.to_pdl`` renders it as ``-sck``'s own operand (confirmed
        against a real, independently-authored PDL grammar; see that module's own docstring)."""
        if sck_port is None:
            self.program.append(
                Runtest(count, run_state=TapState.RUN_TEST_IDLE, end_state=TapState.RUN_TEST_IDLE)
            )
        else:
            self.program.append(PulsePin(sck_port, count))
        self.history.append(PdlRunLoopStmt(count, sck_port))

    def iApply(self) -> list[Union[ShiftDR, GotoState]]:
        """The sole action command. Resolves the current scope's gating SIB, emits phase 1
        (one or more rounds closing whatever's open and opening the new target -- sized
        against the network's actual prior state, implementation_plan.md §3.3's
        ``network_configuration`` seam) then phase 2 (shift the real payload/expected-read
        value with the target's segment now spliced in), clears the pending write/read for
        this target, and returns the ops (also appended to ``self.program``).

        Phase 1 is one round per :func:`~warptap.sib_retarget.stage_open_sequence` entry, not
        always exactly one: a closed SIB's nested content isn't physically part of the live
        scan chain yet (confirmed against real RTL, ``tests/test_sib_cell_nested_cross_sim.
        py``), so opening a target nested ``d`` levels deep genuinely needs ``d`` separate
        Capture-Shift-Update rounds, each one committing one more ancestor open before the
        next round can address anything inside it. For today's -- and every pre-nesting --
        case (a directly-gated top-level instrument), this loop runs exactly once, with the
        same ``open_now``/``open_after`` values phase 1 always used, so behavior is
        byte-identical to before this loop existed.

        Each round's length is a function of whatever is open *right now* (``self.
        _currently_open`` for the first round; the previous round's own result after that),
        not a static ``len(graph.chain)`` baseline -- if a previous ``iApply`` targeting a
        different instrument left some other SIB open, the first round must shift through
        that instrument's still-physically-present content (as don't-care garbage) to close
        it. This is load-bearing for correctly sequencing back-to-back ``iApply`` calls, not
        merely future-SAT-extension bookkeeping.

        For a READ instrument this garbage shift has zero lasting effect (``bc1_shift_only.v``
        has no update latch). **For a WRITE instrument it does** (implementation_plan.md §7
        Stage 9): every phase-1 round always feeds ``0`` for a non-target slot's content
        (``sib_layout.compose_bits``'s own documented behavior), and ``instrument_write.v``'s
        update-latch commits on any edge that leaves its gating SIB open both before and
        after -- which includes a phase-1 round's own Update-DR whenever a WRITE instrument
        stays open across it. Retargeting *away* from an open WRITE instrument (closing it) is
        safe -- the closing edge itself is gated off (open-before but not-open-after) -- and a
        value survives being closed and later reopened via a *different* intermediate target,
        since it's never touched while closed.

        **The one case this used to NOT protect -- fixed by :func:`~warptap.sib_retarget.
        stage_open_sequence`'s own ``currently_open`` parameter (retargeting shift-length
        optimization plan)**: calling ``iApply`` twice in a row for the *same* still-open
        WRITE instrument with no intervening different target. Before the fix, phase 1's
        round saw that instrument open both before and after (nothing changed) and still ran
        a real Update-DR, so its 0 don't-care fill committed, clobbering the value before
        phase 2 ever ran -- confirmed as a real bug on real RTL (not just the Python model),
        for both a plain SIB and a :class:`~warptap.icl_model.ScanMuxNode` arm, before this
        fix landed. Since ``target_open`` in that exact scenario is byte-identical to
        ``self._currently_open``, :func:`~warptap.sib_retarget.stage_open_sequence` now
        returns zero rounds for phase 1 -- no redundant Update-DR occurs at all, so there is
        nothing left to clobber the value. See ``tests/test_sib_insert_write_instrument_
        cross_sim.py``/``tests/test_sib_insert_scan_mux_cross_sim.py`` for the permanent real-
        RTL regression tests proving this.

        **Targeting an instrument gated by a :class:`~warptap.icl_model.ScanMuxNode` arm --
        including switching directly from one already-open arm to a different one -- needed no
        changes here at all** (multi-arm ScanMux plan Phase 4): ``open_path_to``'s own
        ``PathStep.value`` already carries which arm, and phase 1's existing round-per-
        ``stage_open_sequence``-entry loop already isolates "commit the new arm" (phase 1,
        ``target_sib=None``) from "deliver its payload" (phase 2) into separate rounds --
        exactly the same separation a hierarchy SIB's own open-then-descend already needed, not
        a new mechanism. Confirmed by :mod:`tests.test_pdl_interpreter_scan_mux`, which drives
        this exact method (not a hand-built ``compose_bits`` sequence) through an arm switch.

        **A real, confirmed bug, fixed by ``self._committed_writes``**: phase 2's own
        ``compose_bits`` call always re-asserts the identical ``opened``/``opened`` state
        before and after, so ``stays_matched``/``stays_open`` always holds for the target
        across that edge -- phase 2 *always* commits its own payload, regardless of whether a
        fresh ``iWrite`` was queued this apply. Before this fix, no fresh ``iWrite`` meant
        ``payload`` silently defaulted to ``0``, which still committed -- a read-only ``iApply``
        of an already-open WRITE instrument (no fresh write, ``iRead`` only) correctly observed
        its own real value (self-captured at Capture-DR, *before* that same round's own
        zero-fill Update-DR), but silently zeroed it out for any *later* touch. Confirmed as a
        real bug on real RTL (not just the Python model), for both a plain SIB and a
        :class:`~warptap.icl_model.ScanMuxNode` arm, before this fix landed -- and unrelated to
        the phase-1 fix two paragraphs up (that one only ever protected the *first* reapply;
        this covers every later one too, and every plain read-only touch in between). Now,
        absent a fresh ``iWrite``, phase 2's own payload falls back to
        ``self._committed_writes``, the target's own last-known committed value, instead of a
        bare ``0`` -- re-asserting what's already there is a real, physically correct no-op
        commit, not a fresh clobber. See ``tests/test_sib_insert_write_instrument_cross_
        sim.py``/``tests/test_sib_insert_scan_mux_cross_sim.py`` for the permanent real-RTL
        regression tests proving this.
        """
        scope = self._require_scope("iApply")
        self.history.append(PdlApplyStmt())
        instrument_name = scope.name
        target_path = open_path_to(
            self._graph, instrument_name, network_configuration=self._currently_open
        )
        target_sib = target_path[-1].name

        ops: list[Union[ShiftDR, GotoState]] = []

        # Phase 1: one round per stage_open_sequence entry. Each round asserts exactly that
        # round's open_after select bits; every other SIB (including whatever a prior
        # iApply left open, on round 1) is driven closed. Sized against `prior_open` -- the
        # network's actual physical state entering that round.
        prior_open = self._currently_open
        target_open = {step.name: step.value for step in target_path}
        for open_after in stage_open_sequence(
            self._graph, target_open, currently_open=self._currently_open
        ):
            len1 = layout_bit_length(self._graph, prior_open)
            bits1 = compose_bits(self._graph, prior_open, open_after, target_sib=None, payload_value=0)
            ops.append(GotoState(TapState.SHIFT_DR))
            ops.append(ShiftDR(len1, tdi=bits_to_int(list(reversed(bits1)))))
            ops.append(GotoState(TapState.RUN_TEST_IDLE))
            prior_open = open_after
        opened = prior_open  # == target_open plus every implied ancestor, each with its value
        self._currently_open = opened

        # Phase 2: shift the real payload, re-asserting the same (now-physical) select
        # state, with the target's own instrument segment now spliced into the chain.
        target_instrument = _find_instrument(self._graph.chain, instrument_name)
        is_write_target = (
            target_instrument is not None
            and target_instrument.direction is InstrumentDirection.WRITE
        )
        payload = self._pending_writes.pop(instrument_name, None)
        if payload is None:
            payload = self._committed_writes.get(instrument_name, 0) if is_write_target else 0
        if is_write_target:
            # Phase 2 always commits (see this method's own docstring) -- record it now,
            # whether payload came from a fresh iWrite or the fallback above (re-storing the
            # same value is a harmless no-op), so a LATER apply has a real value to fall back
            # to instead of a bare 0.
            self._committed_writes[instrument_name] = payload
        pending_read = self._pending_reads.pop(instrument_name, None)
        len2 = layout_bit_length(self._graph, opened)
        bits2 = compose_bits(
            self._graph, opened, opened, target_sib=target_sib, payload_value=payload
        )
        tdo = mask = None
        if pending_read is not None:
            expected, low, high = pending_read
            offset, width, is_mux_arm = _target_layout(self._graph, opened, target_sib)
            if low is None:  # whole-instrument iRead (no field given) -- Stage 5's original case
                low, high = 0, width - 1
            if is_mux_arm:
                # Real, RTL-confirmed exception (mux-arm-readback bug fix plan) -- a matched
                # ScanMuxNode arm's own content surfaces on different chronological cycles
                # than a uniform position-order-then-reversed layout would predict.
                tdo, mask = _mux_arm_read_chronological_bits(len2, width, low, high, expected << low)
            else:
                expected_bits = compose_bits(
                    self._graph, opened, opened, target_sib=target_sib, payload_value=expected << low
                )
                mask_bits = _read_mask_bits(len(expected_bits), offset, low, high)
                tdo = bits_to_int(list(reversed(expected_bits)))
                mask = bits_to_int(list(reversed(mask_bits)))
        ops.append(GotoState(TapState.SHIFT_DR))
        ops.append(ShiftDR(len2, tdi=bits_to_int(list(reversed(bits2))), tdo=tdo, mask=mask))
        ops.append(GotoState(TapState.RUN_TEST_IDLE))

        self.program.extend(ops)
        return ops
