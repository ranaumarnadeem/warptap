"""The ``tdo_drive`` keyword of the TAP insertions (:data:`warptap.tap_model.TDO_DRIVES`): what
each value builds around the ``tdo`` pin, for both insertions, and that anything else is refused
before anything is built. Structure only: ``tests/test_tdo_drive_cross_sim.py`` simulates the pin.
"""

from __future__ import annotations

import copy
import inspect

import pytest

from warptap.bsr_insert import insert_bsr
from warptap.bsr_plan import build_bsr_plan
from warptap.netlist import Module, Netlist
from warptap.pipeline import insert_test_access
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_model import DEFAULT_TDO_DRIVE, TDO_DRIVES, check_tdo_drive
from warptap.tap_ports import TCK, TDI, TDO, TDO_OE, TMS, TRST_N
from warptap.yosys_io import ingest

_SPECS = [InstrumentSpec("stub", width=1, capture_value=0)]


def _sib_netlist(fixtures_dir, yosys_command) -> tuple[Netlist, str]:
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    return Netlist.from_json(raw), "real_signal"


def _bsr_netlist(fixtures_dir, yosys_command) -> tuple[Netlist, str]:
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    return Netlist.from_json(raw), "trivial"


def _insert_sib(netlist, top, yosys_command, **options):
    graph, _root = build_sib_plan(_SPECS)
    insert_sib_network(netlist, top, graph, yosys_command=yosys_command, **options)


def _insert_bsr(netlist, top, yosys_command, **options):
    plan = build_bsr_plan(
        netlist.module(top), clock_ports=frozenset({"clk"}), excluded_ports=frozenset({"rst_n"})
    )
    insert_bsr(netlist, top, plan, yosys_command=yosys_command, **options)


_INSERTIONS = {"sib": (_sib_netlist, _insert_sib), "bsr": (_bsr_netlist, _insert_bsr)}


def _inserted(insertion, fixtures_dir, yosys_command, **options) -> Module:
    make, insert = _INSERTIONS[insertion]
    netlist, top = make(fixtures_dir, yosys_command)
    insert(netlist, top, yosys_command, **options)
    return netlist.module(top)


def _drivers(top: Module, port: str) -> list[str]:
    """The cells with an output connected to ``port``'s bits."""
    bits = set(top.port_bits(port))
    return sorted(
        name
        for name, cell in top.data["cells"].items()
        for pin, connected in cell["connections"].items()
        if cell["port_directions"].get(pin) == "output" and bits & set(connected)
    )


def test_tristate_is_the_default_everywhere():
    assert TDO_DRIVES == ("tristate", "low", "enable_port")
    assert DEFAULT_TDO_DRIVE == "tristate"
    for function in (insert_sib_network, insert_bsr, insert_test_access):
        assert inspect.signature(function).parameters["tdo_drive"].default == "tristate", function


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_tristate_drives_the_pin_through_a_tribuf_enabled_by_tdo_oe(
    insertion, fixtures_dir, yosys_command
):
    top = _inserted(insertion, fixtures_dir, yosys_command)  # the default
    cells = top.data["cells"]
    tap = cells["warptap_tap_core"]["connections"]
    tribuf = cells["warptap_tdo_tribuf"]
    assert tribuf["type"] == "$tribuf"
    assert tribuf["connections"] == {"A": tap["tdo"], "EN": tap["tdo_oe"], "Y": top.port_bits(TDO)}
    assert _drivers(top, TDO) == ["warptap_tdo_tribuf"]  # tap_core no longer drives the pin
    assert TDO_OE not in top.data["ports"]


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_low_drives_the_pin_from_tap_core_and_leaves_tdo_oe_unconnected(
    insertion, fixtures_dir, yosys_command
):
    top = _inserted(insertion, fixtures_dir, yosys_command, tdo_drive="low")
    tap = top.data["cells"]["warptap_tap_core"]
    assert tap["connections"]["tdo"] == top.port_bits(TDO)
    assert "tdo_oe" not in tap["connections"] and "tdo_oe" not in tap["port_directions"]
    assert _drivers(top, TDO) == ["warptap_tap_core"]
    assert "warptap_tdo_tribuf" not in top.data["cells"]
    assert TDO_OE not in top.data["ports"]


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_enable_port_adds_a_tdo_oe_output_beside_a_driven_tdo(
    insertion, fixtures_dir, yosys_command
):
    top = _inserted(insertion, fixtures_dir, yosys_command, tdo_drive="enable_port")
    ports = top.data["ports"]
    assert list(ports)[-6:] == [TCK, TMS, TDI, TRST_N, TDO, TDO_OE]
    assert ports[TDO_OE]["direction"] == "output"
    tap = top.data["cells"]["warptap_tap_core"]["connections"]
    assert tap["tdo"] == top.port_bits(TDO)
    assert tap["tdo_oe"] == top.port_bits(TDO_OE)
    assert _drivers(top, TDO) == _drivers(top, TDO_OE) == ["warptap_tap_core"]
    assert "warptap_tdo_tribuf" not in top.data["cells"]


def test_insert_test_access_passes_it_through(fixtures_dir, yosys_command):
    sources = [fixtures_dir / "real_signal.v"]
    tristate, _graph, _root = insert_test_access(
        sources, "real_signal", _SPECS, yosys_command=yosys_command
    )
    assert "assign tdo = warptap_tdo_oe ? warptap_tdo_data : 1'bz;" in tristate
    enable_port, _graph, _root = insert_test_access(
        sources, "real_signal", _SPECS, yosys_command=yosys_command, tdo_drive="enable_port"
    )
    assert "output tdo_oe;" in enable_port and "1'bz" not in enable_port


@pytest.mark.parametrize("value", ["Tristate", "TRISTATE", "high", "z", "", None, 0, True, ("low",)])
def test_anything_else_is_refused_naming_the_three(value):
    with pytest.raises(ValueError, match="'tristate', 'low', 'enable_port'"):
        check_tdo_drive(value)


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_each_insertion_refuses_it_before_touching_the_netlist(
    insertion, fixtures_dir, yosys_command
):
    make, insert = _INSERTIONS[insertion]
    netlist, top = make(fixtures_dir, yosys_command)
    before = copy.deepcopy(netlist.to_json())
    with pytest.raises(ValueError, match="tdo_drive"):
        insert(netlist, top, yosys_command, tdo_drive="tri-state")
    assert netlist.to_json() == before


def test_insert_test_access_refuses_it_before_ingesting(fixtures_dir, yosys_command):
    """Before anything is read: the source doesn't exist, so ingesting would fail otherwise."""
    with pytest.raises(ValueError, match="tdo_drive"):
        insert_test_access(
            [fixtures_dir / "no_such_design.v"], "real_signal", _SPECS,
            yosys_command=yosys_command, tdo_drive="pad",
        )
