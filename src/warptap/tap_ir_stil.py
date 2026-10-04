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

**Where each edge and strobe sits in a period** is :func:`to_stil`'s docstring. Semi-ATE-STIL's
syntax and semantic checks cannot see it, so ``tests/test_tap_ir_stil_replay.py`` replays
emitted files literally against ``rtl/tap_core.v`` and inserted designs in Icarus and counts
TDO mismatches; it caught the 0.0.3 strobe, which compared TDO after the rising TCK edge.

**One ``WaveformTable`` per distinct :class:`~warptap.tap_ir.PulsePin` target port**, each
declaring hold/force/don't-compare WFCs for every OTHER known signal (TCK/TMS/TDI/TDO, every
other pulse port, every ``hold_pins`` key seen anywhere in ``ir_ops``) -- the mechanism,
confirmed empirically against ``Semi-ATE-STIL``, that lets an independently-timed functional
clock pulse coexist with the TAP's own JTAG-speed ``WaveformTable`` in one ``Pattern``,
switched via a plain ``W <name>;`` statement.
"""

from __future__ import annotations

import re
from decimal import Decimal
from fractions import Fraction
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple, Union

from warptap.errors import WarptapError
from warptap.tap_fsm import TapState, next_state
from warptap.tap_ir import GotoState, PulsePin, Runtest, ShiftDR, ShiftIR, bits_from_int
from warptap.tap_ir_play import navigation_tms, shift_tms
from warptap.tap_ports import TCK, TDI, TDO, TMS

_IrOp = Union[ShiftIR, ShiftDR, GotoState, Runtest, PulsePin]
_TAP_PINS = (TCK, TMS, TDI, TDO)

_JTAG_WFT = "jtag_wft"
_JTAG_TIMING_DOMAIN = "tap_timing"
_PATTERN_NAME = "warptap_pattern"
_BURST_NAME = "warptap_burst"
_EXEC_NAME = "warptap_exec"

#: Where events sit in a period, as fractions of it; :func:`to_stil`'s docstring says why.
#: Inputs change at 0, TDO is compared at a quarter, and a clock (TCK, or a PulsePin's port)
#: rises at half and falls at three quarters, so it idles low between cycles.
_STROBE_AT = Fraction(1, 4)
_RISE_AT = Fraction(1, 2)
_FALL_AT = Fraction(3, 4)
_TIME = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(fs|ps|ns|us|ms|s)")


class TapIrStilError(WarptapError):
    """Raised when ``to_stil`` is given an op it can't render, or a ``pulse_periods`` mapping
    missing an entry for some :class:`~warptap.tap_ir.PulsePin` target this ``ir_ops`` list
    actually uses."""


def _offsets(period: str, what: str) -> Tuple[str, str, str]:
    """The strobe, rise and fall times within ``period``, written in its own unit."""
    match = _TIME.fullmatch(period)
    if match is None or Fraction(match.group(1)) == 0:
        raise TapIrStilError(f"{what} {period!r} is not a positive time with a unit, e.g. '50ns'")
    value, unit = Fraction(match.group(1)), match.group(2)

    def at(fraction: Fraction) -> str:
        scaled = value * fraction
        return format((Decimal(scaled.numerator) / scaled.denominator).normalize(), "f") + unit

    return at(_STROBE_AT), at(_RISE_AT), at(_FALL_AT)


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


class _Pins(NamedTuple):
    """The DUT pins a caller declares beyond TCK/TMS/TDI/TDO."""

    inputs: Dict[str, int]  # each with the value it holds
    outputs: Tuple[str, ...]


def _declared_pins(inputs: Optional[Mapping[str, int]], outputs: Iterable[str]) -> _Pins:
    pins = _Pins(dict(inputs or {}), tuple(outputs))
    for name in (*pins.inputs, *pins.outputs):
        if name in _TAP_PINS:
            raise TapIrStilError(f"{name!r} is a TAP pin; to_stil drives and compares it itself")
    for name, value in pins.inputs.items():
        if not isinstance(value, int) or value not in (0, 1):
            raise TapIrStilError(f"inputs[{name!r}] is {value!r}; an input holds 0 or 1")
    both = sorted(set(pins.inputs) & set(pins.outputs))
    if both:
        raise TapIrStilError(f"{both} declared both as inputs and as outputs")
    return pins


def _collect_signals(
    cycles: List[Union[_JtagCycle, _PulseCycle]], pins: _Pins
) -> Tuple[List[str], List[str]]:
    """(all_signals, pulse_ports) -- ``all_signals`` always starts with TCK/TMS/TDI/TDO, then
    every declared input and output, distinct pulse port and ``hold_pins`` key, sorted for
    deterministic output. ``pulse_ports`` is the distinct set of :class:`_PulseCycle.port`
    values, one ``WaveformTable`` needed per entry."""
    pulse_ports: set = set()
    extra_signals: set = set(pins.inputs) | set(pins.outputs)
    for cycle in cycles:
        if isinstance(cycle, _PulseCycle):
            pulse_ports.add(cycle.port)
            extra_signals.add(cycle.port)
            for name, value in cycle.hold_pins:
                if value not in (0, 1):
                    raise TapIrStilError(f"PulsePin hold_pins[{name!r}] is {value!r}, not 0 or 1")
                extra_signals.add(name)
            for name in (cycle.port, *(name for name, _ in cycle.hold_pins)):
                if name in _TAP_PINS or name in pins.outputs:
                    raise TapIrStilError(
                        f"PulsePin on {cycle.port!r} drives {name!r}, which is a TAP pin or "
                        "a declared output"
                    )
    all_signals = list(_TAP_PINS) + sorted(extra_signals)
    return all_signals, sorted(pulse_ports)


def _jtag_waveforms(all_signals: List[str], pins: _Pins, strobe: str, rise: str, fall: str) -> str:
    lines = ["    Waveforms {"]
    for sig in all_signals:
        if sig == TCK:
            lines.append(f"      {TCK} {{ 01 {{ '0ns' D; '{rise}' D/U; '{fall}' D; }}}}")
        elif sig in (TMS, TDI) or sig in pins.inputs:
            lines.append(f"      {sig} {{ 01 {{ '0ns' D/U; }}}}")
        elif sig == TDO:
            lines.append(f"      {TDO} {{ HLX {{ '0ns' X; '{strobe}' H/L/X; }}}}")
        elif sig in pins.outputs:
            lines.append(f"      {sig} {{ X {{ '0ns' X; }}}}")
        else:
            # A PulsePin port or hold_pins signal the caller didn't declare in ``inputs``:
            # held at whatever it last was, never driven by a JTAG cycle.
            lines.append(f"      {sig} {{ P {{ '0ns' P; }}}}")
    lines.append("    }")
    return "\n".join(lines)


def _pulse_waveforms(
    target_port: str, all_signals: List[str], pins: _Pins, rise: str, fall: str
) -> str:
    lines = ["    Waveforms {"]
    for sig in all_signals:
        if sig in (TCK, TMS, TDI):
            lines.append(f"      {sig} {{ P {{ '0ns' P; }}}}")
        elif sig == TDO or sig in pins.outputs:
            lines.append(f"      {sig} {{ X {{ '0ns' X; }}}}")
        elif sig == target_port:
            lines.append(f"      {sig} {{ 01 {{ '0ns' D; '{rise}' D/U; '{fall}' D; }}}}")
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
    inputs: Optional[Mapping[str, int]] = None,
    outputs: Iterable[str] = (),
) -> str:
    """Render ``ir_ops`` as a complete STIL file: ``Signals``/``SignalGroups``/``Timing``
    (one ``WaveformTable`` for ordinary JTAG cycles, one more per distinct
    :class:`~warptap.tap_ir.PulsePin` target port)/``PatternBurst``/``PatternExec``/
    ``Pattern`` (block order matches this module's own empirically-confirmed requirement,
    see module docstring). ``jtag_period``/``pulse_periods`` are real caller inputs -- STIL
    always requires explicit timing, this project has no real frequency data to invent
    (matches :mod:`warptap.tap_ir_stapl`'s own ``NOTE`` field discipline). Each is a positive
    number with a unit (``fs``, ``ps``, ``ns``, ``us``, ``ms`` or ``s``), e.g. ``'50ns'``; the
    edge times are written in the same unit. Raises :class:`TapIrStilError` for an
    unsupported op, a period it can't read, or a ``PulsePin`` target port missing from
    ``pulse_periods``.

    **Pins.** TCK, TMS, TDI (``In``) and TDO (``Out``) are always declared. ``inputs``
    declares the DUT's other inputs, each with the value (0 or 1) it holds: it is driven in
    every vector, except where a ``PulsePin`` pulses it or lists it in ``hold_pins``.
    ``outputs`` declares the DUT's other outputs: ``Out``, and never compared. A tester-ready
    file declares every DUT pin this way. A ``PulsePin`` port or ``hold_pins`` key left out of
    ``inputs`` is still declared ``In``, but only a pulse ever drives it: JTAG cycles hold
    whatever it had (``P``), nothing before its first pulse. Naming a TAP pin, a pin in both,
    or an output in a ``PulsePin`` raises :class:`TapIrStilError`.

    **Timing.** One ``jtag_wft`` vector is one TCK cycle of period ``T``:

    - TMS and TDI change at 0;
    - TDO is compared at T/4;
    - TCK rises at T/2 and falls at 3T/4, so it idles low between cycles.

    A vector's expected TDO bit is the one the TAP shows *before* that vector's rising edge,
    the edge that shifts it out: :meth:`~warptap.tap_model.TapModel.tick` returns it before it
    clocks, and ``ShiftIR``/``ShiftDR``'s ``tdo`` holds those bits. ``rtl/tap_core.v`` changes
    TDO on the rising edge, so that bit is valid from the previous rise (-T/2) to this one
    (T/2). A TAP that changes TDO on the falling edge, as IEEE 1149.1 requires, shows it from
    the previous fall (-T/4) to this cycle's fall (3T/4). A strobe at T/4 lies inside both
    windows and is at least T/4 from any TDO change in either. TMS and TDI get T/2 of setup
    before the rise and T/2 of hold after it. Up to 0.0.3, TCK rose at 1ns and TDO was compared
    at 2ns, after the rising edge, so on ``tap_core`` every compare checked the next bit.

    A ``PulsePin``'s port has the same shape in its own period: low at 0, rising at T/2,
    falling at 3T/4. Its ``hold_pins`` change at 0, half a period before the rising edge."""
    pulse_periods = pulse_periods or {}
    pins = _declared_pins(inputs, outputs)
    cycles = _walk_cycles(ir_ops)
    all_signals, pulse_ports = _collect_signals(cycles, pins)
    for port in pulse_ports:
        if port not in pulse_periods:
            raise TapIrStilError(
                f"PulsePin target {port!r} has no entry in pulse_periods -- STIL always "
                "requires explicit per-signal timing, this project has no real period to "
                "invent for it"
            )
    strobe, rise, fall = _offsets(jtag_period, "jtag_period")

    signals_block = "Signals {\n" + "\n".join(
        f"  {sig} {'Out' if sig == TDO or sig in pins.outputs else 'In'};" for sig in all_signals
    ) + "\n}"
    group_expr = " + ".join(all_signals)
    signal_groups_block = f"SignalGroups {{\n  all_pins = '{group_expr}';\n}}"

    timing_lines = [f"Timing {_JTAG_TIMING_DOMAIN} {{"]
    timing_lines.append(f"  WaveformTable {_JTAG_WFT} {{")
    timing_lines.append(f"    Period '{jtag_period}';")
    timing_lines.append(_jtag_waveforms(all_signals, pins, strobe, rise, fall))
    timing_lines.append("  }")
    for port in pulse_ports:
        table_name = f"pulse_{port}_wft"
        _, port_rise, port_fall = _offsets(pulse_periods[port], f"pulse_periods[{port!r}]")
        timing_lines.append(f"  WaveformTable {table_name} {{")
        timing_lines.append(f"    Period '{pulse_periods[port]}';")
        timing_lines.append(_pulse_waveforms(port, all_signals, pins, port_rise, port_fall))
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
            table_name = _JTAG_WFT
            tdo_char = "X" if cycle.tdo_compare is None else ("H" if cycle.tdo_compare else "L")
            values = [(TCK, "1"), (TMS, str(cycle.tms)), (TDI, str(cycle.tdi)), (TDO, tdo_char)]
            for sig in all_signals[len(_TAP_PINS):]:
                if sig in pins.outputs:
                    values.append((sig, "X"))
                elif sig in pins.inputs:
                    values.append((sig, str(pins.inputs[sig])))
                else:
                    values.append((sig, "P"))  # named only by a PulsePin: holds what it had
        else:
            table_name = f"pulse_{cycle.port}_wft"
            hold_map = dict(cycle.hold_pins)
            values = [(TCK, "P"), (TMS, "P"), (TDI, "P"), (TDO, "X")]
            for sig in all_signals[len(_TAP_PINS):]:
                if sig == cycle.port:
                    values.append((sig, "1"))
                elif sig in pins.outputs:
                    values.append((sig, "X"))
                elif sig in hold_map:
                    values.append((sig, str(hold_map[sig])))
                elif sig in pins.inputs:
                    values.append((sig, str(pins.inputs[sig])))
                else:
                    values.append((sig, "0"))
        if current_wft != table_name:
            pattern_lines.append(f"  W {table_name};")
            current_wft = table_name
        pattern_lines.append(f"  V {{ {''.join(f'{sig}={v}; ' for sig, v in values)}}}")
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
