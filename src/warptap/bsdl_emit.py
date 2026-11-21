"""BSDL (IEEE 1149.1 Boundary-Scan Description Language) emitter for the TAP warptap inserts.

Scope is exactly what an IEEE 1687 ICL ``AccessLink ... Of STD_1149_1_2001`` needs to point at:
the entity, the five TAP pins, the TAP scan attributes, the instruction register (length,
opcodes, capture pattern), the IDCODE register, and the instruction that reaches the IJTAG
network. Every value comes from :mod:`warptap.tap_model` / :mod:`warptap.tap_ports` -- no literal
is duplicated here -- so this file can only drift from ``rtl/tap_core.v`` if ``tap_model`` itself
does, which ``tests/test_tap_fsm_cross_sim.py`` checks against the RTL.

**Two kinds of file.** For a SIB-network design (no boundary register) the BSDL is TAP-only: it
declares no ``BOUNDARY_LENGTH`` and no ``BOUNDARY_REGISTER``. Standard-following BSDL parsers and
boundary-scan tools treat the boundary register as mandatory (IEEE 1149.1's
BYPASS/SAMPLE/PRELOAD/EXTEST requirements all concern it), so expect such a tool to reject that
file or report it incomplete. For a design with a boundary-scan register inserted by
:func:`warptap.bsr_insert.insert_bsr`, pass ``to_bsdl`` the same plan as ``boundary``: the file
then describes the whole chip, every port in the port list with its declared range, the boundary
register cell by cell (numbered from TDO, as BSDL numbers them, the reverse of the plan's
numbering from TDI), and ``REGISTER_ACCESS`` giving EXTEST, SAMPLE and PRELOAD the boundary
register. Ports the plan left off the register are named in the ``DESIGN_WARNING``. The
mandatory-attribute list here was taken from secondary sources (ASSET InterTech's BSDL overview,
Lauterbach's boundary-scan guide, the IEEE 1149.1 working group's published
``STD_1149_1_2012`` package declarations); the primary IEEE 1149.1 text is paywalled and was not
read.

**IJTAG_ACCESS.** In real 1149.1, EXTEST drives the boundary register, and a warptap SIB design
has none. The TAP :mod:`warptap.sib_insert` inserts by default selects the IJTAG network with an
instruction of its own, IJTAG_ACCESS, at ``ijtag_access_opcode``
(:data:`warptap.tap_model.OPCODE_IJTAG_ACCESS`, ``1100``), and ``to_bsdl`` declares it there by
default (pass it the opcode the TAP was inserted with, if another): ``REGISTER_ACCESS`` gives
EXTEST, SAMPLE and PRELOAD the BYPASS register, which is what they select in that TAP. A tool that
holds those three to the boundary register will object; there is none to give them, and the
``DESIGN_WARNING`` says so. The network's length depends on SIB state, so it cannot be declared
as a fixed-length ``REGISTER_ACCESS`` entry and is deliberately not.

**EXTEST.** A TAP inserted with ``ijtag_access_opcode=None`` selects the network with EXTEST
instead; pass ``to_bsdl`` the same ``None``. ``rtl/tap_core.v`` presents the SIB chain's tail
(``external_dr_tdo``) at ``tdo`` for every instruction that is neither IDCODE nor BYPASS, but
there :mod:`warptap.sib_insert` decodes EXTEST into every top-level SIB's ``select``, so only
EXTEST moves the IJTAG network: under SAMPLE/PRELOAD its tail reaches ``tdo`` but it holds still
(see :data:`warptap.tap_model.NETWORK_ACCESS_INSTRUCTION`). The emitted ``DESIGN_WARNING`` says
so.

**The IDCODE** is whatever the design's ``tap_core`` was built with: pass ``to_bsdl`` the same
``idcode_value`` given to ``insert_test_access``/``insert_sib_network``. The default,
:data:`warptap.tap_model.IDCODE_VALUE`, is a placeholder, and only then does the output call it
one.

**TDO** is driven however the design's TAP was inserted: pass ``to_bsdl`` the same ``tdo_drive``.
By default (``"tristate"``) the inserted TAP releases TDO outside Shift-IR/Shift-DR, as IEEE
1149.1 requires, and the ``DESIGN_WARNING`` says nothing about it; ``"low"`` and
``"enable_port"`` each get a sentence there saying how the pin is driven instead.

**What checks the output.** The emitted text is read back and compared with ``tap_model``
(``tests/test_bsdl_emit.py``), and every behavioral claim is verified by driving the real TAP RTL
under Icarus (``tests/test_bsdl_emit_cross_sim.py``; the TDO pin of each ``tdo_drive``, on
inserted designs, in ``tests/test_tdo_drive_cross_sim.py``). A boundary-scan design's BSDL is
checked against its inserted RTL with every bit taken from the BSDL itself
(``tests/test_bsdl_boundary.py``), and by ebyst, an open-source boundary-scan tool warptap didn't
write: it parses the file with its own grammar, builds its device model from it, validates the
chain (IR length, IDCODE) and drives and reads every pin of the simulated design
(``tests/test_bsdl_ebyst.py``, Python 3.13). The other candidates fall short: UrJTAG's
Ubuntu-packaged build fails on every input, and cb_bsdl_parser crashes on a BC_7 cell and
misreads multi-bit ports (``tests/test_bsdl_emit.py``'s docstring has the evidence).

**When updates land.** The instruction register and every update stage latch on the rising edge
of TCK that leaves Update-IR or Update-DR, half a TCK period after IEEE 1149.1's falling edge in
those states. Patterns that keep TCK running see no difference; a tester that stops TCK in
Update-IR or Update-DR (ebyst does) sees the change only at the next rising edge. Every file's
``DESIGN_WARNING`` says so.
"""

from __future__ import annotations

import math
import re
from typing import Any, List

from warptap.bsr_plan import BsrCell, BsrFunction, BsrPlan, BsrPort
from warptap.errors import WarptapError
from warptap.tap_model import (
    CAPTURE_IR_PATTERN,
    DEFAULT_IR_WIDTH,
    DEFAULT_TDO_DRIVE,
    IDCODE_VALUE,
    NETWORK_ACCESS_INSTRUCTION,
    OPCODE_EXTEST,
    OPCODE_IDCODE,
    OPCODE_IJTAG_ACCESS,
    OPCODE_SAMPLE_PRELOAD,
    Instruction,
    bypass_opcode,
    check_tdo_drive,
    idcode_value_error,
    ijtag_access_opcode_error,
)
from warptap.tap_ports import TCK, TDI, TDO, TMS, TRST_N

NETWORK_ACCESS_BSDL_INSTRUCTION = NETWORK_ACCESS_INSTRUCTION.value
"""The BSDL instruction name that selects the IJTAG network of a TAP inserted with
``ijtag_access_opcode=None``, EXTEST: what the ICL ``AccessLink`` of
``to_icl(..., ijtag_access=False)`` names."""

IJTAG_ACCESS_BSDL_INSTRUCTION = Instruction.IJTAG_ACCESS.value
"""The name the network's instruction has in a TAP built with an ``ijtag_access_opcode``, as
warptap inserts it by default: what the ICL ``AccessLink`` names by default."""

# to_bsdl's ijtag_access_opcode default: OPCODE_IJTAG_ACCESS without a boundary register, None
# with one. A value no caller passes, so an opcode given together with a boundary is still refused.
_DEFAULT_OPCODE: Any = object()

_PHYSICAL_PIN_MAP = "UNPACKAGED"
_PIN_MAP_STRING_WIDTH = 68

_IDENTIFIER = re.compile(r"^[A-Za-z](_?[A-Za-z0-9])*$")

# VHDL-93 reserved words: a BSDL entity name is a VHDL identifier and may not be one of these.
_VHDL_RESERVED = frozenset(
    "abs access after alias all and architecture array assert attribute begin block body "
    "buffer bus case component configuration constant disconnect downto else elsif end entity "
    "exit file for function generate generic group guarded if impure in inertial inout is "
    "label library linkage literal loop map mod nand new next nor not null of on open or "
    "others out package port postponed procedure process pure range record register reject "
    "rem report return rol ror select severity shared signal sla sll sra srl subtype then to "
    "transport type unaffected units until use variable wait when while with xnor xor".split()
)


class BsdlEmitError(WarptapError):
    """Raised for an entity name that isn't a valid BSDL/VHDL identifier, or a TCK frequency
    that isn't a finite positive number."""


def _bits(value: int, width: int) -> str:
    """MSB-left bit string: BSDL writes the bit closest to TDI on the left, so the rightmost
    character is the first bit shifted out of TDO (the LSB, matching this project's LSB-first
    shifting everywhere)."""
    return format(value, f"0{width}b")


def _vhdl_string(text: str, width: int = _PIN_MAP_STRING_WIDTH) -> str:
    """``text`` as a concatenation of VHDL string literals, split at word boundaries so no line
    is long. The pieces concatenate back to exactly ``text``."""
    assert '"' not in text, "BSDL string content must not contain a double quote"
    pieces: List[str] = []
    current = ""
    for token in re.findall(r"\S+\s*", text):
        if current and len(current) + len(token) > width:
            pieces.append(current)
            current = ""
        current += token
    if current:
        pieces.append(current)
    return " &\n      ".join(f'"{piece}"' for piece in pieces)


_UPDATE_TIMING = (
    "The instruction register and every update stage latch on the rising edge of TCK that leaves "
    "Update-IR or Update-DR, half a TCK period after IEEE 1149.1's falling edge in those states: "
    "a tester that stops TCK in Update-IR or Update-DR sees the new instruction or outputs only "
    "at the next rising edge. "
)


def _tdo_warning(tdo_drive: str) -> str:
    """What the reader must know about the TDO pin, which only ``"tristate"`` makes conformant
    on its own; empty for it."""
    if tdo_drive == "low":
        return (
            "Not fully IEEE 1149.1 conformant: TDO is driven low, not tri-stated, outside the "
            "shift states. "
        )
    if tdo_drive == "enable_port":
        return (
            "TDO is not tri-stated inside this design: it is driven at all times (low outside "
            "the shift states), and the pad that drives the TDO pin must do the tri-state, "
            "enabled by the design's tdo_oe output, which is high only in Shift-IR and Shift-DR. "
        )
    return ""


def _design_warning(
    placeholder_idcode: bool, ijtag_access_opcode: int | None, tdo_drive: str
) -> str:
    """Everything a reader of this BSDL must know that the standard attributes can't say. Built
    from the same constants as the attributes above it so the two can't disagree."""
    ir_width = DEFAULT_IR_WIDTH
    extest = _bits(OPCODE_EXTEST, ir_width)
    sample_preload = _bits(OPCODE_SAMPLE_PRELOAD, ir_width)
    if ijtag_access_opcode is None:
        routing = (
            f"{NETWORK_ACCESS_BSDL_INSTRUCTION} ({extest}) and SAMPLE/PRELOAD ({sample_preload}) "
            "do not select a boundary register: both put the tail of the IEEE 1687 IJTAG network "
            "(the SIB chain) described by the companion ICL AccessLink on TDO, but only "
            f"{NETWORK_ACCESS_BSDL_INSTRUCTION} selects the network, so only under it does the "
            "network capture, shift and update. Its length depends on SIB state, so it is not "
            "declared in REGISTER_ACCESS. "
            f"Load {NETWORK_ACCESS_BSDL_INSTRUCTION} to reach that network. "
        )
    else:
        ijtag = _bits(ijtag_access_opcode, ir_width)
        routing = (
            f"{IJTAG_ACCESS_BSDL_INSTRUCTION} ({ijtag}) selects the IEEE 1687 IJTAG network (the "
            "SIB chain) described by the companion ICL AccessLink: load it to reach that "
            "network. Its length depends on SIB state, so it is not declared in REGISTER_ACCESS. "
            f"With no boundary register, EXTEST ({extest}) and SAMPLE/PRELOAD ({sample_preload}) "
            "select the 1-bit BYPASS register, as REGISTER_ACCESS states, so a board-level "
            "EXTEST leaves the network untouched. "
        )
    return (
        "Generated by warptap. TAP-only description, NOT a chip-level BSDL: no BOUNDARY_LENGTH "
        "or BOUNDARY_REGISTER is declared, so a tool that requires a boundary register will "
        "reject this file. "
        + routing
        + _tdo_warning(tdo_drive)
        + _UPDATE_TIMING
        + (
            "The IDCODE value is a placeholder, not a registered manufacturer ID. "
            if placeholder_idcode
            else ""
        )
        + f"PHYSICAL_PIN_MAP {_PHYSICAL_PIN_MAP} is a placeholder: no physical package is "
        "described."
    )


def _boundary_design_warning(
    skipped_ports: List[str], placeholder_idcode: bool, tdo_drive: str
) -> str:
    """The ``DESIGN_WARNING`` of a design with a boundary-scan register."""
    return (
        "Generated by warptap. "
        + (
            "These ports have no boundary-register cell, so boundary scan neither observes nor "
            f"controls them: {', '.join(skipped_ports)}. "
            if skipped_ports
            else ""
        )
        + _tdo_warning(tdo_drive)
        + _UPDATE_TIMING
        + (
            "The IDCODE value is a placeholder, not a registered manufacturer ID. "
            if placeholder_idcode
            else ""
        )
        + f"PHYSICAL_PIN_MAP {_PHYSICAL_PIN_MAP} is a placeholder: no physical package is "
        "described, and each pin is named after its port."
    )


def _port_type(port: BsrPort) -> str:
    if not port.is_vector:
        return "bit"
    high = port.offset + port.width - 1
    if port.upto:
        return f"bit_vector({port.offset} to {high})"
    return f"bit_vector({high} downto {port.offset})"


def _declared_bits(port: BsrPort) -> List[int]:
    """A vector port's HDL bit numbers in declaration order, left to right: the order a
    PIN_MAP_STRING lists its pins in."""
    high = port.offset + port.width - 1
    return list(range(port.offset, high + 1)) if port.upto else list(range(high, port.offset - 1, -1))


def _cell_port(cell: BsrCell) -> str:
    if cell.function is BsrFunction.CONTROL:
        return "*"
    return cell.port_name if cell.bit is None else f"{cell.port_name}({cell.bit})"


def _boundary_entries(cells: List[BsrCell]) -> List[str]:
    """BOUNDARY_REGISTER entries, numbered as BSDL numbers them: cell 0 is the one nearest
    TDO, the reverse of the plan's numbering from TDI. Every control cell disables its outputs
    at 0 (bsr_insert wires its ``pin_out`` straight to their tri-state enables), and a disabled
    output is high impedance."""
    last = len(cells) - 1
    by_bsdl_number = sorted(cells, key=lambda c: last - c.cell_number)
    entries = []
    for cell in by_bsdl_number:
        number = last - cell.cell_number
        head = f"{number} ({cell.cell_type}, {_cell_port(cell)}, {cell.function.value}"
        if cell.function is BsrFunction.INPUT:
            entries.append(f"{head}, X)")
        elif cell.function is BsrFunction.CONTROL:
            entries.append(f"{head}, {cell.safe_value})")
        elif cell.function in (BsrFunction.OUTPUT3, BsrFunction.BIDIR):
            entries.append(f"{head}, X, {last - cell.disable_cell_index}, 0, Z)")
        else:
            raise BsdlEmitError(f"no BSDL description for a {cell.function.value} cell")
    return entries


def _check_boundary(boundary: BsrPlan, ijtag_access_opcode: int | None) -> None:
    if ijtag_access_opcode is not None:
        raise BsdlEmitError(
            "ijtag_access_opcode is for a TAP whose network is an IJTAG one; a boundary-scan "
            "design's EXTEST and SAMPLE/PRELOAD select its boundary register, so leave it out "
            "with boundary"
        )
    if not boundary.ports:
        raise BsdlEmitError(
            "the boundary-scan plan has no port list: build it with build_bsr_plan"
        )
    seen = {p.lower() for p in (TCK, TMS, TDI, TRST_N, TDO)}
    for port in boundary.ports:
        if not _IDENTIFIER.match(port.name) or port.name.lower() in _VHDL_RESERVED:
            raise BsdlEmitError(
                f"port {port.name!r} is not a valid BSDL port name: it must be a VHDL "
                "identifier (starts with a letter; letters, digits and single underscores "
                "only; not a VHDL reserved word)"
            )
        if port.name.lower() in seen:
            raise BsdlEmitError(
                f"port {port.name!r} clashes with a TAP pin or another port (BSDL names are "
                "case-insensitive)"
            )
        seen.add(port.name.lower())
    names = {p.name for p in boundary.ports}
    for cell in boundary.cells:
        if cell.function is not BsrFunction.CONTROL and cell.port_name not in names:
            raise BsdlEmitError(f"cell {cell.cell_number} names port {cell.port_name!r}, which the plan doesn't list")


def to_bsdl(
    entity_name: str,
    *,
    tck_max_freq_hz: float,
    idcode_value: int = IDCODE_VALUE,
    ijtag_access_opcode: int | None = _DEFAULT_OPCODE,
    tdo_drive: str = DEFAULT_TDO_DRIVE,
    boundary: BsrPlan | None = None,
) -> str:
    """Render the BSDL for the TAP warptap inserts, as the entity ``entity_name`` (use the same
    string passed to ``to_icl`` as its ``BSDLEntity`` -- the real top module's name).

    ``tck_max_freq_hz`` is required: BSDL's mandatory ``TAP_SCAN_CLOCK`` states the maximum TCK
    frequency, and TCK timing is not something the RTL tells warptap -- the integrator must
    state it. The clock is declared safe to stop in either state (``BOTH``): ``rtl/tap_core.v``
    is fully static edge-triggered logic. Raises :class:`BsdlEmitError` for an entity name that
    isn't a valid BSDL/VHDL identifier (letters, digits, single underscores, starts with a letter,
    not a VHDL reserved word) or a non-positive/non-finite frequency.

    ``idcode_value`` must be the IDCODE the design's ``tap_core`` was built with (the
    ``idcode_value`` given to ``insert_test_access``/``insert_sib_network``): a 32-bit value with
    bit 0 set, as IEEE 1149.1 requires, else :class:`BsdlEmitError`. Only the default,
    :data:`~warptap.tap_model.IDCODE_VALUE`, is described as a placeholder in the output.

    ``ijtag_access_opcode`` must be the one the design's TAP was built with. Left out, it is
    :data:`~warptap.tap_model.OPCODE_IJTAG_ACCESS`, the opcode
    :func:`~warptap.sib_insert.insert_sib_network` gives IJTAG_ACCESS by default (or ``None``
    with ``boundary``, below). With an opcode, an ``IJTAG_ACCESS`` instruction is declared at it
    and ``REGISTER_ACCESS`` gives EXTEST, SAMPLE and PRELOAD the BYPASS register; an opcode no
    TAP can have raises :class:`BsdlEmitError`. ``None`` is a TAP whose network instruction is
    EXTEST, inserted with ``ijtag_access_opcode=None``.

    ``tdo_drive`` must be the one the design's TAP was inserted with
    (:data:`warptap.tap_model.TDO_DRIVES`). The default, ``"tristate"``, gets no warning about
    TDO: the pin is high impedance outside Shift-IR/Shift-DR, as IEEE 1149.1 requires. With
    ``"low"`` the ``DESIGN_WARNING`` says the TAP is not fully conformant, TDO being driven low
    there; with ``"enable_port"`` it says the tri-state is the pad's, enabled from ``tdo_oe``.
    Anything else raises ValueError.

    ``boundary`` is the :class:`~warptap.bsr_plan.BsrPlan` a boundary-scan design was inserted
    with (:func:`warptap.bsr_insert.insert_bsr`); without it the file is TAP-only. With it, the
    file declares every port of the plan and the boundary register, and ``ijtag_access_opcode``
    must be left out or ``None``: such a design has no IJTAG network, and its EXTEST, SAMPLE and
    PRELOAD select the boundary register. Raises :class:`BsdlEmitError` for a
    plan without a port list (one not from :func:`~warptap.bsr_plan.build_bsr_plan`), a port
    name that isn't a valid BSDL/VHDL identifier or clashes with a TAP pin (``bus`` is a VHDL
    reserved word, for instance), or a cell naming a port the plan doesn't list.

    See the module docstring for the two kinds of file and what checks each."""
    if ijtag_access_opcode is _DEFAULT_OPCODE:
        ijtag_access_opcode = OPCODE_IJTAG_ACCESS if boundary is None else None
    check_tdo_drive(tdo_drive)
    if problem := idcode_value_error(idcode_value):
        raise BsdlEmitError(problem)
    if ijtag_access_opcode is not None and (
        problem := ijtag_access_opcode_error(ijtag_access_opcode)
    ):
        raise BsdlEmitError(problem)
    if not _IDENTIFIER.match(entity_name) or entity_name.lower() in _VHDL_RESERVED:
        raise BsdlEmitError(
            f"{entity_name!r} is not a valid BSDL entity name: it must be a VHDL identifier "
            "(starts with a letter; letters, digits and single underscores only; not a VHDL "
            "reserved word). Pass a different entity_name and give the ICL BSDLEntity the same one."
        )
    if (
        isinstance(tck_max_freq_hz, bool)
        or not isinstance(tck_max_freq_hz, (int, float))
        or not math.isfinite(tck_max_freq_hz)
        or tck_max_freq_hz <= 0
    ):
        raise BsdlEmitError(
            f"tck_max_freq_hz must be a finite number > 0 (Hz), got {tck_max_freq_hz!r}"
        )

    if boundary is not None:
        _check_boundary(boundary, ijtag_access_opcode)

    name = entity_name
    ir_width = DEFAULT_IR_WIDTH
    pins = (TCK, TMS, TDI, TRST_N, TDO)
    system_ports = list(boundary.ports) if boundary is not None else []
    pin_width = max(len(p) for p in (*pins, *(port.name for port in system_ports)))

    port_lines = [
        f"    {pin.ljust(pin_width)} : {'out' if pin == TDO else 'in'} bit" for pin in pins
    ]
    pin_map_entries = [f"{pin.ljust(pin_width)} : {pin}" for pin in pins]
    _modes = {"input": "in", "output": "out", "inout": "inout"}
    for port in system_ports:
        port_lines.append(f"    {port.name.ljust(pin_width)} : {_modes[port.direction]} {_port_type(port)}")
        if port.is_vector:
            pin_ids = ", ".join(f"{port.name}_{bit}" for bit in _declared_bits(port))
            pin_map_entries.append(f"{port.name.ljust(pin_width)} : ({pin_ids})")
        else:
            pin_map_entries.append(f"{port.name.ljust(pin_width)} : {port.name}")
    pin_map_lines = [
        f'    "{entry}{"," if i < len(pin_map_entries) - 1 else ""}"'
        for i, entry in enumerate(pin_map_entries)
    ]

    placeholder_idcode = idcode_value == IDCODE_VALUE
    version = idcode_value >> 28
    part = (idcode_value >> 12) & 0xFFFF
    manufacturer = (idcode_value >> 1) & 0x7FF
    required_one = idcode_value & 1

    opcode_entries = [
        ("EXTEST", OPCODE_EXTEST),
        ("SAMPLE", OPCODE_SAMPLE_PRELOAD),
        ("PRELOAD", OPCODE_SAMPLE_PRELOAD),
        ("IDCODE", OPCODE_IDCODE),
        ("BYPASS", bypass_opcode(ir_width)),
    ]
    register_entries = ["DEVICE_ID (IDCODE)"]
    if boundary is not None:
        register_entries.insert(0, "BOUNDARY (EXTEST, SAMPLE, PRELOAD)")
    if ijtag_access_opcode is not None:
        opcode_entries.append((IJTAG_ACCESS_BSDL_INSTRUCTION, ijtag_access_opcode))
        # What they select in that TAP: there is no boundary register.
        register_entries.append("BYPASS (EXTEST, SAMPLE, PRELOAD)")
    name_width = max(len(n) for n, _ in opcode_entries)
    opcode_lines = [
        f'    "{n.ljust(name_width)} ({_bits(op, ir_width)}){"," if i < len(opcode_entries) - 1 else ""}"'
        for i, (n, op) in enumerate(opcode_entries)
    ]
    opcode_body = " &\n".join(opcode_lines)
    register_lines = [
        f'    "{entry}{"," if i < len(register_entries) - 1 else ""}"'
        for i, entry in enumerate(register_entries)
    ]

    if boundary is None:
        title = "-- BSDL for the IEEE 1149.1 TAP inserted by warptap (TAP only; no boundary register)."
        warning = _design_warning(placeholder_idcode, ijtag_access_opcode, tdo_drive)
        boundary_lines: List[str] = []
    else:
        title = "-- BSDL for the IEEE 1149.1 TAP and boundary-scan register inserted by warptap."
        warning = _boundary_design_warning(
            list(boundary.skipped_ports), placeholder_idcode, tdo_drive
        )
        entries = _boundary_entries(list(boundary.cells))
        boundary_lines = [
            f"  attribute BOUNDARY_LENGTH of {name} : entity is {len(entries)};",
            "",
            f"  attribute BOUNDARY_REGISTER of {name} : entity is",
            " &\n".join(
                f'    "{entry}{"," if i < len(entries) - 1 else ""}"'
                for i, entry in enumerate(entries)
            )
            + ";",
            "",
        ]

    lines = [
        title,
        "-- Read the DESIGN_WARNING attribute below before using this file with a boundary-scan tool.",
        "",
        f"entity {name} is",
        "",
        f'  generic (PHYSICAL_PIN_MAP : string := "{_PHYSICAL_PIN_MAP}");',
        "",
        "  port (",
        ";\n".join(port_lines),
        "  );",
        "",
        "  use STD_1149_1_2001.all;",
        "",
        f'  attribute COMPONENT_CONFORMANCE of {name} : entity is "STD_1149_1_2001";',
        "",
        f"  attribute PIN_MAP of {name} : entity is PHYSICAL_PIN_MAP;",
        "",
        f"  constant {_PHYSICAL_PIN_MAP} : PIN_MAP_STRING :=",
        " &\n".join(pin_map_lines) + ";",
        "",
        f"  attribute TAP_SCAN_IN    of {TDI.ljust(pin_width)} : signal is true;",
        f"  attribute TAP_SCAN_MODE  of {TMS.ljust(pin_width)} : signal is true;",
        f"  attribute TAP_SCAN_OUT   of {TDO.ljust(pin_width)} : signal is true;",
        f"  attribute TAP_SCAN_CLOCK of {TCK.ljust(pin_width)} : signal is ({tck_max_freq_hz:.6e}, BOTH);",
        f"  attribute TAP_SCAN_RESET of {TRST_N.ljust(pin_width)} : signal is true;",
        "",
        f"  attribute INSTRUCTION_LENGTH of {name} : entity is {ir_width};",
        "",
        f"  attribute INSTRUCTION_OPCODE of {name} : entity is",
        opcode_body + ";",
        "",
        f'  attribute INSTRUCTION_CAPTURE of {name} : entity is "{_bits(CAPTURE_IR_PATTERN, ir_width)}";',
        "",
        f"  attribute IDCODE_REGISTER of {name} : entity is",
        f'    "{_bits(version, 4)}" &{" " * 18}-- version',
        f'    "{_bits(part, 16)}" &{" " * 6}-- part number',
        f'    "{_bits(manufacturer, 11)}" &{" " * 11}-- manufacturer'
        + (" (placeholder)" if placeholder_idcode else ""),
        f'    "{_bits(required_one, 1)}";{" " * 22}-- required by IEEE 1149.1',
        "",
        f"  attribute REGISTER_ACCESS of {name} : entity is",
        " &\n".join(register_lines) + ";",
        "",
        *boundary_lines,
        f"  attribute DESIGN_WARNING of {name} : entity is",
        "      " + _vhdl_string(warning) + ";",
        "",
        f"end {name};",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "BsdlEmitError",
    "IJTAG_ACCESS_BSDL_INSTRUCTION",
    "NETWORK_ACCESS_BSDL_INSTRUCTION",
    "to_bsdl",
]
