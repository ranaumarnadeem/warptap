"""JTAG network-integrity patterns: the TCK-level program a production flow plays through the
TAP to test the TAP itself and the IJTAG network behind it, with the TDO it expects.

The TAP and the network are usually left out of scan (they run on TCK/TRST), and are tested
through TCK instead, with patterns derived from the network's own description. The program
here runs, in order:

1. ``reset_instruction``: TRST, then a DR scan of the reset instruction's register -- IDCODE
   (its value, then the fed sentinel after 32 bits), or BYPASS without one;
2. ``instruction_register``: the IR's capture pattern, then the fed sentinel after
   ``ir_width`` bits (its length); the scan loads BYPASS;
3. ``bypass``: BYPASS's captured 0 and one-bit delay;
4. ``opcode_<bits>``: every unimplemented opcode behaving as BYPASS (``exhaustive_opcodes``);
5. ``idcode``: an explicit IDCODE load (with IDCODE);
6. the TAP's own state machine and registers (``tap_paths``), each a raw TCK sequence:
   ``dr_paths`` (Run-Test/Idle held, a DR scan paused mid-register and resumed, Exit2-DR ->
   Update-DR, Update-DR -> Select-DR, Capture-DR -> Exit1-DR without a shift, then the
   register read whole again), ``ir_paths`` (the same for the IR, then Update-IR ->
   Select-DR), ``ir_capture_update`` (Capture-IR -> Exit1-IR -> Update-IR loads the capture
   pattern), ``select_ir_reset`` (Select-IR-Scan -> Test-Logic-Reset, held);
   ``idcode_hold``/``bypass_hold``/``ir_hold`` (each register held in Pause-DR/Pause-IR at
   0 and at 1 in every bit); ``idcode_capture``/``bypass_capture`` (each register captured
   while it holds the complement of its capture value). With the rest of the program, every
   one of the 32 TAP state transitions is taken;
7. ``network_closed``: the network's instruction (EXTEST, or IJTAG_ACCESS for a TAP with an
   ``ijtag_access_opcode``) with every SIB closed -- the network's length and continuity, by
   an over-shift probe (:mod:`warptap.sib_overshift`);
8. ``open_<slot>``: each SIB, and each ScanMux arm, opened alone from an all-closed network
   and probed the same way; then ``open_all``, every SIB outside a mux at once;
9. the network holding still (``network_hold``): ``sample_preload`` (SAMPLE/PRELOAD puts the
   network's tail on TDO for the whole scan without selecting the network; with
   IJTAG_ACCESS, ``extest`` and then ``sample_preload``, each a scan through BYPASS), then
   ``network_hold`` (one scan through the network as the probes left it, paused holding an
   alternating pattern and then its complement, which ends by closing every SIB);
10. ``write_readback_<instrument>``: each WRITE instrument written with an alternating
    pattern P and read back (Capture-DR captures what it last committed), then ~P, then 0
    (``write_readback``);
11. ``tms_reset``: five TMS=1 cycles (no TRST), then the reset instruction's scan again --
    IEEE 1149.1's Test-Logic-Reset reload.

Every probe starts from an all-closed network: :func:`~warptap.sib_retarget.
stage_open_sequence` leaves an already-open SIB it isn't asked about open, so each probe is
preceded by one scan that closes whatever the last one opened.

The expected TDO comes from :class:`~warptap.tap_model.TapModel` with
:class:`~warptap.sib_model.SibNetworkRegister` registered under the network's instruction, and
under SAMPLE/PRELOAD the network's tail (:meth:`~warptap.sib_model.SibNetworkRegister.tail`;
BYPASS with IJTAG_ACCESS), stepped through every TCK cycle. A READ instrument bound to a
design signal (a *live* one) captures a value the model can't know, so two models run in
lockstep, their live instruments capturing all 0s and all 1s; a TDO bit on which they
disagree carries a live value and is don't-care (``care=False``). A probe's fed bits fully
replace the chain's content, so a
captured value reaches TDO but never a SIB's state, and the two models only ever disagree
bit-for-bit. ``live_values`` pins a live instrument to a known value instead.

What the program can't see: whatever a TAP register does while another instruction is
selected (every DR read starts with a capture), decodes of the opcodes Update-IR never
latches (it normalizes every unimplemented one to BYPASS), TDO outside the shift states, and
mostly a SIB's capture (it recaptures the state it last committed, which its shift bit
normally already holds). A fault simulator grading the program finds those faults
undetected.

A consumer plays :attr:`IntegrityProgram.cycles` one TCK period each: TMS, TDI and TRST_N
applied for the period, TDO sampled before its rising edge on a ``shift`` cycle, and passes
what it saw to :func:`check_integrity`. :meth:`IntegrityProgram.to_json` writes the same
program as a self-contained file for a consumer without warptap (FaultFlow's ``jtag``
command).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence, Union

from warptap.errors import WarptapError
from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanMuxNode,
    SibNode,
    validate_physical_graph,
)
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_layout import compose_bits, layout_bit_length
from warptap.sib_model import SibNetworkRegister
from warptap.sib_overshift import build_overshift_ops, default_sentinel_pattern
from warptap.sib_retarget import stage_open_sequence
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, Runtest, ShiftDR, ShiftIR, bits_from_int, bits_to_int
from warptap.tap_ir_play import shift_op_ranges, to_cycles
from warptap.tap_model import (
    DEFAULT_IR_WIDTH,
    IDCODE_VALUE,
    OPCODE_EXTEST,
    OPCODE_IDCODE,
    OPCODE_SAMPLE_PRELOAD,
    Instruction,
    TapModel,
    bypass_opcode,
    idcode_value_error,
    ijtag_access_opcode_error,
)

PROGRAM_FORMAT = "warptap-tck-program"
PROGRAM_VERSION = 1

# (tms, tdi, trst_n) per TCK cycle; both end in Run-Test/Idle.
TRST_LEAD_IN: tuple[tuple[int, int, int], ...] = ((0, 0, 0), (0, 0, 0), (0, 0, 1))
TMS_RESET_LEAD_IN: tuple[tuple[int, int, int], ...] = ((1, 0, 1),) * 5 + ((0, 0, 1),)

_IrOp = Union[GotoState, ShiftIR, ShiftDR, Runtest]

# Raw (tms, tdi) cycles for the paths tap_ir_play doesn't navigate (Pause, Exit2, ...).
_TO_SHIFT_DR = ((1, 0), (0, 0), (0, 0))  # Run-Test/Idle -> Select-DR -> Capture-DR -> Shift-DR
_TO_SHIFT_IR = ((1, 0), (1, 0), (0, 0), (0, 0))  # ... -> Select-IR -> Capture-IR -> Shift-IR
_EXIT1_TO_IDLE = ((1, 0), (0, 0))  # Exit1 -> Update -> Run-Test/Idle
# Exit1 -> Pause, held two more cycles -> Exit2 -> Shift again (DR or IR alike).
_PAUSE_AND_RESUME = ((0, 0), (0, 0), (0, 0), (1, 0), (0, 0))


def _raw(pairs: Iterable[tuple[int, int]]) -> tuple[tuple[int, int, int], ...]:
    return tuple((tms, tdi, 1) for tms, tdi in pairs)


def _shift_cycles(bits: Sequence[int]) -> list[tuple[int, int]]:
    """Shift-IR/Shift-DR cycles feeding ``bits``, the last one exiting to Exit1."""
    return [(int(k == len(bits) - 1), bit) for k, bit in enumerate(bits)]


def _ir_load(opcode: int, ir_width: int) -> list[tuple[int, int]]:
    return [*_TO_SHIFT_IR, *_shift_cycles(bits_from_int(opcode, ir_width)), *_EXIT1_TO_IDLE]


def _dr_scan_cycles(bits: int, tdi: int) -> list[tuple[int, int]]:
    return [*_TO_SHIFT_DR, *_shift_cycles(bits_from_int(tdi, bits)), *_EXIT1_TO_IDLE]


class _NetworkTail:
    """SAMPLE/PRELOAD's data register in a SIB design: tap_core puts the network's tail on
    TDO, and the network, not selected, neither shifts nor updates; only its instrument
    leaves capture (:meth:`~warptap.sib_model.SibNetworkRegister.capture_unselected`)."""

    def __init__(self, network: SibNetworkRegister) -> None:
        self._network = network

    def capture(self) -> None:
        self._network.capture_unselected()

    def shift(self, tdi: int) -> int:
        return self._network.tail()

    def update(self) -> None:
        pass


class TapIntegrityError(WarptapError):
    """Raised for a program that can't be built from the given network and options, or an
    observed TDO stream that doesn't match the program's length."""


@dataclass(frozen=True)
class TapConfig:
    """The TAP the program targets -- ``rtl/tap_core.v``'s parameters. Its opcodes are
    :mod:`warptap.tap_model`'s (EXTEST 0, IDCODE 1, SAMPLE_PRELOAD 2, BYPASS all 1s), plus
    IJTAG_ACCESS at ``ijtag_access_opcode`` for a TAP built with one: the network's instruction
    then, and EXTEST and SAMPLE/PRELOAD decode to BYPASS."""

    ir_width: int = DEFAULT_IR_WIDTH
    has_idcode: bool = True
    idcode_value: int = IDCODE_VALUE
    ijtag_access_opcode: Optional[int] = None

    def __post_init__(self) -> None:
        if self.has_idcode and (problem := idcode_value_error(self.idcode_value)):
            raise TapIntegrityError(problem)
        if self.ijtag_access_opcode is not None and (
            problem := ijtag_access_opcode_error(self.ijtag_access_opcode, self.ir_width)
        ):
            raise TapIntegrityError(problem)

    def implemented_opcodes(self) -> frozenset[int]:
        if self.ijtag_access_opcode is None:
            opcodes = {OPCODE_EXTEST, OPCODE_SAMPLE_PRELOAD, bypass_opcode(self.ir_width)}
        else:
            opcodes = {self.ijtag_access_opcode, bypass_opcode(self.ir_width)}
        if self.has_idcode:
            opcodes.add(OPCODE_IDCODE)
        return frozenset(opcodes)

    def network_opcode(self) -> int:
        """The opcode that selects the network: EXTEST's, or IJTAG_ACCESS's."""
        return OPCODE_EXTEST if self.ijtag_access_opcode is None else self.ijtag_access_opcode

    def model(self) -> TapModel:
        return TapModel(
            ir_width=self.ir_width,
            has_idcode=self.has_idcode,
            idcode_value=self.idcode_value,
            ijtag_access_opcode=self.ijtag_access_opcode,
        )


class TckCycle(NamedTuple):
    tms: int
    tdi: int
    trst_n: int
    shift: bool  # resident in Shift-IR/Shift-DR for the period: TDO carries a bit
    tdo: int  # expected TDO; 0 unless ``shift``
    care: bool  # ``shift`` and the expected TDO doesn't depend on a live instrument
    test: str


class IntegrityTest(NamedTuple):
    name: str
    lead_in: tuple[tuple[int, int, int], ...]  # (tms, tdi, trst_n), ending in Run-Test/Idle
    ops: tuple[_IrOp, ...]  # played from Run-Test/Idle; empty for a program read from JSON


class IntegrityFailure(NamedTuple):
    test: str
    cycle: int  # index into IntegrityProgram.cycles
    expected: int
    observed: int  # -1 for anything other than 0 or 1 (an unknown value)


class IntegrityResult(NamedTuple):
    passed: bool
    compared: int  # care cycles compared
    failures: tuple[IntegrityFailure, ...]  # the first mismatch of each failing test


@dataclass(frozen=True)
class IntegrityProgram:
    tap: TapConfig
    tests: tuple[IntegrityTest, ...]
    cycles: tuple[TckCycle, ...]

    def test_ranges(self) -> list[tuple[str, int, int]]:
        """``(name, start, stop)`` of each test's cycles, in program order."""
        ranges: list[tuple[str, int, int]] = []
        for index, cycle in enumerate(self.cycles):
            if ranges and ranges[-1][0] == cycle.test:
                ranges[-1] = (cycle.test, ranges[-1][1], index + 1)
            else:
                ranges.append((cycle.test, index, index + 1))
        return ranges

    def to_json(self) -> dict[str, Any]:
        """The program as JSON data: ``tap``, ``tests`` (name and cycle range), and one
        string of ``0``/``1`` per cycle field -- ``tms``, ``tdi``, ``trst_n``, ``shift``,
        ``tdo``, ``care`` -- each character one TCK cycle. ``tap`` has an
        ``ijtag_access_opcode`` only for a TAP with one."""

        def column(field: str) -> str:
            return "".join(str(int(getattr(cycle, field))) for cycle in self.cycles)

        tap: dict[str, Any] = {
            "ir_width": self.tap.ir_width,
            "has_idcode": self.tap.has_idcode,
            "idcode_value": self.tap.idcode_value,
        }
        if self.tap.ijtag_access_opcode is not None:
            tap["ijtag_access_opcode"] = self.tap.ijtag_access_opcode
        return {
            "format": PROGRAM_FORMAT,
            "version": PROGRAM_VERSION,
            "tap": tap,
            "tests": [
                {"name": name, "start": start, "stop": stop}
                for name, start, stop in self.test_ranges()
            ],
            **{field: column(field) for field in ("tms", "tdi", "trst_n", "shift", "tdo", "care")},
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> IntegrityProgram:
        """The inverse of :meth:`to_json`. The tests carry only their names -- the cycles
        are the program."""
        if data.get("format") != PROGRAM_FORMAT or data.get("version") != PROGRAM_VERSION:
            raise TapIntegrityError(
                f"not a {PROGRAM_FORMAT} v{PROGRAM_VERSION} file "
                f"(format={data.get('format')!r}, version={data.get('version')!r})"
            )
        columns = {f: str(data[f]) for f in ("tms", "tdi", "trst_n", "shift", "tdo", "care")}
        count = len(columns["tms"])
        if any(len(c) != count or set(c) - {"0", "1"} for c in columns.values()):
            raise TapIntegrityError("cycle columns must be equal-length strings of 0/1")
        test_of = [""] * count
        for entry in data["tests"]:
            for index in range(int(entry["start"]), int(entry["stop"])):
                test_of[index] = str(entry["name"])
        cycles = tuple(
            TckCycle(
                tms=int(columns["tms"][i]),
                tdi=int(columns["tdi"][i]),
                trst_n=int(columns["trst_n"][i]),
                shift=columns["shift"][i] == "1",
                tdo=int(columns["tdo"][i]),
                care=columns["care"][i] == "1",
                test=test_of[i],
            )
            for i in range(count)
        )
        tap = data["tap"]
        ijtag_access_opcode = tap.get("ijtag_access_opcode")
        return cls(
            tap=TapConfig(
                ir_width=int(tap["ir_width"]),
                has_idcode=bool(tap["has_idcode"]),
                idcode_value=int(tap["idcode_value"]),
                ijtag_access_opcode=(
                    None if ijtag_access_opcode is None else int(ijtag_access_opcode)
                ),
            ),
            tests=tuple(
                IntegrityTest(str(entry["name"]), (), ()) for entry in data["tests"]
            ),
            cycles=cycles,
        )


def select_instruction(opcode: int, *, ir_width: int = DEFAULT_IR_WIDTH) -> list[_IrOp]:
    """Run-Test/Idle -> shift ``opcode`` into the instruction register -> Run-Test/Idle."""
    if not 0 <= opcode < (1 << ir_width):
        raise ValueError(f"opcode {opcode:#x} does not fit a {ir_width}-bit instruction register")
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(ir_width, tdi=opcode),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def build_integrity_program(
    graph: PhysicalGraph,
    root: ModuleInstance,
    *,
    tap: TapConfig = TapConfig(),
    live: Optional[Iterable[str]] = None,
    live_values: Optional[Mapping[str, int]] = None,
    margin: int = 8,
    exhaustive_opcodes: bool = True,
    write_readback: bool = True,
    tap_paths: bool = True,
    network_hold: bool = True,
) -> IntegrityProgram:
    """The integrity program for ``graph`` (and ``root``, its dotted-address tree, for the
    write/readback tests) behind a TAP configured as ``tap``.

    ``live`` names the READ instruments whose capture depends on the design; the default is
    every READ instrument bound to a design signal. ``live_values`` gives the captured value
    of some of them, which makes their bits care bits. ``margin`` is the number of sentinel
    bits fed past each register's length (at least 1: a register's length shows only when a
    fed bit comes back out). ``exhaustive_opcodes``, ``write_readback``, ``tap_paths`` and
    ``network_hold`` each include a group of tests (see the module docstring)."""
    if margin < 1:
        raise TapIntegrityError(f"margin must be >= 1, got {margin}")
    validate_physical_graph(graph)
    instruments = _instruments(graph.chain)
    live_names = _live_instruments(instruments, live)
    values = dict(live_values or {})
    unknown = sorted(set(values) - live_names)
    if unknown:
        raise TapIntegrityError(f"live_values names instruments that aren't live: {unknown}")
    for name, value in values.items():
        if not 0 <= value < (1 << instruments[name].width):
            raise TapIntegrityError(
                f"live value {value:#x} does not fit {name!r}'s {instruments[name].width} bits"
            )
    low = {n: values.get(n, 0) for n in live_names}
    high = {n: values.get(n, (1 << instruments[n].width) - 1) for n in live_names}
    tests = _integrity_tests(
        graph,
        root,
        instruments,
        tap,
        margin,
        exhaustive_opcodes=exhaustive_opcodes,
        write_readback=write_readback,
        tap_paths=tap_paths,
        network_hold=network_hold,
    )
    cycles = _expected_cycles(
        tests,
        tap,
        PhysicalGraph(chain=_recaptured(graph.chain, low)),
        PhysicalGraph(chain=_recaptured(graph.chain, high)),
    )
    return IntegrityProgram(tap=tap, tests=tuple(tests), cycles=tuple(cycles))


def check_integrity(program: IntegrityProgram, observed_tdo: Sequence[object]) -> IntegrityResult:
    """Compare ``observed_tdo`` (one TDO value per program cycle) with the expected TDO on
    every care cycle, naming the first mismatch of each failing test."""
    if len(observed_tdo) != len(program.cycles):
        raise TapIntegrityError(
            f"observed {len(observed_tdo)} TDO values for a {len(program.cycles)}-cycle program"
        )
    failures: list[IntegrityFailure] = []
    failed: set[str] = set()
    compared = 0
    for index, (cycle, seen) in enumerate(zip(program.cycles, observed_tdo)):
        if not cycle.care:
            continue
        compared += 1
        value = int(seen) if isinstance(seen, int) and seen in (0, 1) else -1
        if value != cycle.tdo and cycle.test not in failed:
            failed.add(cycle.test)
            failures.append(IntegrityFailure(cycle.test, index, cycle.tdo, value))
    return IntegrityResult(passed=not failures, compared=compared, failures=tuple(failures))


def _instruments(chain: tuple) -> dict[str, InstrumentNode]:
    found: dict[str, InstrumentNode] = {}
    for slot in chain:
        if isinstance(slot, ScanMuxNode):
            for arm in slot.arms:
                if arm.instrument is not None:
                    found[arm.instrument.name] = arm.instrument
                found.update(_instruments(arm.nested))
        else:
            if slot.instrument is not None:
                found[slot.instrument.name] = slot.instrument
            found.update(_instruments(slot.nested))
    return found


def _live_instruments(
    instruments: Mapping[str, InstrumentNode], live: Optional[Iterable[str]]
) -> set[str]:
    if live is None:
        return {
            name
            for name, ins in instruments.items()
            if ins.direction is InstrumentDirection.READ and ins.signal_bits
        }
    names = set(live)
    unknown = sorted(n for n in names if n not in instruments)
    if unknown:
        raise TapIntegrityError(f"live names unknown instruments: {unknown}")
    writes = sorted(n for n in names if instruments[n].direction is InstrumentDirection.WRITE)
    if writes:
        raise TapIntegrityError(
            f"live names WRITE instruments, which capture their own last write: {writes}"
        )
    return names


def _recaptured(chain: tuple, capture_of: Mapping[str, int]) -> tuple:
    """``chain`` with each instrument named in ``capture_of`` capturing that value."""

    def instrument(ins: Optional[InstrumentNode]) -> Optional[InstrumentNode]:
        if ins is not None and ins.name in capture_of:
            return ins._replace(capture_value=capture_of[ins.name])
        return ins

    slots: list = []
    for slot in chain:
        if isinstance(slot, ScanMuxNode):
            arms = tuple(
                arm._replace(
                    instrument=instrument(arm.instrument),
                    nested=_recaptured(arm.nested, capture_of),
                )
                for arm in slot.arms
            )
            slots.append(slot._replace(arms=arms))
        else:
            slots.append(
                slot._replace(
                    instrument=instrument(slot.instrument),
                    nested=_recaptured(slot.nested, capture_of),
                )
            )
    return tuple(slots)


def _probe_targets(chain: tuple) -> list[tuple[str, Union[frozenset, dict]]]:
    """(name, open-set) for every SIB and every ScanMux arm, depth first."""
    targets: list[tuple[str, Union[frozenset, dict]]] = []
    for slot in chain:
        if isinstance(slot, ScanMuxNode):
            for arm in slot.arms:
                value = arm.values[0]
                targets.append((f"{slot.mux_name}_{value}", {slot.mux_name: value}))
                targets.extend(_probe_targets(arm.nested))
        else:
            targets.append((slot.sib_name, frozenset({slot.sib_name})))
            targets.extend(_probe_targets(slot.nested))
    return targets


def _sibs_outside_muxes(chain: tuple) -> list[str]:
    names: list[str] = []
    for slot in chain:
        if isinstance(slot, SibNode):
            names.append(slot.sib_name)
            names.extend(_sibs_outside_muxes(slot.nested))
    return names


def _opened(graph: PhysicalGraph, target: Union[frozenset, dict]) -> dict:
    """What's physically open once ``target`` is reached from an all-closed network: the
    target plus its ancestors."""
    return dict(stage_open_sequence(graph, target)[-1])


def _close_all(graph: PhysicalGraph, opened: Union[frozenset, dict]) -> list[_IrOp]:
    """One DR scan, under the network's instruction, that closes every SIB (and bypasses every
    mux) open in ``opened``, content fed 0. A WRITE instrument doesn't commit on its SIB's
    closing edge."""
    if not opened:
        return []
    bits = compose_bits(graph, opened, {}, target_sib=None, payload_value=0)
    return [
        GotoState(TapState.SHIFT_DR),
        ShiftDR(layout_bit_length(graph, opened), tdi=bits_to_int(list(reversed(bits)))),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _dotted_address(root: ModuleInstance, name: str) -> str:
    paths: list[str] = []

    def walk(node: ModuleInstance, prefix: str) -> None:
        for child in node.children:
            dotted = f"{prefix}.{child.name}" if prefix else child.name
            if child.name == name:
                paths.append(dotted)
            walk(child, dotted)

    walk(root, "")
    if len(paths) != 1:
        raise TapIntegrityError(
            f"instrument {name!r} has {len(paths)} addresses under {root.name!r}, need one"
        )
    return paths[0]


def _alternating(width: int) -> int:
    """0b...0101: bit 0 set, every other bit after it."""
    return sum(1 << k for k in range(0, width, 2))


def _dr_scan(bits: int, tdi: int) -> list[_IrOp]:
    return [
        GotoState(TapState.SHIFT_DR),
        ShiftDR(bits, tdi=tdi),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _tap_path_tests(tap: TapConfig, margin: int) -> list[IntegrityTest]:
    """The TAP's state machine and its own registers, as raw TCK sequences. The reset
    instruction's register (IDCODE, or BYPASS without one) carries the DR paths."""
    ir_width = tap.ir_width
    reset_opcode = OPCODE_IDCODE if tap.has_idcode else bypass_opcode(ir_width)
    reset_bits = (32 if tap.has_idcode else 1) + margin
    sentinel = default_sentinel_pattern(reset_bits)
    fed = bits_from_int(sentinel, reset_bits)
    split = max(1, reset_bits // 4)
    tests = []

    seq = _ir_load(reset_opcode, ir_width) + [(0, 0), (0, 0)]  # Run-Test/Idle held
    seq += [*_TO_SHIFT_DR, *_shift_cycles(fed[:split]), *_PAUSE_AND_RESUME]
    seq += _shift_cycles(fed[split:])
    seq += [(0, 0), (1, 0), (1, 0)]  # Exit1 -> Pause -> Exit2 -> Update
    seq += [(1, 0), (0, 0), (1, 0), (1, 0), (0, 0)]  # -> Select -> Capture -> Exit1 -> Update
    seq += _dr_scan_cycles(reset_bits, sentinel)  # -> Idle; the register read whole again
    tests.append(IntegrityTest("dr_paths", _raw(seq), ()))

    ones = [1] * (ir_width + margin)  # BYPASS, fed through the IR's length
    split = max(1, ir_width // 2)
    seq = [*_TO_SHIFT_IR, *_shift_cycles(ones[:split]), *_PAUSE_AND_RESUME]
    seq += _shift_cycles(ones[split:])
    seq += [(0, 0), (1, 0), (1, 0)]  # Exit1-IR -> Pause-IR -> Exit2-IR -> Update-IR
    seq += [(1, 0), (0, 0), (0, 0)]  # Update-IR -> Select-DR -> Capture-DR -> Shift-DR
    seq += _shift_cycles(bits_from_int(default_sentinel_pattern(1 + margin), 1 + margin))
    seq += _EXIT1_TO_IDLE
    tests.append(IntegrityTest("ir_paths", _raw(seq), ()))

    # Capture-IR -> Exit1-IR -> Update-IR: the capture pattern itself is loaded.
    seq = [(1, 0), (1, 0), (0, 0), (1, 0), (1, 0), (0, 0)]
    seq += _dr_scan_cycles(reset_bits, sentinel)
    tests.append(IntegrityTest("ir_capture_update", _raw(seq), ()))

    # Select-IR-Scan -> Test-Logic-Reset, held there, from BYPASS.
    seq = _ir_load(bypass_opcode(ir_width), ir_width) + [(1, 0), (1, 0), (1, 0), (1, 0), (0, 0)]
    seq += _dr_scan_cycles(reset_bits, sentinel)
    tests.append(IntegrityTest("select_ir_reset", _raw(seq), ()))

    # Each register held in Pause at 0 and at 1 in every bit: an alternating pattern, then
    # its complement, each pushed out by the next.
    if tap.has_idcode:
        pattern = _alternating(32)
        seq = _ir_load(OPCODE_IDCODE, ir_width) + list(_TO_SHIFT_DR)
        seq += [*_shift_cycles(bits_from_int(pattern, 32)), *_PAUSE_AND_RESUME]
        seq += [*_shift_cycles(bits_from_int(~pattern, 32)), *_PAUSE_AND_RESUME]
        seq += [*_shift_cycles(bits_from_int(sentinel, reset_bits)), *_EXIT1_TO_IDLE]
        tests.append(IntegrityTest("idcode_hold", _raw(seq), ()))
    seq = _ir_load(bypass_opcode(ir_width), ir_width) + list(_TO_SHIFT_DR)
    seq += [*_shift_cycles([1]), *_PAUSE_AND_RESUME, *_shift_cycles([0, 0])]
    seq += [*_PAUSE_AND_RESUME, *_shift_cycles([1, 1]), *_EXIT1_TO_IDLE]
    tests.append(IntegrityTest("bypass_hold", _raw(seq), ()))
    pattern = _alternating(ir_width)
    seq = [*_TO_SHIFT_IR, *_shift_cycles(bits_from_int(pattern, ir_width)), *_PAUSE_AND_RESUME]
    seq += [*_shift_cycles(bits_from_int(~pattern, ir_width)), *_PAUSE_AND_RESUME]
    seq += [*_shift_cycles(ones), *_EXIT1_TO_IDLE]
    tests.append(IntegrityTest("ir_hold", _raw(seq), ()))

    # Each register captured while it holds the complement of what it captures.
    if tap.has_idcode:
        seq = _ir_load(OPCODE_IDCODE, ir_width) + _dr_scan_cycles(32, ~tap.idcode_value)
        seq += _dr_scan_cycles(reset_bits, sentinel)
        tests.append(IntegrityTest("idcode_capture", _raw(seq), ()))
    seq = _ir_load(bypass_opcode(ir_width), ir_width) + _dr_scan_cycles(2, 0b11)
    seq += _dr_scan_cycles(1 + margin, default_sentinel_pattern(1 + margin))
    tests.append(IntegrityTest("bypass_capture", _raw(seq), ()))
    return tests


def _network_hold_tests(
    graph: PhysicalGraph, opened: Union[frozenset, dict], tap: TapConfig, margin: int
) -> list[IntegrityTest]:
    """SAMPLE/PRELOAD (with IJTAG_ACCESS, EXTEST and then SAMPLE/PRELOAD, both BYPASS there),
    then one scan under the network's instruction, paused through the network as ``opened``
    (what the probes left open), holding an alternating pattern and then its complement; the
    scan ends by closing every SIB."""
    ir_width = tap.ir_width
    tests = []
    bits = len(graph.chain) + margin
    board = [("sample_preload", OPCODE_SAMPLE_PRELOAD)]
    if tap.ijtag_access_opcode is not None:
        board.insert(0, ("extest", OPCODE_EXTEST))
    for name, opcode in board:
        seq = _ir_load(opcode, ir_width)
        seq += _dr_scan_cycles(bits, default_sentinel_pattern(bits))
        tests.append(IntegrityTest(name, _raw(seq), ()))

    length = layout_bit_length(graph, opened)
    pattern = _alternating(length)
    close = list(reversed(compose_bits(graph, opened, {}, target_sib=None, payload_value=0)))
    seq = _ir_load(tap.network_opcode(), ir_width) + list(_TO_SHIFT_DR)
    seq += [*_shift_cycles(bits_from_int(pattern, length)), *_PAUSE_AND_RESUME]
    seq += [*_shift_cycles(bits_from_int(~pattern, length)), *_PAUSE_AND_RESUME]
    seq += [*_shift_cycles(close), *_EXIT1_TO_IDLE]
    tests.append(IntegrityTest("network_hold", _raw(seq), ()))
    return tests


def _integrity_tests(
    graph: PhysicalGraph,
    root: ModuleInstance,
    instruments: Mapping[str, InstrumentNode],
    tap: TapConfig,
    margin: int,
    *,
    exhaustive_opcodes: bool,
    write_readback: bool,
    tap_paths: bool,
    network_hold: bool,
) -> list[IntegrityTest]:
    ir_width = tap.ir_width
    reset_dr = 32 if tap.has_idcode else 1

    def sentinel_scan(register_bits: int) -> list[_IrOp]:
        return _dr_scan(register_bits + margin, default_sentinel_pattern(register_bits + margin))

    tests = [IntegrityTest("reset_instruction", TRST_LEAD_IN, tuple(sentinel_scan(reset_dr)))]

    # The sentinel first, then BYPASS's opcode: what Update-IR loads is the last fed bits.
    fed = bits_from_int(default_sentinel_pattern(margin), margin) + [1] * ir_width
    ir_scan = [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(ir_width + margin, tdi=bits_to_int(fed)),
        GotoState(TapState.RUN_TEST_IDLE),
    ]
    tests.append(IntegrityTest("instruction_register", (), tuple(ir_scan)))
    tests.append(IntegrityTest("bypass", (), tuple(sentinel_scan(1))))

    if exhaustive_opcodes:
        for opcode in range(1 << ir_width):
            if opcode not in tap.implemented_opcodes():
                ops = select_instruction(opcode, ir_width=ir_width) + sentinel_scan(1)
                tests.append(IntegrityTest(f"opcode_{opcode:0{ir_width}b}", (), tuple(ops)))

    if tap.has_idcode:
        ops = select_instruction(OPCODE_IDCODE, ir_width=ir_width) + sentinel_scan(32)
        tests.append(IntegrityTest("idcode", (), tuple(ops)))

    if tap_paths:
        tests.extend(_tap_path_tests(tap, margin))

    if graph.chain:
        network = select_instruction(tap.network_opcode(), ir_width=ir_width)
        probe = build_overshift_ops(graph, frozenset(), margin=margin)
        tests.append(IntegrityTest("network_closed", (), tuple(network + probe)))
        opened: dict = {}
        targets = _probe_targets(graph.chain)
        every_sib = _sibs_outside_muxes(graph.chain)
        if len(every_sib) > 1:
            targets.append(("all", frozenset(every_sib)))
        for name, target in targets:
            ops = _close_all(graph, opened) + build_overshift_ops(graph, target, margin=margin)
            opened = _opened(graph, target)
            tests.append(IntegrityTest(f"open_{name}", (), tuple(ops)))

        if network_hold:
            tests.extend(_network_hold_tests(graph, opened, tap, margin))
            opened = {}  # network_hold closes every SIB

        writes = [i for i in instruments.values() if i.direction is InstrumentDirection.WRITE]
        if write_readback and writes:
            pdl = PDLInterpreter(graph, root)  # starts from the all-closed network
            closing = _close_all(graph, opened)
            for ins in writes:
                start = len(pdl.program)
                dotted = _dotted_address(root, ins.name)
                pattern = _alternating(ins.width)
                for value in (pattern, pattern ^ ((1 << ins.width) - 1), 0):
                    pdl.iTarget(dotted)
                    pdl.iWrite(value)
                    pdl.iApply()
                    pdl.iTarget(dotted)
                    pdl.iRead(value)
                    pdl.iApply()
                ops = closing + list(pdl.program[start:])
                closing = []
                tests.append(IntegrityTest(f"write_readback_{ins.name}", (), tuple(ops)))

    tests.append(IntegrityTest("tms_reset", TMS_RESET_LEAD_IN, tuple(sentinel_scan(reset_dr))))
    return tests


def _expected_cycles(
    tests: Sequence[IntegrityTest],
    tap: TapConfig,
    low: PhysicalGraph,
    high: PhysicalGraph,
) -> list[TckCycle]:
    """Step two lockstep models (live instruments capturing all 0s / all 1s) through every
    test, one TCK cycle at a time. Also checks each PDL read expectation against them."""
    pairs = []
    for graph in (low, high):
        model = tap.model()
        network = SibNetworkRegister(graph)
        model.register_data_register(model.network_instruction, network)
        if graph.chain and tap.ijtag_access_opcode is None:
            model.register_data_register(Instruction.SAMPLE_PRELOAD, _NetworkTail(network))
        pairs.append((model, network))

    cycles: list[TckCycle] = []
    for test in tests:
        for tms, tdi, trst_n in test.lead_in:
            if not trst_n:
                for model, network in pairs:
                    model.reset()
                    network.reset()
                cycles.append(TckCycle(tms, tdi, 0, False, 0, False, test.name))
            else:
                cycles.append(_tick(pairs, tms, tdi, test.name))
        if pairs[0][0].state is not TapState.RUN_TEST_IDLE:
            raise TapIntegrityError(f"test {test.name!r} does not start in Run-Test/Idle")
        first = len(cycles)
        for tms, tdi in to_cycles(list(test.ops)):
            cycles.append(_tick(pairs, tms, tdi, test.name))
        _check_reads(test, cycles[first:])
    return cycles


def _tick(pairs: list, tms: int, tdi: int, test: str) -> TckCycle:
    shift = pairs[0][0].state in (TapState.SHIFT_IR, TapState.SHIFT_DR)
    low_tdo, high_tdo = (model.tick(tms, tdi) for model, _network in pairs)
    return TckCycle(
        tms, tdi, 1, shift, low_tdo if shift else 0, shift and low_tdo == high_tdo, test
    )


def _check_reads(test: IntegrityTest, cycles: Sequence[TckCycle]) -> None:
    """A PDL iRead's expected value (an op's tdo/mask) must be what the models shift out:
    the program's own consistency check, not a check of any hardware."""
    shift_ops = [op for op in test.ops if isinstance(op, (ShiftIR, ShiftDR))]
    for op, (start, bits) in zip(shift_ops, shift_op_ranges(list(test.ops))):
        if op.mask is None or op.tdo is None:
            continue
        window = cycles[start : start + bits]
        if not all(c.care for k, c in enumerate(window) if (op.mask >> k) & 1):
            raise TapIntegrityError(f"test {test.name!r} reads a live bit")
        seen = sum(c.tdo << k for k, c in enumerate(window))
        if (seen ^ op.tdo) & op.mask:
            raise TapIntegrityError(
                f"test {test.name!r}: the network model shifts out {seen:#x} where the PDL "
                f"read expects {op.tdo:#x} (mask {op.mask:#x})"
            )
