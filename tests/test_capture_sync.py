"""capture_sync: a READ instrument whose capture goes through a two-TCK-flop synchronizer
(rtl/bc1_shift_only_sync.v), for a signal from another clock domain. Off by default, and
then nothing about the inserted network changes."""

from __future__ import annotations

import pytest
from test_sib_insert_write_instrument_cross_sim import (
    _pdl_program,
    _select_extest_ops,
    run_on_python,
    run_on_rtl,
)

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.sib_insert import SibInsertError, insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.yosys_io import ingest


def _specs(*, sync: bool) -> list[InstrumentSpec]:
    return [
        InstrumentSpec(
            "ctrl_write", width=1, capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("ctrl_in", 0),),
        ),
        InstrumentSpec(
            "status_read", width=1, capture_value=1,
            direction=InstrumentDirection.READ,
            signal_bits=(SignalBinding("status_out", 0),),
            capture_sync=sync,
        ),
    ]


def _inserted(fixtures_dir, yosys_command, specs):
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, root = build_sib_plan(specs, top_name="real_signal")
    insert_sib_network(netlist, "real_signal", graph, yosys_command=yosys_command)
    return netlist, graph, root


def _cell_types(netlist: Netlist) -> dict[str, str]:
    cells = netlist.to_json()["modules"]["real_signal"]["cells"]
    return {name: cell["type"] for name, cell in cells.items()}


def test_off_by_default() -> None:
    assert InstrumentSpec("x", width=1, capture_value=0).capture_sync is False


def test_only_a_synchronized_read_uses_the_synchronizing_cell(fixtures_dir, yosys_command):
    netlist, _, _ = _inserted(fixtures_dir, yosys_command, _specs(sync=True))
    types = _cell_types(netlist)
    assert types["warptap_sib_status_read_inst_0"] == "bc1_shift_only_sync"
    assert types["warptap_sib_ctrl_write_inst_0"] == "instrument_write"
    assert "bc1_shift_only_sync" in netlist.to_json()["modules"]


def test_without_it_the_network_is_unchanged(fixtures_dir, yosys_command):
    netlist, _, _ = _inserted(fixtures_dir, yosys_command, _specs(sync=False))
    assert _cell_types(netlist)["warptap_sib_status_read_inst_0"] == "bc1_shift_only"
    assert "bc1_shift_only_sync" not in netlist.to_json()["modules"]


def test_a_write_instrument_cannot_ask_for_it(fixtures_dir, yosys_command):
    specs = _specs(sync=False)
    specs[0] = specs[0]._replace(capture_sync=True)
    with pytest.raises(SibInsertError, match="WRITE instrument with capture_sync"):
        _inserted(fixtures_dir, yosys_command, specs)


def test_a_read_through_the_synchronizer_matches_the_model(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
):
    """Write ctrl_write=1, wait, read status_read: the synchronizer has had more than two
    TCK edges by the capture, so real RTL reads exactly what the model expects."""
    netlist, graph, root = _inserted(fixtures_dir, yosys_command, _specs(sync=True))
    ir_ops = _select_extest_ops() + _pdl_program(graph, root)
    python_observed, _ = run_on_python(graph, ir_ops)
    rtl_observed = run_on_rtl(
        fixtures_dir, yosys_command, iverilog_command, vvp_command, netlist, ir_ops
    )
    assert rtl_observed == python_observed
