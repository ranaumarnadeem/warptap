"""Python behavioral model of a TAP core: FSM + instruction register + BYPASS/IDCODE data
registers (implementation_plan.md §7 Stage 2).

This is the reference side of every cross-simulation test against rtl/tap_core.v, and the
extension point Stage 9's PDL functional verification eventually drives. SAMPLE_PRELOAD and
EXTEST decode correctly and drive capture/shift/update strobes correctly (real hardware
computes those from *state* alone, not from which instruction is active), but have no data
register registered by default: their data register is the boundary-scan register, which
doesn't exist until Stage 3. Shifting through them before Stage 3 registers a real one raises
TapModelError loudly rather than silently validating nothing against a fake stand-in.

A TAP built with a dedicated IJTAG_ACCESS opcode (``ijtag_access_opcode``) decodes that opcode
to IJTAG_ACCESS, the instruction that selects the IJTAG network, and EXTEST's and
SAMPLE/PRELOAD's to BYPASS.
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

# The suggested opcode for a dedicated IJTAG_ACCESS instruction (``ijtag_access_opcode``,
# off by default). The only 4-bit value at least two bits from each of EXTEST, SAMPLE/PRELOAD,
# IDCODE and BYPASS, so one corrupted IR bit can't turn a board instruction into it.
OPCODE_IJTAG_ACCESS = 0b1100

# The pattern Capture-IR loads is deliberately equal to OPCODE_IDCODE: if a shift sequence
# goes Capture-IR -> Update-IR without ever visiting Shift-IR, the captured pattern becomes
# the new instruction, and this way that edge case lands on the same safe default the
# reset rule already requires instead of on something hazardous like EXTEST.
CAPTURE_IR_PATTERN = OPCODE_IDCODE

# A placeholder, not an ID warptap registered with anyone. Decoded per IEEE 1149.1: version
# 0x1, part 0xA5A5, manufacturer bits [11:1] = 0x001 (JEP106 bank 1, code 0x01), LSB 1. That
# manufacturer field is NOT an unused code: it lands in an assigned JEP106 slot (not checked
# here against the current JEP106 table), so this value can collide with a real manufacturer's
# ID. It is only the default: insert_test_access/insert_sib_network(idcode_value=...) bake a
# design's own value into its tap_core, and to_bsdl/TapModel/TapConfig take the same keyword.
# Replacing the default itself means changing rtl/tap_core.v's IDCODE_VALUE default with it.
IDCODE_VALUE = 0x1A5A5003


def idcode_value_error(value: object) -> str | None:
    """Why ``value`` can't be a TAP's IDCODE, or ``None`` if it can. IEEE 1149.1's
    device-identification register is 32 bits with bit 0 fixed at 1. Checked here, in Python,
    because nothing downstream would catch it: Yosys silently truncates a value wider than
    ``rtl/tap_core.v``'s ``[31:0]`` parameter, and the RTL doesn't check bit 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return f"IDCODE value must be an int, got {value!r}"
    if not 0 <= value < 1 << 32:
        return f"IDCODE value {value:#x} is not a 32-bit value"
    if value & 1 != 1:
        return f"IDCODE value {value:#010x} has bit 0 clear; IEEE 1149.1 requires bit 0 = 1"
    return None


def bypass_opcode(ir_width: int) -> int:
    """BYPASS must decode to all-1s for whatever IR width is configured: computed, not
    hardcoded, since the mandate is about the pattern, not a fixed bit width."""
    return (1 << ir_width) - 1


class Instruction(enum.Enum):
    EXTEST = "EXTEST"
    SAMPLE_PRELOAD = "SAMPLE_PRELOAD"
    IDCODE = "IDCODE"
    BYPASS = "BYPASS"
    IJTAG_ACCESS = "IJTAG_ACCESS"


# The instruction an external tester loads to reach the IJTAG (SIB/TDR) network. rtl/tap_core.v's
# TDO mux presents ``external_dr_tdo`` -- where the network's tail plugs in -- for any latched
# instruction that is neither IDCODE nor BYPASS, and Update-IR normalizes every reserved opcode
# to BYPASS, so both EXTEST and SAMPLE_PRELOAD put the network's tail on TDO; but sib_insert
# decodes EXTEST into every top-level SIB's select, so the network captures, shifts and updates
# only under EXTEST (under SAMPLE_PRELOAD it holds still). EXTEST is the one named to consumers
# (the ICL AccessLink, the BSDL); in real 1149.1 EXTEST means "drive the boundary register",
# which is only true of a design with a boundary-scan register and no SIB network (the two never
# coexist, see sib_insert.insert_sib_network). That is the default; a TAP inserted with
# ``ijtag_access_opcode`` selects the network under IJTAG_ACCESS instead
# (:func:`network_access_instruction`), and a board-level EXTEST then can't reach it.
NETWORK_ACCESS_INSTRUCTION = Instruction.EXTEST


def network_access_instruction(ijtag_access_opcode: int | None) -> Instruction:
    """The instruction that selects the IJTAG network: EXTEST, or IJTAG_ACCESS for a TAP built
    with an ``ijtag_access_opcode``."""
    if ijtag_access_opcode is None:
        return NETWORK_ACCESS_INSTRUCTION
    return Instruction.IJTAG_ACCESS


def ijtag_access_opcode_error(opcode: object, ir_width: int = DEFAULT_IR_WIDTH) -> str | None:
    """Why ``opcode`` can't be a TAP's IJTAG_ACCESS opcode, or ``None`` if it can: it must fit
    the instruction register and be none of EXTEST, SAMPLE/PRELOAD, IDCODE or BYPASS. IDCODE's
    is also the Capture-IR pattern, which an Update-IR straight after Capture-IR loads."""
    if isinstance(opcode, bool) or not isinstance(opcode, int):
        return f"IJTAG_ACCESS opcode must be an int, got {opcode!r}"
    if not 0 <= opcode < 1 << ir_width:
        return f"IJTAG_ACCESS opcode {opcode:#x} does not fit a {ir_width}-bit instruction register"
    taken = {
        OPCODE_EXTEST: "EXTEST's",
        OPCODE_SAMPLE_PRELOAD: "SAMPLE/PRELOAD's",
        OPCODE_IDCODE: "IDCODE's (and the Capture-IR pattern)",
        bypass_opcode(ir_width): "BYPASS's",
    }
    if opcode in taken:
        return f"IJTAG_ACCESS opcode {opcode:0{ir_width}b} is already {taken[opcode]}"
    return None


def decode_instruction(
    opcode: int,
    ir_width: int,
    *,
    has_idcode: bool = True,
    ijtag_access_opcode: int | None = None,
) -> Instruction:
    """Map a shifted-in IR opcode to an Instruction. Unimplemented/reserved opcodes (and
    the IDCODE opcode when IDCODE isn't configured) decode to BYPASS, matching common
    real-TAP practice and keeping the TDO mux's default case safe. With
    ``ijtag_access_opcode``, that opcode decodes to IJTAG_ACCESS, and EXTEST's and
    SAMPLE/PRELOAD's decode to BYPASS: there is no boundary register for them to select."""
    if ijtag_access_opcode is not None:
        if opcode == ijtag_access_opcode:
            return Instruction.IJTAG_ACCESS
    elif opcode == OPCODE_EXTEST:
        return Instruction.EXTEST
    elif opcode == OPCODE_SAMPLE_PRELOAD:
        return Instruction.SAMPLE_PRELOAD
    if has_idcode and opcode == OPCODE_IDCODE:
        return Instruction.IDCODE
    if opcode == bypass_opcode(ir_width):
        return Instruction.BYPASS
    return Instruction.BYPASS


class TapModelError(WarptapError):
    """Raised when TapModel is asked to do something its currently-registered data
    registers don't support: e.g. shifting through SAMPLE_PRELOAD/EXTEST before Stage 3's
    boundary-scan register exists (implementation_plan.md §7)."""


class DataRegister(Protocol):
    """The interface TapModel expects from anything registered via
    `register_data_register`: BYPASS/IDCODE below, and Stage 3+'s real boundary-scan
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

    def reset(self) -> None:
        """rtl/tap_core.v's ``bypass_bit`` clears on ``trst_n``."""
        self._bit = 0


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

    def reset(self) -> None:
        """rtl/tap_core.v's ``idcode_shift`` clears on ``trst_n``."""
        self._shreg = 0


class TapModel:
    """Cycle-stepped behavioral model: call `tick(tms, tdi)` once per TCK rising edge,
    exactly as a real TAP would be clocked, and get back the TDO value for that cycle."""

    def __init__(
        self,
        *,
        ir_width: int = DEFAULT_IR_WIDTH,
        has_idcode: bool = True,
        idcode_value: int = IDCODE_VALUE,
        ijtag_access_opcode: int | None = None,
    ):
        if has_idcode and (problem := idcode_value_error(idcode_value)):
            raise TapModelError(problem)
        if ijtag_access_opcode is not None and (
            problem := ijtag_access_opcode_error(ijtag_access_opcode, ir_width)
        ):
            raise TapModelError(problem)
        self.ir_width = ir_width
        self.has_idcode = has_idcode
        self.ijtag_access_opcode = ijtag_access_opcode
        # What the network's data register is registered under.
        self.network_instruction = network_access_instruction(ijtag_access_opcode)
        bypass = BypassRegister()
        self._builtin_registers: list[BypassRegister | IdcodeRegister] = [bypass]
        self._data_registers: dict[Instruction, DataRegister] = {Instruction.BYPASS: bypass}
        if has_idcode:
            idcode = IdcodeRegister(idcode_value)
            self._builtin_registers.append(idcode)
            self._data_registers[Instruction.IDCODE] = idcode
        self.state = TapState.TEST_LOGIC_RESET
        self._ir_shift = 0
        self.instruction = self._reset_instruction()

    def register_data_register(self, instruction: Instruction, dr: DataRegister) -> None:
        """Plug in a real data register for `instruction`: the extension point Stage
        3 (boundary-scan register, for EXTEST/SAMPLE_PRELOAD) and later stages use,
        without touching any FSM/IR logic in this class."""
        self._data_registers[instruction] = dr

    def reset(self) -> None:
        """Async reset: TEST_LOGIC_RESET, the instruction defaults to IDCODE if configured,
        else BYPASS (implementation_plan.md §3.1's default-on-reset rule), and the built-in
        BYPASS/IDCODE registers clear, as rtl/tap_core.v's do on ``trst_n``. A register
        plugged in with register_data_register() is not reset here; the caller resets it."""
        self.state = TapState.TEST_LOGIC_RESET
        self._ir_shift = 0
        self.instruction = self._reset_instruction()
        for dr in self._builtin_registers:
            dr.reset()

    def _reset_instruction(self) -> Instruction:
        return Instruction.IDCODE if self.has_idcode else Instruction.BYPASS

    def instruction_opcode(self) -> int:
        """Canonical opcode for the currently-active instruction, normalizing
        reserved/unimplemented opcodes to BYPASS's own opcode: matches
        rtl/tap_core.v's `current_instruction` register value exactly (both
        normalize at Update-IR time the same way), so
        tests/test_tap_fsm_cross_sim.py can diff the two with no translation."""
        opcodes = {
            Instruction.EXTEST: OPCODE_EXTEST,
            Instruction.SAMPLE_PRELOAD: OPCODE_SAMPLE_PRELOAD,
            Instruction.IDCODE: OPCODE_IDCODE,
            Instruction.BYPASS: bypass_opcode(self.ir_width),
        }
        if self.ijtag_access_opcode is not None:
            opcodes[Instruction.IJTAG_ACCESS] = self.ijtag_access_opcode
        return opcodes[self.instruction]

    def _active_dr(self) -> DataRegister:
        dr = self._data_registers.get(self.instruction)
        if dr is None and self.instruction is Instruction.IJTAG_ACCESS:
            raise TapModelError(
                "no data register registered for IJTAG_ACCESS -- register the network's "
                "(a SibNetworkRegister) with register_data_register() first"
            )
        if dr is None:
            raise TapModelError(
                f"no data register registered for {self.instruction.value}: its data "
                "register is the boundary-scan register, which doesn't exist until "
                "Stage 3 (implementation_plan.md §7). Call register_data_register() "
                "first, or drive an instruction with a built-in register (BYPASS/IDCODE)."
            )
        return dr

    def tick(self, tms: int, tdi: int = 0) -> int:
        """Step one TCK rising edge. `tms`/`tdi` are the values sampled on this edge;
        returns the TDO value for this cycle (only meaningful while resident in
        Shift-IR/Shift-DR: other states return 0, since real TAPs don't guarantee a
        protocol-significant TDO value outside a shift state either)."""
        old_state = self.state
        tdo = 0

        if old_state is TapState.TEST_LOGIC_RESET:
            # IEEE 1149.1: Test-Logic-Reset reloads the reset instruction, so five TMS=1
            # cycles deselect any test-mode instruction without a TRST pin.
            self.instruction = self._reset_instruction()
        elif old_state is TapState.CAPTURE_IR:
            self._ir_shift = CAPTURE_IR_PATTERN
        elif old_state is TapState.SHIFT_IR:
            tdo = self._ir_shift & 1
            self._ir_shift = (self._ir_shift >> 1) | ((tdi & 1) << (self.ir_width - 1))
        elif old_state is TapState.UPDATE_IR:
            self.instruction = decode_instruction(
                self._ir_shift,
                self.ir_width,
                has_idcode=self.has_idcode,
                ijtag_access_opcode=self.ijtag_access_opcode,
            )
        elif old_state is TapState.CAPTURE_DR:
            self._active_dr().capture()
        elif old_state is TapState.SHIFT_DR:
            tdo = self._active_dr().shift(tdi)
        elif old_state is TapState.UPDATE_DR:
            self._active_dr().update()

        self.state = next_state(old_state, tms)
        return tdo
