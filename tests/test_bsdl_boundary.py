"""The full BSDL of a boundary-scan design, checked against the inserted RTL.

``to_bsdl(..., boundary=plan)`` describes the TAP and the boundary register ``insert_bsr``
inserts. These tests insert ``tests/fixtures/bsr_buses.v`` (see ``bsr_buses_design``) and drive
the inserted RTL in Icarus through ``jtag_bridge``, taking every bit they shift from the emitted
BSDL: which cell serves which port bit, which control cell enables it and which value disables
it, the opcodes, the instruction length and the IDCODE. warptap's plan is used only to build the
design, so a BSDL that misdescribed the RTL would fail here. ``test_bsdl_ebyst.py`` has a
third-party tool do the same from its own reading of the file.

Three of these pin insert_bsr fixes the full BSDL needed, checked against 0.0.6's insert_bsr
with the same bridge:

- It could not insert this fixture at all. It gave each port one cell, so a multi-bit inout
  raised ``BsrInsertError`` (its driver search expected a 1-bit ``$mux``), and with that port
  removed, Yosys refused to write the Verilog: a 1-bit ``$tribuf`` drove the whole 3-bit
  ``dout``.
- On ``tests/fixtures/trivial.v``, a scan under IDCODE between PRELOAD and EXTEST turned ``y``
  from the preloaded 0 to 1: the cells took tap_core's capture/shift/update strobes, which fire
  in every DR scan, whatever the instruction.
- IDCODE read the placeholder 0x1a5a5003: insert_bsr took no ``idcode_value``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest
from bsdl_reader import BoundaryCell, Bsdl, parse_bsdl, strict_problems
from bsr_buses_design import IDCODE, BsrDesign, build
from jtag_bridge import Jtag

from warptap.bsdl_emit import BsdlEmitError, to_bsdl
from warptap.bsr_plan import BsrPlan, BsrPort


@pytest.fixture(scope="module")
def design(fixtures_dir, yosys_command, tmp_path_factory) -> BsrDesign:
    return build(fixtures_dir, tmp_path_factory.mktemp("bsr_buses"), yosys_command)


@pytest.fixture(scope="module")
def parsed(design) -> Bsdl:
    return parse_bsdl(design.bsdl)


def _opcode(bsdl: Bsdl, name: str) -> int:
    return int(bsdl.opcodes[name][0], 2)


def _port_bit(cell: BoundaryCell) -> Tuple[str, Optional[int]]:
    m = re.fullmatch(r"(\w+)(?:\((\d+)\))?", cell.port)
    return m.group(1), (int(m.group(2)) if m.group(2) is not None else None)


def _position(bsdl: Bsdl, port: str, bit: Optional[int]) -> int:
    """Where port bit ``bit`` is in an integer pin value: the declaration's rightmost bit is
    the least significant."""
    declared = bsdl.port_bits[port]
    return 0 if bit is None else len(declared) - 1 - declared.index(bit)


def _value_bit(value: int, bsdl: Bsdl, port: str, bit: Optional[int]) -> int:
    return (value >> _position(bsdl, port, bit)) & 1


def _driven(bsdl: Bsdl) -> List[BoundaryCell]:
    return [c for c in bsdl.boundary_register if c.function in ("output3", "bidir")]


def _register(bsdl: Bsdl, data: Dict[int, int], enabled: bool) -> List[int]:
    """The boundary register's contents, cell 0 first: each output3/bidir cell holds
    ``data[cell number]`` and its control cell enables it (or, ``enabled=False``, holds the
    value that disables it)."""
    bits = [0] * bsdl.boundary_length
    for cell in _driven(bsdl):
        bits[cell.number] = data.get(cell.number, 0)
        bits[cell.ccell] = (1 - cell.disval) if enabled else cell.disval
    return bits


def _preload_then_extest(jtag: Jtag, bsdl: Bsdl, bits: List[int]) -> None:
    jtag.ir(_opcode(bsdl, "PRELOAD"), bsdl.instruction_length)
    jtag.dr(bits)
    jtag.ir(_opcode(bsdl, "EXTEST"), bsdl.instruction_length)


def _release_bidir_pins(bridge, bsdl: Bsdl) -> None:
    for port, direction in bsdl.ports.items():
        if direction == "inout":
            bridge.release(port)


def _drive_mismatches(design: BsrDesign, bsdl: Bsdl, pattern: int, iverilog_command, vvp_command) -> List[str]:
    """EXTEST every output3/bidir pin to bit ``k`` of ``pattern`` (k counting the driven cells
    in BSDL order), enabled, and report each pin that doesn't read what the BSDL says it
    should."""
    data = {cell.number: (pattern >> k) & 1 for k, cell in enumerate(_driven(bsdl))}
    mismatches = []
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        jtag = Jtag(bridge)
        jtag.reset()
        _release_bidir_pins(bridge, bsdl)
        _preload_then_extest(jtag, bsdl, _register(bsdl, data, enabled=True))
        for cell in _driven(bsdl):
            port, bit = _port_bit(cell)
            got = bridge.pin_bit(port, _position(bsdl, port, bit))
            if got != str(data[cell.number]):
                mismatches.append(f"{cell.port} reads {got}, not {data[cell.number]}")
    return mismatches


_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden_bsdl" / "bsr_buses.bsd"


def test_the_bsdl_matches_its_golden(design):
    """Pins the format; the tests below check what it says."""
    assert design.bsdl == _GOLDEN.read_bytes().decode("utf-8")


def test_the_bsdl_describes_every_port_bit_except_the_two_it_names(design, parsed):
    assert parsed.boundary_length == len(parsed.boundary_register) == len(design.plan.cells) == 23
    assert parsed.port_bits["din"] == [3, 2, 1, 0]
    assert parsed.port_bits["lane"] == [0, 1]
    assert parsed.ports["dbus"] == "inout" and parsed.ports["dout"] == "out"
    assert parsed.register_access["BOUNDARY"] == ["EXTEST", "SAMPLE", "PRELOAD"]
    assert strict_problems(design.bsdl) == [
        "port clk has no boundary-register cell",
        "port rst_n has no boundary-register cell",
    ]
    assert "neither observes nor controls them: clk, rst_n." in parsed.design_warning


def test_sample_captures_every_pin_and_every_enable(design, parsed, iverilog_command, vvp_command):
    board = {"rst_n": 0, "a": 1, "din": 0b1011, "lane": 0b01, "io_oe": 0, "dbus_oe": 1}
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        jtag = Jtag(bridge)
        jtag.reset()
        bridge.drive("rst_n", 1)  # q resets on a falling edge of rst_n
        for port, value in board.items():
            bridge.drive(port, value)
        bridge.drive("io", 1)  # io_oe=0: the chip doesn't drive io, the board does
        bridge.release("dbus")  # dbus_oe=1: the chip drives dbus with din[1:0]
        jtag.ir(_opcode(parsed, "SAMPLE"), parsed.instruction_length)
        captured = jtag.dr([0] * parsed.boundary_length)

    pins = {**board, "io": 1, "dbus": board["din"] & 0b11}
    functional = {"dout": 0, "flag": bin(board["din"]).count("1") & 1}  # rst_n=0 holds q at 0
    enables = {"io": board["io_oe"], "dbus": board["dbus_oe"]}
    controlled = {c.ccell: c for c in _driven(parsed)}
    for cell in parsed.boundary_register:
        if cell.function == "control":
            port, _ = _port_bit(controlled[cell.number])
            expected = enables.get(port, 1)  # an output3's enable is 1 outside EXTEST
        else:
            port, bit = _port_bit(cell)
            source = functional if cell.function == "output3" else pins
            expected = _value_bit(source[port], parsed, port, bit)
        assert captured[cell.number] == str(expected), (cell, captured[cell.number])


@pytest.mark.parametrize("pattern", [0b101100110, 0b010011001], ids=["pattern", "complement"])
def test_extest_drives_every_output_bit_as_the_bsdl_says(design, parsed, pattern, iverilog_command, vvp_command):
    assert _drive_mismatches(design, parsed, pattern, iverilog_command, vvp_command) == []


def test_each_control_cell_tri_states_its_pin_at_its_disable_value(design, parsed, iverilog_command, vvp_command):
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        jtag = Jtag(bridge)
        jtag.reset()
        _release_bidir_pins(bridge, parsed)
        ones = {cell.number: 1 for cell in _driven(parsed)}
        _preload_then_extest(jtag, parsed, _register(parsed, ones, enabled=False))
        for cell in _driven(parsed):
            port, bit = _port_bit(cell)
            assert bridge.pin_bit(port, _position(parsed, port, bit)) == "z", cell


def test_extest_captures_bidir_pins_the_board_drives(design, parsed, iverilog_command, vvp_command):
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        jtag = Jtag(bridge)
        jtag.reset()
        _preload_then_extest(jtag, parsed, _register(parsed, {}, enabled=False))
        board = {"io": 1, "dbus": 0b10}
        for port, value in board.items():
            bridge.drive(port, value)
        captured = jtag.dr(_register(parsed, {}, enabled=False))
    for cell in parsed.boundary_register:
        if cell.function == "bidir":
            port, bit = _port_bit(cell)
            assert captured[cell.number] == str(_value_bit(board[port], parsed, port, bit)), cell


def test_a_scan_under_idcode_or_bypass_leaves_what_preload_set(design, parsed, iverilog_command, vvp_command):
    data = {cell.number: k % 2 for k, cell in enumerate(_driven(parsed))}
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        jtag = Jtag(bridge)
        jtag.reset()
        _release_bidir_pins(bridge, parsed)
        jtag.ir(_opcode(parsed, "PRELOAD"), parsed.instruction_length)
        jtag.dr(_register(parsed, data, enabled=True))
        jtag.ir(_opcode(parsed, "IDCODE"), parsed.instruction_length)
        jtag.dr([1] * 32)
        jtag.ir(_opcode(parsed, "BYPASS"), parsed.instruction_length)
        jtag.dr([1] * 8)
        jtag.ir(_opcode(parsed, "EXTEST"), parsed.instruction_length)
        for cell in _driven(parsed):
            port, bit = _port_bit(cell)
            assert bridge.pin_bit(port, _position(parsed, port, bit)) == str(data[cell.number]), cell


def test_the_tap_reports_the_idcode_the_bsdl_declares(design, parsed, iverilog_command, vvp_command):
    assert int(parsed.idcode_register, 2) == IDCODE
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        jtag = Jtag(bridge)
        jtag.reset()
        jtag.ir(_opcode(parsed, "IDCODE"), parsed.instruction_length)
        shifted = jtag.dr([0] * 32)  # least significant bit first
    assert int("".join(reversed(shifted)), 2) == IDCODE


def test_a_bsdl_with_two_cells_swapped_fails_the_extest_check(design, parsed, iverilog_command, vvp_command):
    """Control: the check above can tell a wrong BSDL from the right one. The pattern gives the
    two swapped cells (driven cells 5 and 6 in BSDL order) different values."""
    swapped = design.bsdl.replace("dout(0)", "@").replace("dout(1)", "dout(0)").replace("@", "dout(1)")
    assert swapped != design.bsdl
    assert _drive_mismatches(design, parsed, 0b0100110, iverilog_command, vvp_command) == []
    mismatches = _drive_mismatches(design, parse_bsdl(swapped), 0b0100110, iverilog_command, vvp_command)
    assert sorted(m.split()[0] for m in mismatches) == ["dout(0)", "dout(1)"]


def test_the_boundary_bsdl_refuses_what_it_cannot_describe(design):
    with pytest.raises(BsdlEmitError, match="ijtag_access_opcode"):
        to_bsdl("bsr_buses", tck_max_freq_hz=1e6, boundary=design.plan, ijtag_access_opcode=0b1100)
    with pytest.raises(BsdlEmitError, match="no port list"):
        to_bsdl("bsr_buses", tck_max_freq_hz=1e6, boundary=BsrPlan(design.plan.cells, []))
    reserved = design.plan._replace(ports=(*design.plan.ports, BsrPort("bus", "input", 1)))
    with pytest.raises(BsdlEmitError, match="'bus' is not a valid BSDL port name"):
        to_bsdl("bsr_buses", tck_max_freq_hz=1e6, boundary=reserved)
    clash = design.plan._replace(ports=(*design.plan.ports, BsrPort("TDO", "output", 1)))
    with pytest.raises(BsdlEmitError, match="clashes with a TAP pin"):
        to_bsdl("bsr_buses", tck_max_freq_hz=1e6, boundary=clash)
