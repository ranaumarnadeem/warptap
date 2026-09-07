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
from warptap.icl_model import ModuleInstance, PhysicalGraph, resolve_dotted_address
from warptap.pdl_history import (
    PdlApplyStmt,
    PdlReadStmt,
    PdlRunLoopStmt,
    PdlStatement,
    PdlTargetStmt,
    PdlWriteStmt,
)
from warptap.sib_layout import compose_bits, layout_bit_length
from warptap.sib_retarget import open_path_to
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, bits_to_int


class PDLError(WarptapError):
    """PDL-command-layer misuse not already covered by a lower-layer exception
    (``ICLAddressError`` for a bad ``iTarget`` path, ``SibRetargetError`` for an unreachable
    instrument) -- e.g. ``iWrite``/``iRead``/``iApply`` called before any ``iTarget``."""


def _target_layout(graph: PhysicalGraph, opened: frozenset[str], target_sib: str) -> tuple[int, int]:
    """(offset, width) of ``target_sib``'s own instrument-content bits within
    ``compose_bits``'s position-ordered output for the ``opened`` configuration -- offset
    counts from index 0 (nearest TDI), the same convention ``compose_bits`` itself uses."""
    offset = 0
    for node in graph.chain:
        if node.sib_name == target_sib:
            return offset, node.instrument.width
        offset += node.instrument.width + 1 if node.sib_name in opened else 1
    raise AssertionError(f"{target_sib!r} not found in graph.chain")


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


def _instrument_for(graph: PhysicalGraph, instrument_name: str):
    """The :class:`~warptap.icl_model.InstrumentNode` named ``instrument_name`` in ``graph``,
    or ``None`` if absent -- ``iApply`` already discovers this indirectly via
    ``sib_retarget.open_path_to``; this direct lookup exists for ``iWrite``/``iRead``'s own
    named-sub-field resolution (Stage 15), which needs the instrument's declared ``aliases``
    before ``iApply`` ever runs."""
    for node in graph.chain:
        if node.instrument is not None and node.instrument.name == instrument_name:
            return node.instrument
    return None


def _resolve_field(graph: PhysicalGraph, instrument_name: str, field: str) -> tuple[int, int]:
    """``(low_bit, high_bit)`` for ``field`` -- a declared :class:`~warptap.icl_model.Alias`
    name -- on ``instrument_name``'s own instrument. Raises :class:`PDLError` naming the bad
    instrument/field rather than a bare ``KeyError``, matching this project's own "name the
    exact bad input" convention (``ICLAddressError``, ``SibRetargetError``)."""
    instrument = _instrument_for(graph, instrument_name)
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
    """Drives ``iTarget``/``iWrite``/``iRead``/``iRunLoop``/``iApply`` against a single
    flat/static SIB network (implementation_plan.md §7 Stage 4's scope)."""

    def __init__(self, graph: PhysicalGraph, root: ModuleInstance) -> None:
        self._graph = graph
        self._root = root
        self._scope: Optional[ModuleInstance] = None
        self._pending_writes: dict[str, int] = {}
        # (expected, low_bit, high_bit) -- (low_bit, high_bit) = (None, None) means "whole
        # instrument," resolved against the target's real width in iApply itself.
        self._pending_reads: dict[str, tuple[int, Optional[int], Optional[int]]] = {}
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
        silent one. Raises :class:`PDLError` for an unknown ``field``."""
        scope = self._require_scope("iWrite")
        if field is None:
            self._pending_writes[scope.name] = value
        else:
            low, high = _resolve_field(self._graph, scope.name, field)
            current = self._pending_writes.get(scope.name, 0)
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
        TCK's own timing domain," rather than inventing a second, parallel concept. Neither
        PDL text nor ICL gives ``-sck``'s target port a confirmed real place to live, so
        ``sck_port`` is a Python-side-only parameter -- the same "caller must separately
        supply the real clock's name" pattern ``faultflow_retarget.retarget_faultflow_patterns``'s
        own ``clock_port`` parameter already established."""
        if sck_port is None:
            self.program.append(
                Runtest(count, run_state=TapState.RUN_TEST_IDLE, end_state=TapState.RUN_TEST_IDLE)
            )
        else:
            self.program.append(PulsePin(sck_port, count))
        self.history.append(PdlRunLoopStmt(count, sck_port))

    def iApply(self) -> list[Union[ShiftDR, GotoState]]:
        """The sole action command. Resolves the current scope's gating SIB, emits phase 1
        (close whatever's open, open the new target -- sized against the network's actual
        prior state, implementation_plan.md §3.3's ``network_configuration`` seam) then
        phase 2 (shift the real payload/expected-read value with the target's segment now
        spliced in), clears the pending write/read for this target, and returns the ops
        (also appended to ``self.program``).

        Phase 1's length is a function of whatever is open *right now* (``self.
        _currently_open``), not a static ``len(graph.chain)`` baseline -- if a previous
        ``iApply`` targeting a different instrument left some other SIB open, phase 1 must
        shift through that instrument's still-physically-present content (as don't-care
        garbage) to close it. This is load-bearing for correctly sequencing back-to-back
        ``iApply`` calls, not merely future-SAT-extension bookkeeping.

        For a READ instrument this garbage shift has zero lasting effect (``bc1_shift_only.v``
        has no update latch). **For a WRITE instrument it does** (implementation_plan.md §7
        Stage 9): phase 1 always feeds ``0`` for a non-target slot's content (``sib_layout.
        compose_bits``'s own documented behavior), and ``instrument_write.v``'s update-latch
        commits on any edge that leaves its gating SIB open both before and after -- which
        includes phase 1's own Update-DR whenever a WRITE instrument stays open across it.
        Retargeting *away* from an open WRITE instrument (closing it) is safe -- the closing
        edge itself is gated off (open-before but not-open-after) -- and a value survives
        being closed and later reopened via a *different* intermediate target, since it's
        never touched while closed. The one case this does NOT protect: calling ``iApply``
        twice in a row for the *same* still-open WRITE instrument with no intervening
        different target -- phase 1 of the second call sees that instrument open both before
        and after (nothing changed), so its 0 don't-care fill commits, clobbering the value
        before phase 2 ever runs. A real PDL sequence naturally avoids this (a second touch
        without going elsewhere would normally carry a fresh ``iWrite`` anyway); it is not
        specially detected or rejected here.
        """
        scope = self._require_scope("iApply")
        self.history.append(PdlApplyStmt())
        instrument_name = scope.name
        target_path = open_path_to(
            self._graph, instrument_name, network_configuration=self._currently_open
        )
        target_sib = next(iter(target_path))  # v1: exactly one SIB on a flat network's path

        old = self._currently_open
        opened = frozenset({target_sib})
        ops: list[Union[ShiftDR, GotoState]] = []

        # Phase 1: assert only the new target's select bit; every other SIB (including
        # whatever a prior iApply left open) is driven closed. Sized against `old` -- the
        # network's actual current physical length.
        len1 = layout_bit_length(self._graph, old)
        bits1 = compose_bits(self._graph, old, opened, target_sib=None, payload_value=0)
        ops.append(GotoState(TapState.SHIFT_DR))
        ops.append(ShiftDR(len1, tdi=bits_to_int(list(reversed(bits1)))))
        ops.append(GotoState(TapState.RUN_TEST_IDLE))
        self._currently_open = opened

        # Phase 2: shift the real payload, re-asserting the same (now-physical) select
        # state, with the target's own instrument segment now spliced into the chain.
        payload = self._pending_writes.pop(instrument_name, 0)
        pending_read = self._pending_reads.pop(instrument_name, None)
        len2 = layout_bit_length(self._graph, opened)
        bits2 = compose_bits(
            self._graph, opened, opened, target_sib=target_sib, payload_value=payload
        )
        tdo = mask = None
        if pending_read is not None:
            expected, low, high = pending_read
            offset, width = _target_layout(self._graph, opened, target_sib)
            if low is None:  # whole-instrument iRead (no field given) -- Stage 5's original case
                low, high = 0, width - 1
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
