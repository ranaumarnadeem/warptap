"""STIL (IEEE Std 1450, "Standard Test Interface Language") text emitter (implementation_plan.md
§7 Stage 13). The fourth "stateless pretty-printer over one IR" (:mod:`warptap.tap_ir_svf`,
:mod:`warptap.tap_ir_stapl`, and this module all walk the same :mod:`warptap.tap_ir` ops list),
built specifically to carry :class:`~warptap.tap_ir.PulsePin` -- a functional/system-clock
pulse independent of TCK's own timing domain, which SVF/STAPL (a JTAG-pins-only vocabulary)
cannot express at all. See :class:`~warptap.tap_ir.PulsePin`'s own docstring for why this
capability exists and why it isn't modeled as TCK gating a functional clock instead.

**Grounded in real primary sources fetched directly this session**: the IEEE 1450 working
group's own "D03" STIL Reference Guide and a real worked 1450-1999 example file (both hosted at
``grouper.ieee.org/groups/1450``) -- the actual ratified standard text is paywalled and was not
independently read. **Live-validated against a real, independent, pip-installable parser**,
``Semi-ATE-STIL`` (``github.com/Semi-ATE/STIL``, Lark-based) -- despite its own "not yet ready
for production" disclaimer, it both syntax- and semantic-validated real STIL text this session,
including the exact multi-``WaveformTable`` mechanism this emitter depends on.

**Real, empirically-discovered requirements this module's output follows** (found by actually
feeding files through ``Semi-ATE-STIL``, not derived from documentation alone -- the Reference
Guide's own condensed worked example omits several of these):

- A ``WaveformTable``'s per-signal event schedules must be wrapped in a ``Waveforms { ... }``
  block -- the real worked 1450-1999 example this session's research found omits this wrapper
  entirely (an older/simplified presentation), but ``Semi-ATE-STIL``'s own grammar requires it.
- Block order is significant and was not documented anywhere this session's research found:
  ``SignalGroups`` must precede ``Timing``; ``PatternBurst`` and ``PatternExec`` must both
  precede any ``Pattern`` block they reference.
- Every signal referenced in ANY vector under a given ``WaveformTable`` must have its OWN WFC
  (waveform character) defined in THAT table -- switching tables mid-pattern (this module's own
  mechanism for letting the TCK domain and a functional-clock domain coexist) means every
  signal needs an entry in EVERY table the pattern ever selects, even if that entry is just
  "hold" (``P``) or "don't-compare" (``X``) for a signal irrelevant to that table's own phase.
- ``STILParser.parse_semantic()``'s return value is always ``None`` regardless of outcome --
  real success is ``parser.is_parsing_done is True`` with ``parser.err_msg == ""`` (confirmed
  by reading its own source directly; a naive ``if parser.parse_semantic():`` would silently
  treat every file, valid or not, as failing).

**No real, independent-tool-confirmed convention exists for encoding a JTAG SIR/SDR-style scan
operation as STIL vectors** -- this session's own research found no public example (an EDA
vendor's own IJTAG-to-STIL translator was cited as existing, but its actual per-cycle
convention isn't public). This emitter's own convention -- one ``V{}`` per TCK edge; TMS/TDI
driven via :func:`warptap.tap_ir_play.navigation_tms`/:func:`~warptap.tap_ir_play.shift_tms`;
TDO compared (``H``/``L``) when a bit falls within a ``ShiftIR``/``ShiftDR``'s own ``mask``,
don't-compared (``X``) otherwise -- is a reasoned construction from STIL's own confirmed
primitives, not an established industry idiom, stated here rather than presented as settled
convention.

**One ``WaveformTable`` per distinct :class:`~warptap.tap_ir.PulsePin` target port**, each
declaring hold/force/don't-compare WFCs for every OTHER known signal (TCK/TMS/TDI/TDO, every
other pulse port, every ``hold_pins`` key seen anywhere in ``ir_ops``) -- the mechanism,
confirmed empirically against ``Semi-ATE-STIL``, that lets an independently-timed functional
clock pulse coexist with the TAP's own JTAG-speed ``WaveformTable`` in one ``Pattern``,
switched via a plain ``W <name>;`` statement.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Tuple, Union

from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR, bits_from_int
from warptap.tap_ir_play import navigation_tms, shift_tms
from warptap.tap_ports import TCK, TDI, TDO, TMS

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest, PulsePin]

_JTAG_WFT = "jtag_wft"
_JTAG_TIMING_DOMAIN = "tap_timing"
_PATTERN_NAME = "warptap_pattern"
_BURST_NAME = "warptap_burst"
_EXEC_NAME = "warptap_exec"

#: Fixed, small, unit-agnostic sub-cycle offsets for a WaveformTable's own internal edge
#: placement -- deliberately NOT computed as a fraction of the caller's own period string
#: (e.g. "50% of period"), since STIL's own time-expression grammar wasn't confirmed to
#: support percentage-of-period arithmetic this session; a caller-supplied period large
#: enough to fit both offsets (any real value above a few nanoseconds) is a reasonable, real
#: STIL-authoring expectation, not a new burden this module introduces.
_EDGE_OFFSET = "1ns"
_COMPARE_OFFSET = "2ns"


class TapIrStilError(RuntimeError):
    """Raised when ``to_stil`` is given an op it can't render, or a ``pulse_periods`` mapping
    missing an entry for some :class:`~warptap.tap_ir.PulsePin` target this ``ir_ops`` list
    actually uses."""


class _JtagCycle(NamedTuple):
    tms: int
    tdi: int
    tdo_compare: Optional[int]  # None = don't-compare this cycle


class _PulseCycle(NamedTuple):
    port: str
    hold_pins: Tuple[Tuple[str, int], ...]


def _walk_cycles(ir_ops: List[_IrOp]) -> List[Union[_JtagCycle, _PulseCycle]]:
    """One record per emitted STIL vector -- reuses the exact navigation/shift logic
    :mod:`warptap.tap_ir_play` already established, extended to also carry per-cycle
    TDO-compare information (:func:`~warptap.tap_ir_play.to_cycles` deliberately doesn't,
    being send-only) and :class:`~warptap.tap_ir.PulsePin` cycles."""
    state = TapState.RUN_TEST_IDLE
    cycles: List[Union[_JtagCycle, _PulseCycle]] = []

    def jtag_tick(tms: int, tdi: int, tdo_compare: Optional[int] = None) -> None:
        nonlocal state
        cycles.append(_JtagCycle(tms, tdi, tdo_compare))
        state = next_state(state, tms)

    for op in ir_ops:
        if isinstance(op, GotoState):
            for tms in navigation_tms(state, op.state):
                jtag_tick(tms, 0)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            fed_bits = bits_from_int(op.tdi, op.bits)
            tdo_bits = bits_from_int(op.tdo, op.bits) if op.tdo is not None else None
            mask_bits = bits_from_int(op.mask, op.bits) if op.mask is not None else None
            for i, (tms, tdi_bit) in enumerate(zip(shift_tms(op.bits), fed_bits)):
                compare = None
                if mask_bits is not None and mask_bits[i] and tdo_bits is not None:
                    compare = tdo_bits[i]
                jtag_tick(tms, tdi_bit, compare)
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                jtag_tick(0, 0)
        elif isinstance(op, PulsePin):
            for _ in range(op.count):
                cycles.append(_PulseCycle(op.port, op.hold_pins))
        else:
            raise TapIrStilError(f"to_stil() does not support op {op!r}")
    return cycles


def _collect_signals(cycles: List[Union[_JtagCycle, _PulseCycle]]) -> Tuple[List[str], List[str]]:
    """(all_signals, pulse_ports) -- ``all_signals`` always starts with TCK/TMS/TDI/TDO, then
    every distinct pulse port and ``hold_pins`` key, sorted for deterministic output.
    ``pulse_ports`` is the distinct set of :class:`_PulseCycle.port` values, one
    ``WaveformTable`` needed per entry."""
    pulse_ports: set = set()
    extra_signals: set = set()
    for cycle in cycles:
        if isinstance(cycle, _PulseCycle):
            pulse_ports.add(cycle.port)
            extra_signals.add(cycle.port)
            for name, _value in cycle.hold_pins:
                extra_signals.add(name)
    all_signals = [TCK, TMS, TDI, TDO] + sorted(extra_signals)
    return all_signals, sorted(pulse_ports)


def _jtag_waveforms(all_signals: List[str], pulse_ports: List[str]) -> str:
    lines = ["    Waveforms {"]
    for sig in all_signals:
        if sig == TCK:
            lines.append(f"      {TCK} {{ 01 {{ '0ns' D; '{_EDGE_OFFSET}' U; }}}}")
        elif sig in (TMS, TDI):
            lines.append(f"      {sig} {{ 01 {{ '0ns' D/U; }}}}")
        elif sig == TDO:
            lines.append(f"      {TDO} {{ HLX {{ '0ns' X; '{_COMPARE_OFFSET}' H/L/X; }}}}")
        else:
            # A PulsePin port or hold_pins signal, irrelevant while the JTAG table is
            # selected -- held at whatever it last was (never driven for the first time
            # under this table; every caller sequence starts inside jtag_wft's own reset
            # lead-in, matching every prior emitter's own precondition).
            lines.append(f"      {sig} {{ P {{ '0ns' P; }}}}")
    lines.append("    }")
    return "\n".join(lines)


def _pulse_waveforms(target_port: str, all_signals: List[str]) -> str:
    lines = ["    Waveforms {"]
    for sig in all_signals:
        if sig in (TCK, TMS, TDI):
            lines.append(f"      {sig} {{ P {{ '0ns' P; }}}}")
        elif sig == TDO:
            lines.append(f"      {TDO} {{ X {{ '0ns' X; }}}}")
        elif sig == target_port:
            lines.append(f"      {sig} {{ 01 {{ '0ns' D; '{_EDGE_OFFSET}' U; }}}}")
        else:
            # Another pulse port, or a hold_pins signal -- given a real 0/1 WFC pair
            # (explicit force, not "hold whatever was prior") since a hold_pins value can
            # be driven for the first time exactly during a pulse phase.
            lines.append(f"      {sig} {{ 01 {{ '0ns' D/U; }}}}")
    lines.append("    }")
    return "\n".join(lines)


def to_stil(
    ir_ops: List[_IrOp],
    *,
    jtag_period: str = "100ns",
    pulse_periods: Optional[Dict[str, str]] = None,
) -> str:
    """Render ``ir_ops`` as a complete STIL file: ``Signals``/``SignalGroups``/``Timing``
    (one ``WaveformTable`` for ordinary JTAG cycles, one more per distinct
    :class:`~warptap.tap_ir.PulsePin` target port)/``PatternBurst``/``PatternExec``/
    ``Pattern`` (block order matches this module's own empirically-confirmed requirement,
    see module docstring). ``jtag_period``/``pulse_periods`` are real caller inputs -- STIL
    always requires explicit timing, this project has no real frequency data to invent
    (matches :mod:`warptap.tap_ir_stapl`'s own ``NOTE`` field discipline). Raises
    :class:`TapIrStilError` for an unsupported op, or a ``PulsePin`` target port missing from
    ``pulse_periods``."""
    pulse_periods = pulse_periods or {}
    cycles = _walk_cycles(ir_ops)
    all_signals, pulse_ports = _collect_signals(cycles)
    for port in pulse_ports:
        if port not in pulse_periods:
            raise TapIrStilError(
                f"PulsePin target {port!r} has no entry in pulse_periods -- STIL always "
                "requires explicit per-signal timing, this project has no real period to "
                "invent for it"
            )

    signals_block = "Signals {\n" + "\n".join(
        f"  {sig} {'Out' if sig == TDO else 'In'};" for sig in all_signals
    ) + "\n}"
    group_expr = " + ".join(all_signals)
    signal_groups_block = f"SignalGroups {{\n  all_pins = '{group_expr}';\n}}"

    timing_lines = [f"Timing {_JTAG_TIMING_DOMAIN} {{"]
    timing_lines.append(f"  WaveformTable {_JTAG_WFT} {{")
    timing_lines.append(f"    Period '{jtag_period}';")
    timing_lines.append(_jtag_waveforms(all_signals, pulse_ports))
    timing_lines.append("  }")
    for port in pulse_ports:
        table_name = f"pulse_{port}_wft"
        timing_lines.append(f"  WaveformTable {table_name} {{")
        timing_lines.append(f"    Period '{pulse_periods[port]}';")
        timing_lines.append(_pulse_waveforms(port, all_signals))
        timing_lines.append("  }")
    timing_lines.append("}")
    timing_block = "\n".join(timing_lines)

    burst_block = f"PatternBurst {_BURST_NAME} {{\n  PatList {{ {_PATTERN_NAME}; }}\n}}"
    exec_block = (
        f"PatternExec {_EXEC_NAME} {{\n"
        f"  Timing {_JTAG_TIMING_DOMAIN};\n"
        f"  PatternBurst {_BURST_NAME};\n"
        "}"
    )

    pattern_lines = [f"Pattern {_PATTERN_NAME} {{"]
    current_wft: Optional[str] = None
    for cycle in cycles:
        if isinstance(cycle, _JtagCycle):
            if current_wft != _JTAG_WFT:
                pattern_lines.append(f"  W {_JTAG_WFT};")
                current_wft = _JTAG_WFT
            tdo_char = "X" if cycle.tdo_compare is None else ("H" if cycle.tdo_compare else "L")
            parts = [f"{TCK}=1; ", f"{TMS}={cycle.tms}; ", f"{TDI}={cycle.tdi}; ", f"{TDO}={tdo_char}; "]
            parts.append(_hold_others_as_prior(all_signals))
            pattern_lines.append(f"  V {{ {''.join(parts)}}}")
        else:
            table_name = f"pulse_{cycle.port}_wft"
            if current_wft != table_name:
                pattern_lines.append(f"  W {table_name};")
                current_wft = table_name
            hold_map = dict(cycle.hold_pins)
            parts = [f"{TCK}=P; {TMS}=P; {TDI}=P; {TDO}=X; {cycle.port}=1; "]
            for sig in all_signals:
                if sig in (TCK, TMS, TDI, TDO, cycle.port):
                    continue
                parts.append(f"{sig}={hold_map.get(sig, 0)}; ")
            pattern_lines.append(f"  V {{ {''.join(parts)}}}")
    pattern_lines.append("}")
    pattern_block = "\n".join(pattern_lines)

    return (
        "STIL 1.0;\n\n"
        f"{signals_block}\n\n"
        f"{signal_groups_block}\n\n"
        f"{timing_block}\n\n"
        f"{burst_block}\n\n"
        f"{exec_block}\n\n"
        f"{pattern_block}\n"
    )


def _hold_others_as_prior(all_signals: List[str]) -> str:
    """Every non-JTAG signal (a PulsePin port or hold_pins key), while the jtag_wft table is
    selected, holds via 'P' -- irrelevant to this phase, matches the 'P' WFC declared for it
    in :func:`_jtag_waveforms`."""
    parts = []
    for sig in all_signals:
        if sig in (TCK, TMS, TDI, TDO):
            continue
        parts.append(f"{sig}=P; ")
    return "".join(parts)
