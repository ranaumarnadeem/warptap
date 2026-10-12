"""The integrity program of an IEEE 1500 wrapper serial port: WSP cycles that exercise the
WIR, the bypass register and the boundary register, each with the value the model
(:mod:`warptap.wsp1500_model`) expects at every observation point -- the test a production
flow runs on the wrapper's own infrastructure, in place of scan ATPG, which holds the
instruction still and so can't.

Tests, in program order:

- ``reset``: WRSTN, then a sentinel pattern through the bypass registers, which must come
  out ``N`` shifts late, ``N`` the wrappers of the chain (WBY is one bit, and WRSTN and
  CaptureWR clear it).
- ``wir_length``: a sentinel through the WIRs, which must come out as many shifts late as the
  WIRs are long; the pattern ends WS_BYPASS, so the next test starts from it.
- ``code_<bits>`` (``u<k>_code_<bits>`` in a chain): every code of the WIR, one wrapper at
  a time with the others in bypass -- an unused code included, which acts as WS_BYPASS --
  loaded, then the data register it selects scanned with a sentinel: the bypass bit, or in
  a serial instruction the boundary register, captured from the ports the program drives
  (twice, with complementary ports) and shifted out, so every cell and the register's
  length show. Then idle cycles, in which the mode's parallel outputs are observed.
- ``held_update``: an update while the WIR is deselected, after a new code is shifted into
  the WIR's shift stage, leaves the instruction in force.
- ``capture_update``: CaptureWR with the WIR selected doesn't capture the boundary cells.

Every cycle carries what the model expects at WSO, at each wrapped output and at each
wrapper chain's scan output (:class:`ObservePoint`). What the program can't know -- the
core's outputs, which an output cell captures -- is no care: the model runs twice, with
those at 0 and at 1, and an observation is checked only where the runs agree. A program
is JSON, a ``warptap-wsp-program``, which a tool without warptap can read and play.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple, Optional, Sequence, Union

from warptap.errors import WarptapError
from warptap.icl_emit_1500 import ROLES
from warptap.tap_ir import Runtest, ShiftDR, ShiftIR
from warptap.wsp1500_model import (
    INPUT,
    OUTPUT,
    Value,
    Wrapper1500,
    Wsp1500Chain,
    WspChainDesign,
    WspCycle,
)
from warptap.wsp_ir import WspReset
from warptap.wsp_play import to_wsp_rows

FORMAT = "warptap-wsp-program"
VERSION = 1
_SENTINEL = (1, 0, 1, 1, 0, 0, 1, 0)
_SIGNALS = ("wrstn", "selectwir", "shiftwr", "capturewr", "updatewr", "wsi", "pulse")

Design = Union[Wrapper1500, WspChainDesign]


class WspIntegrityError(WarptapError):
    """A program that can't be built or read."""


@dataclass(frozen=True)
class ObservePoint:
    """Where the program checks the wrapper: WSO, a wrapped output port (``output``:
    the cell ``index`` of wrapper ``wrapper``), or a wrapper chain's scan output
    (``scan_out``: chain ``index``). ``name``: ``wso``, the port's label, the scan-out
    port's name -- prefixed ``u<k>.`` in a chain."""

    name: str
    kind: str
    wrapper: int
    index: int


class Step(NamedTuple):
    """One tester cycle: the cycle, its test, the data input ports driven (in
    ``drive`` order), and the expected value at each observation point (None: no care)."""

    cycle: WspCycle
    test: str
    ports: tuple[int, ...]
    expect: tuple[Optional[int], ...]


@dataclass(frozen=True)
class WspIntegrityProgram:
    """The steps, their tests and what they drive and observe. ``design`` is the model
    the program was built from (None for one read from JSON)."""

    steps: tuple[Step, ...]
    tests: tuple[tuple[str, int, int], ...]
    observe: tuple[ObservePoint, ...]
    drive: tuple[str, ...]
    roles: Mapping[str, str]
    design: Optional[Design] = None

    def __len__(self) -> int:
        return len(self.steps)

    def test_of(self, step: int) -> str:
        for name, start, stop in self.tests:
            if start <= step < stop:
                return name
        raise IndexError(step)

    def to_json(self) -> dict[str, Any]:
        columns: dict[str, Any] = {
            signal: "".join(
                str(int(bool(getattr(s.cycle, signal)))) for s in self.steps
            )
            for signal in _SIGNALS
        }
        return {
            "format": FORMAT,
            "version": VERSION,
            "roles": dict(self.roles),
            "drive": list(self.drive),
            "observe": [
                {"name": p.name, "kind": p.kind, "wrapper": p.wrapper, "index": p.index}
                for p in self.observe
            ],
            "tests": [
                {"name": name, "start": start, "stop": stop}
                for name, start, stop in self.tests
            ],
            **columns,
            "inputs": {
                name: "".join(str(s.ports[k]) for s in self.steps)
                for k, name in enumerate(self.drive)
            },
            "expect": {
                p.name: {
                    "value": "".join(
                        "0" if s.expect[k] is None else str(s.expect[k])
                        for s in self.steps
                    ),
                    "care": "".join(
                        "0" if s.expect[k] is None else "1" for s in self.steps
                    ),
                }
                for k, p in enumerate(self.observe)
            },
        }

    def digest(self) -> str:
        text = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "WspIntegrityProgram":
        if data.get("format") != FORMAT or data.get("version") != VERSION:
            raise WspIntegrityError(
                f"not a {FORMAT} v{VERSION} program "
                f"(format={data.get('format')!r}, version={data.get('version')!r})"
            )
        try:
            observe = tuple(
                ObservePoint(str(o["name"]), str(o["kind"]), int(o["wrapper"]), int(o["index"]))
                for o in data["observe"]
            )
            drive = tuple(str(name) for name in data["drive"])
            count = len(str(data["wrstn"]))
            columns = {signal: str(data[signal]) for signal in _SIGNALS}
            inputs = {name: str(data["inputs"][name]) for name in drive}
            expect = {
                p.name: (str(data["expect"][p.name]["value"]), str(data["expect"][p.name]["care"]))
                for p in observe
            }
            tests = tuple(
                (str(t["name"]), int(t["start"]), int(t["stop"])) for t in data["tests"]
            )
            roles = {str(k): str(v) for k, v in data["roles"].items()}
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise WspIntegrityError(f"malformed WSP program: {exc}") from exc
        if count == 0:
            raise WspIntegrityError("WSP program has no cycles")
        every = [*columns.values(), *(v for v, _ in expect.values()),
                 *(c for _, c in expect.values()), *inputs.values()]
        if any(len(column) != count or set(column) - {"0", "1"} for column in every):
            raise WspIntegrityError("every column of a WSP program is 0/1 and as long as it")
        steps = []
        for n in range(count):
            cycle = WspCycle(
                wrstn=int(columns["wrstn"][n]),
                selectwir=int(columns["selectwir"][n]),
                shiftwr=int(columns["shiftwr"][n]),
                capturewr=int(columns["capturewr"][n]),
                updatewr=int(columns["updatewr"][n]),
                wsi=int(columns["wsi"][n]),
                pulse=columns["pulse"][n] == "1",
            )
            steps.append(
                Step(
                    cycle,
                    "",
                    tuple(int(inputs[name][n]) for name in drive),
                    tuple(
                        int(expect[p.name][0][n]) if expect[p.name][1][n] == "1" else None
                        for p in observe
                    ),
                )
            )
        named = []
        for name, start, stop in tests:
            if not 0 <= start < stop <= count:
                raise WspIntegrityError(f"test {name}: range [{start}, {stop}) is out of the program")
            named.append((name, start, stop))
        steps = [s._replace(test=_test_name(tests, n)) for n, s in enumerate(steps)]
        return cls(tuple(steps), tuple(named), observe, drive, roles)


def _test_name(tests: Sequence[tuple[str, int, int]], step: int) -> str:
    for name, start, stop in tests:
        if start <= step < stop:
            return name
    return ""


# --- the design ----------------------------------------------------------------------


def _members(design: Design) -> tuple[list[Wrapper1500], list[str]]:
    if isinstance(design, WspChainDesign):
        return list(design.wrappers), [f"u{n}." for n in range(len(design.wrappers))]
    return [design], [""]


def drive_ports(design: Design) -> tuple[str, ...]:
    """The data input ports the program drives: each input cell's label, ``u<k>.``-
    prefixed in a chain, in serial order."""
    wrappers, prefixes = _members(design)
    return tuple(
        prefix + cell.label
        for w, prefix in zip(wrappers, prefixes)
        for cell in w.cells
        if cell.side == INPUT
    )


def observation_points(
    design: Design, *, outputs: Optional[bool] = None, scan_outs: bool = True
) -> tuple[ObservePoint, ...]:
    """WSO, then (``outputs``; default: for one wrapper, not for a chain, whose block
    outputs aren't pins) each wrapped output port, then (``scan_outs``) each wrapper chain's
    scan output."""
    wrappers, prefixes = _members(design)
    if outputs is None:
        outputs = isinstance(design, Wrapper1500)
    points = [ObservePoint("wso", "wso", -1, 0)]
    for w_index, (w, prefix) in enumerate(zip(wrappers, prefixes)):
        if outputs:
            for c_index, cell in enumerate(w.cells):
                if cell.side == OUTPUT:
                    points.append(ObservePoint(prefix + cell.label, "output", w_index, c_index))
        if scan_outs:
            for k in range(len(w.chains)):
                points.append(ObservePoint(prefix + w.ports.so(k), "scan_out", w_index, k))
    return tuple(points)


def _roles(design: Design) -> dict[str, str]:
    if isinstance(design, WspChainDesign):
        first = design.wrappers[0].ports
        return {
            role: design.pins.get(role, (getattr(first, role), False))[0] for role in ROLES
        }
    return {role: getattr(design.ports, role) for role in ROLES}


def _reverse(value: int, bits: int) -> int:
    return int(format(value, f"0{bits}b")[::-1], 2)


def _ir_load(wrappers: Sequence[Wrapper1500], codes: Sequence[int]) -> ShiftIR:
    """The WIR scan that loads ``codes``, the far wrapper's first."""
    stream = offset = 0
    for w, code in reversed(list(zip(wrappers, codes))):
        stream |= _reverse(code, w.ir_bits) << offset
        offset += w.ir_bits
    return ShiftIR(offset, stream)


def _dr_length(wrappers: Sequence[Wrapper1500], codes: Sequence[int]) -> int:
    """The bits of the data register path ``codes`` select: a wrapper's boundary
    register in a serial instruction, else its bypass bit."""
    total = 0
    for w, code in zip(wrappers, codes):
        serial = w.instruction(w.instruction_of(code)).serial
        total += len(w.cells) if serial else 1
    return total


def _pattern(bits: int) -> int:
    """A sentinel of ``bits`` bits as a tdi value, bit 0 first."""
    return sum(_SENTINEL[k % len(_SENTINEL)] << k for k in range(bits))


# --- the recorder -----------------------------------------------------------------------


class _Recorder:
    """Plays the program's cycles on the model twice -- the core's outputs at 0 and at
    1 -- and keeps what both runs agree on as the expected value."""

    def __init__(self, design: Design, observe: Sequence[ObservePoint]) -> None:
        self.wrappers, self.prefixes = _members(design)
        self.observe = tuple(observe)
        self.drive = drive_ports(design)
        self.chains = [Wsp1500Chain(self.wrappers), Wsp1500Chain(self.wrappers)]
        self.ports = dict.fromkeys(self.drive, 0)
        self.steps: list[Step] = []
        self.tests: list[tuple[str, int, int]] = []
        self._start = 0
        self._name = ""

    def begin(self, name: str) -> None:
        self._name, self._start = name, len(self.steps)

    def end(self) -> None:
        self.tests.append((self._name, self._start, len(self.steps)))

    def set_ports(self, phase: int) -> None:
        """Drive the data input ports 1, 0, 1... (``phase`` 0) or 0, 1, 0... (1)."""
        for k, name in enumerate(self.drive):
            self.ports[name] = (k + phase) % 2

    def _cfis(self, live: int) -> list[tuple[Value, ...]]:
        found = []
        for w, prefix in zip(self.wrappers, self.prefixes):
            cells: list[Value] = []
            for cell in w.cells:
                cells.append(self.ports[prefix + cell.label] if cell.side == INPUT else live)
            found.append(tuple(cells))
        return found

    def _observed(self, chain: Wsp1500Chain, cycle: WspCycle, live: int) -> list[Value]:
        cfis = self._cfis(live)
        outputs = None
        values: list[Value] = []
        for point in self.observe:
            if point.kind == "wso":
                values.append(chain.wso(cycle, cfis))
            elif point.kind == "output":
                if outputs is None:
                    outputs = chain.outputs(cycle, cfis)
                values.append(outputs[point.wrapper][point.index])
            else:
                values.append(chain.sims[point.wrapper].scan_outs()[point.index])
        return values

    def cycle(self, cycle: WspCycle) -> None:
        runs = [self._observed(chain, cycle, live) for live, chain in enumerate(self.chains)]
        expect = tuple(a if a == b else None for a, b in zip(*runs))
        snapshot = tuple(self.ports[name] for name in self.drive)
        self.steps.append(Step(cycle, self._name, snapshot, expect))
        for live, chain in enumerate(self.chains):
            chain.step(cycle, self._cfis(live))

    def ops(self, ops: Sequence[object]) -> None:
        for row in to_wsp_rows(ops):
            self.cycle(row.cycle)

    def rows(self, cycles: Sequence[WspCycle]) -> None:
        for cycle in cycles:
            self.cycle(cycle)


# --- the program ------------------------------------------------------------------------


def build_wsp_integrity_program(
    design: Design,
    *,
    margin: int = 8,
    exhaustive_opcodes: bool = True,
    outputs: Optional[bool] = None,
    scan_outs: bool = True,
) -> WspIntegrityProgram:
    """The integrity program of ``design`` (see the module docstring). ``margin``: how
    many sentinel bits each scan carries past the register's length; ``exhaustive_opcodes``:
    test every code of the WIR, not only the instructions' own."""
    if margin < 2:
        raise WspIntegrityError("a margin under 2 shows no length")
    wrappers, prefixes = _members(design)
    count = len(wrappers)
    observe = observation_points(design, outputs=outputs, scan_outs=scan_outs)
    rec = _Recorder(design, observe)
    rec.set_ports(0)
    zeros = [0] * count

    rec.begin("reset")
    rec.ops([WspReset(), ShiftDR(count + margin, _pattern(count + margin))])
    rec.end()

    total = sum(w.ir_bits for w in wrappers)
    rec.begin("wir_length")
    # The sentinel first, then zeros: the update at the end loads WS_BYPASS everywhere.
    rec.ops([ShiftIR(total + margin, _pattern(margin))])
    rec.end()

    for k, (w, prefix) in enumerate(zip(wrappers, prefixes)):
        codes = (
            range(1 << w.ir_bits)
            if exhaustive_opcodes
            else sorted(i.code for i in w.instructions)
        )
        for code in codes:
            tag = f"code_{code:0{w.ir_bits}b}"
            rec.begin(tag if count == 1 else f"u{k}_{tag}")
            vector = list(zeros)
            vector[k] = code
            rec.ops([_ir_load(wrappers, vector)])
            length = _dr_length(wrappers, vector)
            serial = w.instruction(w.instruction_of(code)).serial
            for phase in (0, 1) if serial else (0,):
                rec.set_ports(phase)
                rec.ops([ShiftDR(length + margin, _pattern(length + margin))])
            rec.ops([Runtest(2)])
            rec.end()
        rec.set_ports(0)

    serial_codes = [
        (k, i.code) for k, w in enumerate(wrappers) for i in w.instructions if i.serial
    ]
    other = [
        (k, i.code)
        for k, w in enumerate(wrappers)
        for i in w.instructions
        if not i.serial and i.code != 0
    ]
    held = (serial_codes or other or [(0, 0)])[0]

    # An update while the WIR is deselected leaves the instruction in force.
    rec.begin("held_update")
    vector = list(zeros)
    vector[held[0]] = held[1]
    rec.ops([_ir_load(wrappers, vector)])
    rec.rows(
        [WspCycle(selectwir=1, shiftwr=1, wsi=0)] * total
        + [WspCycle(selectwir=0, updatewr=1)]
    )
    length = _dr_length(wrappers, vector)
    rec.ops([ShiftDR(length + margin, _pattern(length + margin))])
    rec.end()

    # CaptureWR with the WIR selected doesn't capture the boundary cells.
    rec.begin("capture_update")
    if serial_codes:
        k, code = serial_codes[0]
        vector = list(zeros)
        vector[k] = code
        rec.ops([_ir_load(wrappers, vector)])
        length = _dr_length(wrappers, vector)
        rec.set_ports(0)
        rec.ops([ShiftDR(length + margin, _pattern(length + margin))])
        rec.set_ports(1)
        rec.rows([WspCycle(selectwir=1, capturewr=1)])
        rec.rows([WspCycle(selectwir=0, shiftwr=1, wsi=_SENTINEL[n % 8]) for n in range(length + margin)])
    else:
        rec.ops([Runtest(2)])
    rec.end()

    return WspIntegrityProgram(
        tuple(rec.steps),
        tuple(rec.tests),
        observe,
        rec.drive,
        _roles(design),
        design,
    )


# --- playing a program -------------------------------------------------------------------


def replay_wsp_program(
    program: WspIntegrityProgram,
    design: Optional[Design] = None,
    *,
    live: int = 0,
) -> list[list[Value]]:
    """The values at each observation point of a program played on ``design`` (default:
    the program's own), the core's outputs at ``live``: one list per step."""
    design = design if design is not None else program.design
    if design is None:
        raise WspIntegrityError("a program read from JSON needs the design to replay on")
    rec = _Recorder(design, program.observe)
    if rec.drive != program.drive:
        raise WspIntegrityError("the design doesn't drive the ports the program does")
    chain = rec.chains[live]
    seen = []
    for step in program.steps:
        rec.ports = dict(zip(program.drive, step.ports))
        seen.append(rec._observed(chain, step.cycle, live))
        chain.step(step.cycle, rec._cfis(live))
    return seen


def check_wsp_integrity(
    program: WspIntegrityProgram, observed: Sequence[Sequence[Value]]
) -> list[tuple[int, str]]:
    """The (step, observation point) pairs where ``observed`` differs from what the
    program expects, at a care bit. An unknown (None) where a value is expected counts."""
    if len(observed) != len(program.steps):
        raise WspIntegrityError("one observation per step")
    found = []
    for n, (step, got) in enumerate(zip(program.steps, observed)):
        for k, point in enumerate(program.observe):
            if step.expect[k] is not None and got[k] != step.expect[k]:
                found.append((n, point.name))
    return found
