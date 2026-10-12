"""An IEEE 1500 wrapper and its wrapper serial port (WSP), as a model: the registers and
the cycle-by-cycle behaviour every other 1500 piece of warptap (the ICL it writes, the PDL
it retargets, the integrity program) is checked against.

The wrapper's registers:

- the instruction register (WIR): a shift stage, WSI into bit 0 and the last bit out to
  WSO, which shifts on the rising edge of WRCK while SelectWIR and ShiftWR are 1; and an
  update stage, the instruction in force, which takes the shift stage on the falling edge
  of WRCK while SelectWIR and UpdateWR are 1. WRSTN at 0 clears the update stage at once:
  WS_BYPASS, the functional mode. A code no instruction has acts as WS_BYPASS.
- the bypass register (WBY): one bit, WSI to WSO, which shifts while ShiftWR is 1 and
  captures 0 while CaptureWR is 1, the WIR not selected; cleared by WRSTN.
- the boundary register (WBR): the boundary cells, in a serial instruction WSI through
  them to WSO -- the wrapper chains one after another, each from its head.

An instruction is serial (WS_*: the WBR is the data register WSI and WSO reach) or
parallel (WP_*: the WBR works on the wrapper's own scan ports), and puts the boundary
cells in a mode, (INTEST, EXTEST) -- (0, 0) is functional. An input cell holds its flop in
INTEST and drives the core 0 in EXTEST; an output cell holds in EXTEST and drives its
port 0 in INTEST. In a serial instruction every cell also holds, its flop keeping its
value, unless the WIR is deselected and CaptureWR is 1 (the cells that observe in the
mode capture) or ShiftWR is 1 (the WBR shifts from WSI): a cell changes only on an event
of its own (the 1500 WBR rule), not through the WIR's own shifts and updates.

A step is one tester cycle: the inputs applied, WSO strobed, then WRCK pulses (rises,
then falls) if the cycle says so. Values are 0, 1 or None, unknown: every register
starts unknown but what WRSTN clears. :class:`Wsp1500Chain` runs wrappers whose WSI and
WSO are chained and whose other controls are shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, cast

INPUT = "input"
OUTPUT = "output"
SIDES = (INPUT, OUTPUT)

Value = Optional[int]  # 0, 1 or None (unknown)
_OWN = object()  # step(): WSI is the cycle's own


@dataclass(frozen=True)
class Instruction:
    """A wrapper instruction: its ``code`` in the WIR's update stage (bit 0 is the
    first bit shifted last), whether its data register is the WBR in series (WSI to
    WSO), and the ``mode`` (INTEST, EXTEST) it puts the cells in."""

    name: str
    code: int
    serial: bool
    mode: tuple[int, int]


DEFAULT_INSTRUCTIONS = (
    Instruction("WS_BYPASS", 0b000, False, (0, 0)),
    Instruction("WS_EXTEST", 0b001, True, (0, 1)),
    Instruction("WP_INTEST", 0b010, False, (1, 0)),
    Instruction("WP_EXTEST", 0b011, False, (0, 1)),
    Instruction("WS_INTEST", 0b100, True, (1, 0)),
)


@dataclass(frozen=True)
class WbrCell:
    """A boundary cell: the ``label`` of the port bit it serves and its ``side``."""

    label: str
    side: str

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ValueError(f"a boundary cell's side is one of {SIDES}, not {self.side!r}")


@dataclass(frozen=True)
class WspPorts:
    """The names of a wrapper's ports: the WSP's eight signals, the wrapper scan
    enable, and each wrapper chain's scan input and output (``si<k>``, ``so<k>`` by
    default)."""

    wsi: str = "wsi"
    wso: str = "wso"
    shiftwr: str = "shiftwr"
    capturewr: str = "capturewr"
    updatewr: str = "updatewr"
    selectwir: str = "selectwir"
    wrck: str = "wrck"
    wrstn: str = "wrstn"
    scan_enable: str = "scan_en"
    chain_in: tuple[str, ...] = ()
    chain_out: tuple[str, ...] = ()

    def si(self, chain: int) -> str:
        return self.chain_in[chain] if chain < len(self.chain_in) else f"si{chain}"

    def so(self, chain: int) -> str:
        return self.chain_out[chain] if chain < len(self.chain_out) else f"so{chain}"


@dataclass(frozen=True)
class Wrapper1500:
    """A wrapper: its boundary cells in ``chains`` (each a wrapper scan chain, head
    first), the WIR's width, its instructions and its port names."""

    name: str
    chains: tuple[tuple[WbrCell, ...], ...]
    ir_bits: int = 3
    instructions: tuple[Instruction, ...] = DEFAULT_INSTRUCTIONS
    ports: WspPorts = WspPorts()

    def __post_init__(self) -> None:
        codes = [i.code for i in self.instructions]
        names = [i.name for i in self.instructions]
        if len(set(codes)) != len(codes) or len(set(names)) != len(names):
            raise ValueError("two instructions with the same code or the same name")
        if any(not 0 <= code < 1 << self.ir_bits for code in codes):
            raise ValueError(f"an instruction's code doesn't fit {self.ir_bits} bits")
        if not any(i.code == 0 for i in self.instructions):
            raise ValueError("code 0 (WS_BYPASS, the reset value) needs an instruction")

    @property
    def cells(self) -> tuple[WbrCell, ...]:
        """The boundary register in serial order: the chains one after another."""
        return tuple(cell for chain in self.chains for cell in chain)

    @property
    def sides(self) -> tuple[str, ...]:
        return tuple(cell.side for cell in self.cells)

    def instruction(self, name: str) -> Instruction:
        for found in self.instructions:
            if found.name == name:
                return found
        raise KeyError(f"{self.name} has no instruction {name}")

    def instruction_of(self, code: int) -> str:
        """The instruction an update stage of ``code`` puts in force."""
        for found in self.instructions:
            if found.code == code:
                return found.name
        return self.instruction_of(0)


@dataclass(frozen=True)
class WspChainDesign:
    """Wrappers daisy-chained on a SoC: the SoC's ``top`` module, the ``wrappers`` from
    its WSI to its WSO, and ``pins`` -- each WSP role's SoC port and whether an inverter
    sits between it and the wrappers (:data:`warptap.icl_emit_1500.ROLES`)."""

    top: str
    wrappers: tuple[Wrapper1500, ...]
    pins: Mapping[str, tuple[str, bool]] = field(default_factory=dict)


@dataclass(frozen=True)
class WspCycle:
    """One tester cycle on the WSP: each signal's value (WRCK aside), whether WRCK
    pulses, and each boundary cell's functional input (its port bit, or the core's
    value), in serial order."""

    wrstn: int = 1
    selectwir: int = 0
    shiftwr: int = 0
    capturewr: int = 0
    updatewr: int = 0
    wsi: int = 0
    pulse: bool = True
    cfi: tuple[Value, ...] = ()


def program_cycles(wrapper: Wrapper1500, instruction: str | int) -> list[WspCycle]:
    """The cycles that put an instruction in force from any state -- its name, or
    any code: WRSTN pulsed, the code shifted into the WIR (last bit first), then
    updated, then the WSP idle (the WIR deselected)."""
    code = wrapper.instruction(instruction).code if isinstance(instruction, str) else instruction
    cycles = [WspCycle(wrstn=0, pulse=False)]
    for k in reversed(range(wrapper.ir_bits)):
        cycles.append(WspCycle(selectwir=1, shiftwr=1, wsi=(code >> k) & 1))
    cycles.append(WspCycle(selectwir=1, updatewr=1))
    cycles.append(WspCycle(pulse=False))
    return cycles


@dataclass
class Wsp1500Sim:
    """A wrapper's state, stepped a cycle at a time."""

    wrapper: Wrapper1500
    shift: list[Value] = field(default_factory=list)
    update: list[Value] = field(default_factory=list)
    wby: Value = None
    wbr: list[Value] = field(default_factory=list)

    def __post_init__(self) -> None:
        bits = self.wrapper.ir_bits
        if not self.shift:
            self.shift = [None] * bits
        if not self.update:
            self.update = [None] * bits
        if not self.wbr:
            self.wbr = [None] * len(self.wrapper.cells)

    @property
    def instruction(self) -> Optional[str]:
        """The instruction in force, or None while the update stage is unknown."""
        if any(bit is None for bit in self.update):
            return None
        code = sum(int(bit or 0) << k for k, bit in enumerate(self.update))
        return self.wrapper.instruction_of(code)

    def _info(self) -> Optional[Instruction]:
        name = self.instruction
        return None if name is None else self.wrapper.instruction(name)

    def _reset(self, cycle: WspCycle) -> None:
        if not cycle.wrstn:
            self.update = [0] * self.wrapper.ir_bits
            self.wby = 0

    def wso(self, cycle: WspCycle) -> Value:
        """WSO at the strobe of ``cycle`` (before its edge): the WIR's last bit when
        SelectWIR is 1, else the WBR's last cell in a serial instruction, else WBY."""
        self._reset(cycle)
        if cycle.selectwir:
            return self.shift[-1]
        info = self._info()
        if info is None:
            return None
        if info.serial:
            return self.wbr[-1] if self.wbr else None
        return self.wby

    def outputs(self, cycle: WspCycle) -> list[Value]:
        """Each boundary cell's functional output in ``cycle`` (to the core for an
        input cell, to the port for an output cell): 0 where the mode makes it safe,
        its flop where the mode holds it, else its functional input. A serial
        instruction's extra holds don't show: they hold only cells the mode keeps
        safe."""
        self._reset(cycle)
        info = self._info()
        if info is None:
            return [None] * len(self.wrapper.cells)
        found: list[Value] = []
        for k, side in enumerate(self.wrapper.sides):
            hold, safe = _hold_safe(side, info.mode)
            if safe:
                found.append(0)
            elif hold:
                found.append(self.wbr[k])
            else:
                found.append(cycle.cfi[k] if k < len(cycle.cfi) else None)
        return found

    def step(self, cycle: WspCycle, wsi: object = _OWN) -> Value:
        """Apply ``cycle``: WSO at its strobe, returned; then its WRCK pulse. ``wsi``
        (0, 1 or None) overrides the cycle's WSI -- a chain feeds the wrapper before's
        WSO."""
        in_bit: Value = cycle.wsi if wsi is _OWN else cast(Value, wsi)
        strobed = self.wso(cycle)
        if not cycle.pulse:
            return strobed
        info = self._info()
        # The rising edge: the WIR's shift stage, WBY and the WBR.
        shift = list(self.shift)
        if cycle.selectwir and cycle.shiftwr:
            shift = [in_bit, *shift[:-1]]
        wby = self.wby
        if not cycle.selectwir and cycle.shiftwr:
            wby = in_bit
        elif not cycle.selectwir and cycle.capturewr:
            wby = 0
        self.wbr = self._wbr_edge(info, cycle, in_bit)
        self.shift, self.wby = shift, wby
        # The falling edge: the WIR's update stage.
        if cycle.selectwir and cycle.updatewr:
            self.update = list(self.shift)
        self._reset(cycle)
        return strobed

    def _wbr_edge(
        self, info: Optional[Instruction], cycle: WspCycle, in_bit: Value
    ) -> list[Value]:
        if info is None:
            return [None] * len(self.wbr)
        if info.serial and cycle.shiftwr and not cycle.selectwir:
            return [in_bit, *self.wbr[:-1]]
        found: list[Value] = []
        for k, side in enumerate(self.wrapper.sides):
            hold, _ = _hold_safe(side, info.mode)
            if info.serial:
                capturing = cycle.capturewr and not cycle.selectwir
                hold = int(hold or not capturing)
            if hold:
                found.append(self.wbr[k])
            else:
                found.append(cycle.cfi[k] if k < len(cycle.cfi) else None)
        return found


def _hold_safe(side: str, mode: tuple[int, int]) -> tuple[int, int]:
    intest, extest = mode
    return (intest, extest) if side == INPUT else (extest, intest)


class Wsp1500Chain:
    """Wrappers whose WSI and WSO are chained -- WSI into the first, each WSO into the
    next WSI, the last one's WSO the chain's -- and whose other WSP controls are the
    same signals."""

    def __init__(self, wrappers: Sequence[Wrapper1500]) -> None:
        if not wrappers:
            raise ValueError("a chain needs a wrapper")
        self.sims = [Wsp1500Sim(wrapper) for wrapper in wrappers]

    def step(self, cycle: WspCycle) -> Value:
        """Apply ``cycle`` to every wrapper; the chain's WSO at its strobe. Every WSO
        is read before any edge, so each wrapper shifts in what its neighbour showed."""
        strobes = [sim.wso(cycle) for sim in self.sims]
        feeds: list[Value] = [cycle.wsi, *strobes[:-1]]
        for sim, feed in zip(self.sims, feeds):
            sim.step(cycle, feed)
        return strobes[-1]

    def run(self, cycles: Sequence[WspCycle]) -> list[Value]:
        return [self.step(cycle) for cycle in cycles]
