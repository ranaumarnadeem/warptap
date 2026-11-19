"""ebyst, an open-source boundary-scan tool warptap didn't write, reads the BSDL and drives the
inserted RTL.

ebyst (MIT, https://github.com/swolix/ebyst) tests real boards through an FTDI adapter. It
builds its model of a device from a BSDL file on its own: a pyparsing grammar the whole file
must match, its own reading of INSTRUCTION_OPCODE, IDCODE_REGISTER and BOUNDARY_REGISTER, and
its own JTAG sequencing. Its hardware interface is one method, ``transfer(tms, tdi) -> tdo``;
here that is ``jtag_bridge``'s live simulation of the design ``insert_bsr`` built
(``bsr_buses_design``), so every bit ebyst shifts comes from its own reading of warptap's BSDL,
and the pins are read from the board's side of the simulation.

ebyst ends every scan in Update-DR and stops TCK there. IEEE 1149.1 latches update stages on
the falling edge of TCK in Update-DR, so a conformant device has its new outputs by then;
warptap's cells latch on the rising edge that leaves Update-DR, half a period later (the
DESIGN_WARNING says so). So after each of ebyst's scans the checks below have ebyst take the TAP
on to Run-Test/Idle, one more TCK, which the protocol allows; the strict xfail
``test_an_update_lands_while_the_tap_waits_in_update_dr`` records the late update until it is
fixed.

ebyst needs Python 3.13; on older Pythons these tests skip.
"""

from __future__ import annotations

import asyncio
import io

import pytest

pytest.importorskip("ebyst")

import pyparsing  # noqa: E402  (ebyst's own dependency)
from bsr_buses_design import build  # noqa: E402
from ebyst import Device, JtagState, TapController  # noqa: E402
from ebyst.bsdl import BSDLFile  # noqa: E402
from ebyst.drivers.driver import Driver  # noqa: E402

from warptap.bsdl_emit import to_bsdl  # noqa: E402


class _BridgeDriver(Driver):
    """ebyst's driver interface, answered by the simulation."""

    def __init__(self, bridge):
        self.bridge = bridge

    def transfer(self, tms: int, tdi: int) -> int:
        return 1 if self.bridge.cycle(tms, tdi) == "1" else 0


@pytest.fixture(scope="module")
def design(fixtures_dir, yosys_command, tmp_path_factory):
    return build(fixtures_dir, tmp_path_factory.mktemp("bsr_buses_ebyst"), yosys_command)


def _controller(bsdl_text: str, tmp_path, bridge):
    path = tmp_path / "chip.bsd"
    path.write_text(bsdl_text, encoding="utf-8")
    device = Device.from_bsdl(str(path))
    bridge.trst(0)
    bridge.cycle(1)
    bridge.trst(1)
    controller = TapController(_BridgeDriver(bridge))
    controller.add_device(device)
    controller.validate_chain()  # IR length, device count, IDCODE against the BSDL, a SAMPLE
    return controller, device


def _port_and_position(design, pin_name: str):
    """``"din(2)"`` -> ``("din", 2)``'s place in the pin's integer value, from the plan's port
    ranges (this only says where the board's bridge keeps a bit)."""
    name, _, rest = pin_name.partition("(")
    port = next(p for p in design.plan.ports if p.name == name)
    return name, (port.position(int(rest.rstrip(")"))) if rest else 0)


def _kinds(device):
    outputs = sorted(n for n, p in device.pinmap.items() if p.output_cell and not p.input_cell)
    bidir = sorted(n for n, p in device.pinmap.items() if p.output_cell and p.input_cell)
    inputs = sorted(n for n, p in device.pinmap.items() if p.input_cell and not p.output_cell)
    return outputs, bidir, inputs


def _cycle(controller) -> None:
    """One of ebyst's EXTEST scans, then on to Run-Test/Idle (see the module docstring)."""
    asyncio.run(controller.cycle())
    controller.enter_state(JtagState.RUN_TEST_IDLE)


def _drive_check(design, device, controller, bridge, values) -> list:
    """Have ebyst drive each output and bidir pin to ``values[name]``; return the pins the
    board sees otherwise."""
    for name, value in values.items():
        device.pinmap[name].output_enable(True)
        device.pinmap[name].set_value(value)
    _cycle(controller)
    wrong = []
    for name, value in values.items():
        port, position = _port_and_position(design, name)
        if bridge.pin_bit(port, position) != str(value):
            wrong.append(name)
    return wrong


def test_ebyst_parses_both_bsdl_files_and_rejects_a_broken_one(design):
    BSDLFile.parse(io.StringIO(design.bsdl))
    BSDLFile.parse(io.StringIO(to_bsdl("chip", tck_max_freq_hz=1e6)))
    broken = design.bsdl.replace("entity is 23;", "entity is 23", 1)
    assert broken != design.bsdl
    with pytest.raises(pyparsing.ParseBaseException):
        BSDLFile.parse(io.StringIO(broken))


def test_ebyst_finds_the_chain_and_the_idcode_the_bsdl_declares(design, tmp_path, iverilog_command, vvp_command):
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        _controller(design.bsdl, tmp_path, bridge)
    other = to_bsdl("bsr_buses", tck_max_freq_hz=1e6, idcode_value=0x0BAD_C0DF, boundary=design.plan)
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        with pytest.raises(Exception, match="IDCode doesn't match"):
            _controller(other, tmp_path, bridge)


def test_ebyst_drives_every_output_and_reads_every_input(design, tmp_path, iverilog_command, vvp_command):
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        for port, direction, _ in design.ports:
            if direction == "inout":
                bridge.release(port)
        controller, device = _controller(design.bsdl, tmp_path, bridge)
        controller.extest()
        outputs, bidir, inputs = _kinds(device)
        assert outputs == ["dout(0)", "dout(1)", "dout(2)", "flag"]
        assert bidir == ["dbus(0)", "dbus(1)", "io"]
        driven = outputs + bidir
        for phase in (0, 1):
            values = {name: (k + phase) % 2 for k, name in enumerate(driven)}
            assert _drive_check(design, device, controller, bridge, values) == [], phase

        for name in driven:
            pin = device.pinmap[name]
            if pin.input_cell is None:
                # ebyst's Pin API takes "disable" to mean "make it an input", which an output3
                # pin can't be: set its control cell to the disable value ebyst read instead.
                pin.control_cell.out_value = pin.output_cell.out_dis_ctl
            else:
                pin.output_enable(False)
        _cycle(controller)
        for name in driven:
            assert bridge.pin_bit(*_port_and_position(design, name)) == "z", name

        board = {"a": 1, "din": 0b0110, "lane": 0b10, "io_oe": 1, "dbus_oe": 0, "io": 0, "dbus": 0b01}
        for port, value in board.items():
            bridge.drive(port, value)
        _cycle(controller)
        for name in inputs + bidir:
            port, position = _port_and_position(design, name)
            assert device.pinmap[name].get_value() == (board[port] >> position) & 1, name


@pytest.mark.parametrize("swap", [False, True], ids=["as_emitted", "two_cells_swapped"])
def test_ebyst_with_two_cells_swapped_drives_the_wrong_pins(design, tmp_path, swap, iverilog_command, vvp_command):
    """Control: from a BSDL with dout(0) and dout(1) swapped, ebyst's own drive lands on the
    wrong pins, and from the emitted one on the right pins, so the check above can tell."""
    text = design.bsdl
    if swap:
        text = text.replace("dout(0)", "@").replace("dout(1)", "dout(0)").replace("@", "dout(1)")
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        controller, device = _controller(text, tmp_path, bridge)
        controller.extest()
        wrong = _drive_check(design, device, controller, bridge, {"dout(0)": 1, "dout(1)": 0})
    assert sorted(wrong) == (["dout(0)", "dout(1)"] if swap else [])


@pytest.mark.xfail(
    strict=True,
    reason="warptap latches update stages on the rising edge that leaves Update-DR, half a TCK "
    "after IEEE 1149.1's falling edge in Update-DR",
)
def test_an_update_lands_while_the_tap_waits_in_update_dr(design, tmp_path, iverilog_command, vvp_command):
    with design.bridge(iverilog_command=iverilog_command, vvp_command=vvp_command) as bridge:
        controller, device = _controller(design.bsdl, tmp_path, bridge)
        controller.extest()
        device.pinmap["flag"].output_enable(True)
        device.pinmap["flag"].set_value(1)
        asyncio.run(controller.cycle())  # ends in Update-DR, TCK stopped
        assert controller.state is JtagState.UPDATE_DR
        assert bridge.pin_bit("flag", 0) == "1"
