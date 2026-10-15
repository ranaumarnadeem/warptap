"""PDL (IEEE 1687) on an IEEE 1500 wrapper, or a chain of wrappers on a SoC, retargeted
onto the wrapper serial port.

``iReset`` clears the wrappers (WRSTN): every WIR is WS_BYPASS. ``iTarget`` sets the
wrapper ``iWrite``/``iRead`` address -- ``u0``, ``u1`` of a chain (the instances of
:func:`warptap.icl_emit_1500.to_icl_1500_chain`), or the wrapper's own name -- and a
register may be written ``u0.WIR`` to name its wrapper too. The registers:

- ``WIR``, written by an instruction name (``WS_EXTEST``) or its ICL value (the code with
  its bits reversed). One ``iApply`` scans every WIR of the chain at once, the far
  wrapper's bits first; a wrapper not written keeps its committed instruction, which must
  be known (an ``iReset``, or a write). A WIR is not read: its shift stage has no capture.
- ``WBY``, in an instruction whose data register is the bypass bit; and ``WBR0``,
  ``WBR1``... in a serial instruction, one per wrapper chain (the ICL registers), each by
  its value: the head cell the MSB, the tail the LSB. One data scan runs through whatever
  each wrapper's instruction selects, the far wrapper's first; a register not written
  shifts in 0, one not read isn't checked.

``iApply`` turns the pending writes and reads into ``tap_ir`` scan operations --
``ShiftIR`` for the WIRs, then ``ShiftDR`` -- appended to ``program`` (:mod:`warptap.wsp_play`
lowers them to cycles); ``iRunLoop`` appends idle cycles where it is called. ``history``
keeps the statements for :func:`to_pdl_1500`, and :func:`import_pdl_1500` reads them back.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional, Sequence, Union

from warptap.icl_emit_1500 import icl_literal
from warptap.tap_ir import Runtest, ShiftDR, ShiftIR
from warptap.wsp1500_model import Wrapper1500, WspChainDesign
from warptap.wsp_ir import WspReset


class Pdl1500Error(ValueError):
    """PDL a wrapper can't take, or can't print."""


class ResetStmt(NamedTuple):
    """``iReset;``"""


class TargetStmt(NamedTuple):
    """``iTarget <wrapper>;``"""

    path: str


class WriteStmt(NamedTuple):
    """``iWrite <register> <value>;`` -- an instruction name, or a number."""

    register: str
    value: Union[int, str]


class ReadStmt(NamedTuple):
    """``iRead <register> <expected>;``"""

    register: str
    expected: int


class RunLoopStmt(NamedTuple):
    """``iRunLoop <count> -sck <wrck>;``"""

    count: int


class ApplyStmt(NamedTuple):
    """``iApply;``"""


Statement = Union[ResetStmt, TargetStmt, WriteStmt, ReadStmt, RunLoopStmt, ApplyStmt]
Op = Union[WspReset, ShiftIR, ShiftDR, Runtest]


def _reverse(value: int, bits: int) -> int:
    return int(format(value, f"0{bits}b")[::-1], 2)


class Wsp1500Interpreter:
    """See the module docstring. ``design``: one wrapper, or a chain of them."""

    def __init__(self, design: Union[Wrapper1500, WspChainDesign]) -> None:
        if isinstance(design, WspChainDesign):
            self.names = [f"u{n}" for n in range(len(design.wrappers))]
            self.wrappers = list(design.wrappers)
        else:
            self.names = [""]
            self.wrappers = [design]
        self._scope: Optional[int] = None
        self._codes: list[Optional[int]] = [None] * len(self.wrappers)
        self._ir_writes: dict[int, int] = {}
        self._dr_writes: dict[tuple[int, str], int] = {}
        self._dr_reads: dict[tuple[int, str], int] = {}
        self.program: list[Op] = []
        self.history: list[Statement] = []

    # --- naming ----------------------------------------------------------------

    def _wrapper_index(self, path: str) -> int:
        for index, name in enumerate(self.names):
            if path == name or path == self.wrappers[index].name:
                if name == "" or path == name or len(self.wrappers) == 1:
                    return index
        raise Pdl1500Error(f"no wrapper {path!r} (the wrappers are {self.names})")

    def _resolve(self, register: str) -> tuple[int, str]:
        """The wrapper index and the bare register name ``register`` names."""
        if "." in register:
            path, _, bare = register.rpartition(".")
            return self._wrapper_index(path), bare
        if self._scope is not None:
            return self._scope, register
        if len(self.wrappers) == 1:
            return 0, register
        raise Pdl1500Error(f"{register}: iTarget a wrapper first, or name it (u0.{register})")

    def _check_register(self, index: int, register: str) -> None:
        wrapper = self.wrappers[index]
        known = {"WIR", "WBY", *(f"WBR{k}" for k in range(len(wrapper.chains)))}
        if register not in known:
            raise Pdl1500Error(f"{wrapper.name} has no register {register!r}")

    def _width(self, index: int, register: str) -> int:
        wrapper = self.wrappers[index]
        if register == "WIR":
            return wrapper.ir_bits
        if register == "WBY":
            return 1
        return len(wrapper.chains[int(register[3:])])

    # --- PDL ---------------------------------------------------------------------

    def iReset(self) -> None:
        """Clear the wrappers: WRSTN for a cycle, every WIR WS_BYPASS."""
        self.program.append(WspReset())
        self._codes = [0] * len(self.wrappers)
        self._ir_writes.clear()
        self._dr_writes.clear()
        self._dr_reads.clear()
        self.history.append(ResetStmt())

    def iTarget(self, path: str) -> None:
        self._scope = self._wrapper_index(path)
        self.history.append(TargetStmt(path))

    def iWrite(self, register: str, value: Union[int, str]) -> None:
        index, bare = self._resolve(register)
        self._check_register(index, bare)
        wrapper = self.wrappers[index]
        if isinstance(value, str):
            if bare != "WIR":
                raise Pdl1500Error(f"{bare} takes a number, not {value!r}")
            try:
                code = wrapper.instruction(value).code
            except KeyError as exc:
                raise Pdl1500Error(f"{wrapper.name} has no instruction {value}") from exc
            number = int(icl_literal(code, wrapper.ir_bits).split("'b")[1], 2)
        else:
            number = value
        width = self._width(index, bare)
        if not 0 <= number < 1 << width:
            raise Pdl1500Error(f"{bare} is {width} bits: {number} doesn't fit")
        if bare == "WIR":
            self._ir_writes[index] = _reverse(number, width)
        else:
            self._dr_writes[(index, bare)] = number
        self.history.append(WriteStmt(register, value))

    def iRead(self, register: str, expected: int) -> None:
        index, bare = self._resolve(register)
        self._check_register(index, bare)
        if bare == "WIR":
            raise Pdl1500Error("the WIR is not read: its shift stage has no capture")
        width = self._width(index, bare)
        if not 0 <= expected < 1 << width:
            raise Pdl1500Error(f"{bare} is {width} bits: {expected} doesn't fit")
        self._dr_reads[(index, bare)] = expected
        self.history.append(ReadStmt(register, expected))

    def iRunLoop(self, count: int) -> None:
        """``count`` idle cycles, where this is called."""
        self.program.append(Runtest(count))
        self.history.append(RunLoopStmt(count))

    def iApply(self) -> list[Op]:
        """Scan the pending writes and reads; the operations added to ``program``."""
        added: list[Op] = []
        if self._ir_writes:
            added.append(self._apply_wir())
        if self._dr_writes or self._dr_reads:
            added.append(self._apply_dr())
        self.program += added
        self.history.append(ApplyStmt())
        return added

    def _apply_wir(self) -> ShiftIR:
        stream, offset = 0, 0
        values = []
        for index, wrapper in enumerate(self.wrappers):
            if index in self._ir_writes:
                code = self._ir_writes[index]
            else:
                known = self._codes[index]
                if known is None:
                    raise Pdl1500Error(
                        f"the WIR of {self.names[index] or wrapper.name} is unknown: "
                        "iReset first, or write it"
                    )
                code = known
            values.append(code)
        # The far wrapper's value goes first: it ends nearest WSO.
        for index in reversed(range(len(self.wrappers))):
            bits = self.wrappers[index].ir_bits
            stream |= _reverse(values[index], bits) << offset
            offset += bits
        self._codes = list(values)
        self._ir_writes.clear()
        return ShiftIR(offset, stream)

    def _apply_dr(self) -> ShiftDR:
        path: list[tuple[int, str]] = []
        for index, wrapper in enumerate(self.wrappers):
            code = self._codes[index]
            if code is None:
                raise Pdl1500Error(
                    f"the WIR of {self.names[index] or wrapper.name} is unknown: iReset first"
                )
            instruction = wrapper.instruction(wrapper.instruction_of(code))
            if instruction.serial:
                path += [(index, f"WBR{k}") for k in range(len(wrapper.chains))]
            else:
                path.append((index, "WBY"))
        for key in [*self._dr_writes, *self._dr_reads]:
            if key not in path:
                index, register = key
                wrapper = self.wrappers[index]
                name = wrapper.instruction_of(self._codes[index] or 0)
                raise Pdl1500Error(
                    f"{register} of {wrapper.name} isn't selected by {name}"
                )
        stream = tdo = mask = 0
        offset = 0
        for key in reversed(path):
            width = self._width(*key)
            stream |= self._dr_writes.get(key, 0) << offset
            if key in self._dr_reads:
                tdo |= self._dr_reads[key] << offset
                mask |= ((1 << width) - 1) << offset
            offset += width
        reads = bool(self._dr_reads)
        self._dr_writes.clear()
        self._dr_reads.clear()
        return ShiftDR(offset, stream, tdo if reads else None, mask if reads else None)


# --- text ----------------------------------------------------------------------------


def to_pdl_1500(
    history: Sequence[Statement], design: Union[Wrapper1500, WspChainDesign]
) -> str:
    """``history`` as PDL text, one statement a line, numbers as sized binary literals."""
    probe = Wsp1500Interpreter(design)
    wrck = probe.wrappers[0].ports.wrck
    scope: Optional[int] = None
    lines = []
    for stmt in history:
        if isinstance(stmt, ResetStmt):
            lines.append("iReset;")
        elif isinstance(stmt, TargetStmt):
            scope = probe._wrapper_index(stmt.path)
            lines.append(f"iTarget {stmt.path};")
        elif isinstance(stmt, (WriteStmt, ReadStmt)):
            probe._scope = scope
            index, bare = probe._resolve(stmt.register)
            value = stmt.value if isinstance(stmt, WriteStmt) else stmt.expected
            if isinstance(value, str):
                token = value
            else:
                width = probe._width(index, bare)
                token = f"{width}'b{value:0{width}b}"
            word = "iWrite" if isinstance(stmt, WriteStmt) else "iRead"
            lines.append(f"{word} {stmt.register} {token};")
        elif isinstance(stmt, RunLoopStmt):
            lines.append(f"iRunLoop {stmt.count} -sck {wrck};")
        elif isinstance(stmt, ApplyStmt):
            lines.append("iApply;")
        else:
            raise Pdl1500Error(f"can't print {stmt!r}")
    return "\n".join(lines) + ("\n" if lines else "")


_LINE = re.compile(r"^(iReset|iApply)\s*;$|^(iTarget|iWrite|iRead|iRunLoop)\s+([^;]*);$")
_SIZED = re.compile(r"^(\d+)'b([01]+)$")


def import_pdl_1500(text: str, interp: Wsp1500Interpreter) -> None:
    """Replay PDL text (:func:`to_pdl_1500`'s subset) on ``interp``."""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = _LINE.match(line)
        if match is None:
            raise Pdl1500Error(f"line {number}: not a PDL statement of this subset: {line!r}")
        if match.group(1) == "iReset":
            interp.iReset()
            continue
        if match.group(1) == "iApply":
            interp.iApply()
            continue
        word, args = match.group(2), match.group(3).split()
        if word == "iTarget" and len(args) == 1:
            interp.iTarget(args[0])
        elif word == "iRunLoop" and len(args) == 3 and args[1] == "-sck" and args[0].isdigit():
            interp.iRunLoop(int(args[0]))
        elif word in ("iWrite", "iRead") and len(args) == 2:
            sized = _SIZED.match(args[1])
            if word == "iRead":
                if sized is None:
                    raise Pdl1500Error(f"line {number}: iRead takes a sized binary literal")
                interp.iRead(args[0], int(sized.group(2), 2))
            else:
                interp.iWrite(args[0], int(sized.group(2), 2) if sized else args[1])
        else:
            raise Pdl1500Error(f"line {number}: malformed {word}: {line!r}")
