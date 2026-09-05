"""Python behavioral model of a TAP core: FSM + instruction register + BYPASS/IDCODE data
registers (implementation_plan.md §7 Stage 2).

This is the reference side of every cross-simulation test against rtl/tap_core.v, and the
extension point Stage 9's PDL functional verification eventually drives. SAMPLE_PRELOAD and
EXTEST decode correctly and drive capture/shift/update strobes correctly (real hardware
computes those from *state* alone, not from which instruction is active), but have no data
register registered by default — their data register is the boundary-scan register, which
doesn't exist until Stage 3. Shifting through them before Stage 3 registers a real one raises
TapModelError loudly rather than silently validating nothing against a fake stand-in.
"""

from __future__ import annotations

import enum
from typing import Protocol

from warptap.errors import WarptapError
from warptap.tap_fsm import TapState, next_state

DEFAULT_IR_WIDTH = 4

# Only BYPASS's value (all-1s) and the Capture-IR pattern's LSBs ('01') are IEEE 1149.1
# mandates; EXTEST/SAMPLE_PRELOAD/IDCODE's specific opcode values are a warptap design
# choice, stated explicitly rather than left implicit (implementation_plan.md §7 Stage 2).
OPCODE_EXTEST = 0b0000
OPCODE_SAMPLE_PRELOAD = 0b0010
OPCODE_IDCODE = 0b0001

# The pattern Capture-IR loads is deliberately equal to OPCODE_IDCODE: if a shift sequence
# goes Capture-IR -> Update-IR without ever visiting Shift-IR, the captured pattern becomes
# the new instruction, and this way that edge case lands on the same safe default the
# reset rule already requires instead of on something hazardous like EXTEST.
CAPTURE_IR_PATTERN = OPCODE_IDCODE

# Explicitly labeled placeholder, not a real registered JEDEC manufacturer ID.
IDCODE_VALUE = 0x1A5A5003


def bypass_opcode(ir_width: int) -> int:
    """BYPASS must decode to all-1s for whatever IR width is configured — computed, not
    hardcoded, since the mandate is about the pattern, not a fixed bit width."""
    return (1 << ir_width) - 1


class Instruction(enum.Enum):
    EXTEST = "EXTEST"
    SAMPLE_PRELOAD = "SAMPLE_PRELOAD"
    IDCODE = "IDCODE"
    BYPASS = "BYPASS"


def decode_instruction(
    opcode: int, ir_width: int, *, has_idcode: bool = True
) -> Instruction:
    """Map a shifted-in IR opcode to an Instruction. Unimplemented/reserved opcodes (and
    the IDCODE opcode when IDCODE isn't configured) decode to BYPASS, matching common
    real-TAP practice and keeping the TDO mux's default case safe."""
    if opcode == OPCODE_EXTEST:
        return Instruction.EXTEST
    if opcode == OPCODE_SAMPLE_PRELOAD:
        return Instruction.SAMPLE_PRELOAD
    if has_idcode and opcode == OPCODE_IDCODE:
        return Instruction.IDCODE
    if opcode == bypass_opcode(ir_width):
        return Instruction.BYPASS
    return Instruction.BYPASS


class TapModelError(WarptapError):
    """Raised when TapModel is asked to do something its currently-registered data
    registers don't support — e.g. shifting through SAMPLE_PRELOAD/EXTEST before Stage 3's
    boundary-scan register exists (implementation_plan.md §7)."""


class DataRegister(Protocol):
    """The interface TapModel expects from anything registered via
    `register_data_register` — BYPASS/IDCODE below, and Stage 3+'s real boundary-scan
    register later."""

    def capture(self) -> None: ...

    def shift(self, tdi: int) -> int: ...

    def update(self) -> None: ...


class BypassRegister:
    """The mandatory 1-bit BYPASS data register. Captures a fixed 0 (IEEE 1149.1
    mandate); shifts TDI straight through to TDO with one cycle of latency."""

    def __init__(self) -> None:
        self._bit = 0

    def capture(self) -> None:
        self._bit = 0

    def shift(self, tdi: int) -> int:
        tdo = self._bit
        self._bit = tdi & 1
        return tdo

    def update(self) -> None:
        pass  # BYPASS has no parallel output to latch


class IdcodeRegister:
    """The optional 32-bit IDCODE data register. Captures a fixed configured value on
    Capture-DR; shifts it out LSB-first, standard right-shift-with-serial-input
    convention (matches rtl/tap_core.v's `shreg <= {tdi, shreg[W-1:1]}` shape exactly, so
    a cross-simulation trace comparison needs no bit-order translation)."""

    def __init__(self, value: int = IDCODE_VALUE, width: int = 32):
        self.width = width
        self._value = value
        self._shreg = 0

    def capture(self) -> None:
        self._shreg = self._value

    def shift(self, tdi: int) -> int:
        tdo = self._shreg & 1
        self._shreg = (self._shreg >> 1) | ((tdi & 1) << (self.width - 1))
        return tdo

    def update(self) -> None:
        pass  # IDCODE has no parallel output to latch


class TapModel:
    """Cycle-stepped behavioral model: call `tick(tms, tdi)` once per TCK rising edge,
    exactly as a real TAP would be clocked, and get back the TDO value for that cycle."""

    def __init__(
        self,
        *,
        ir_width: int = DEFAULT_IR_WIDTH,
        has_idcode: bool = True,
        idcode_value: int = IDCODE_VALUE,
    ):
        self.ir_width = ir_width
        self.has_idcode = has_idcode
        self._data_registers: dict[Instruction, DataRegister] = {
            Instruction.BYPASS: BypassRegister(),
        }
        if has_idcode:
            self._data_registers[Instruction.IDCODE] = IdcodeRegister(idcode_value)
        self.state = TapState.TEST_LOGIC_RESET
        self._ir_shift = 0
        self.instruction = Instruction.IDCODE if has_idcode else Instruction.BYPASS

    def register_data_register(self, instruction: Instruction, dr: DataRegister) -> None:
        """Plug in a real data register for `instruction` — the extension point Stage
        3 (boundary-scan register, for EXTEST/SAMPLE_PRELOAD) and later stages use,
        without touching any FSM/IR logic in this class."""
        self._data_registers[instruction] = dr

    def reset(self) -> None:
        """Async reset: TEST_LOGIC_RESET, and the instruction defaults to IDCODE if
        configured, else BYPASS (implementation_plan.md §3.1's default-on-reset rule)."""
        self.state = TapState.TEST_LOGIC_RESET
        self._ir_shift = 0
        self.instruction = Instruction.IDCODE if self.has_idcode else Instruction.BYPASS

    def instruction_opcode(self) -> int:
        """Canonical opcode for the currently-active instruction, normalizing
        reserved/unimplemented opcodes to BYPASS's own opcode — matches
        rtl/tap_core.v's `current_instruction` register value exactly (both
        normalize at Update-IR time the same way), so
        tests/test_tap_fsm_cross_sim.py can diff the two with no translation."""
        return {
            Instruction.EXTEST: OPCODE_EXTEST,
            Instruction.SAMPLE_PRELOAD: OPCODE_SAMPLE_PRELOAD,
            Instruction.IDCODE: OPCODE_IDCODE,
            Instruction.BYPASS: bypass_opcode(self.ir_width),
        }[self.instruction]

    def _active_dr(self) -> DataRegister:
        dr = self._data_registers.get(self.instruction)
        if dr is None:
            raise TapModelError(
                f"no data register registered for {self.instruction.value} — its data "
                "register is the boundary-scan register, which doesn't exist until "
                "Stage 3 (implementation_plan.md §7). Call register_data_register() "
                "first, or drive an instruction with a built-in register (BYPASS/IDCODE)."
            )
        return dr

    def tick(self, tms: int, tdi: int = 0) -> int:
        """Step one TCK rising edge. `tms`/`tdi` are the values sampled on this edge;
        returns the TDO value for this cycle (only meaningful while resident in
        Shift-IR/Shift-DR — other states return 0, since real TAPs don't guarantee a
        protocol-significant TDO value outside a shift state either)."""
        old_state = self.state
        tdo = 0

        if old_state is TapState.CAPTURE_IR:
            self._ir_shift = CAPTURE_IR_PATTERN
        elif old_state is TapState.SHIFT_IR:
            tdo = self._ir_shift & 1
            self._ir_shift = (self._ir_shift >> 1) | ((tdi & 1) << (self.ir_width - 1))
        elif old_state is TapState.UPDATE_IR:
            self.instruction = decode_instruction(
                self._ir_shift, self.ir_width, has_idcode=self.has_idcode
            )
        elif old_state is TapState.CAPTURE_DR:
            self._active_dr().capture()
        elif old_state is TapState.SHIFT_DR:
            tdo = self._active_dr().shift(tdi)
        elif old_state is TapState.UPDATE_DR:
            self._active_dr().update()

        self.state = next_state(old_state, tms)
        return tdo
