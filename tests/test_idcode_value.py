"""A design's own IDCODE: ``idcode_value`` on ``insert_test_access``/``insert_sib_network``,
``to_bsdl``, ``TapModel`` and ``TapConfig`` -- validation at every entry point, the value reaching
each consumer, and the default path left exactly as it was. The real-RTL side is
``test_idcode_value_cross_sim.py``.
"""

from __future__ import annotations

import pytest
from bsdl_reader import parse_bsdl

from warptap import sib_insert
from warptap.bsdl_emit import BsdlEmitError, to_bsdl
from warptap.netlist import Netlist
from warptap.pipeline import insert_test_access
from warptap.sib_insert import SibInsertError, insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_fsm import TapState
from warptap.tap_integrity import TapConfig, TapIntegrityError
from warptap.tap_ir import GotoState, ShiftDR
from warptap.tap_ir_play import play
from warptap.tap_model import IDCODE_VALUE, TapModel, TapModelError, idcode_value_error
from warptap.yosys_io import ingest

_CUSTOM = 0x5CA1AB1F
_BAD = [
    (0, "bit 0"),
    (0x1A5A5002, "bit 0"),
    (1 << 32, "32-bit"),
    (-1, "32-bit"),
    (True, "must be an int"),
    (float(IDCODE_VALUE), "must be an int"),
    ("0x5CA1AB1F", "must be an int"),
]


@pytest.mark.parametrize("value", [IDCODE_VALUE, _CUSTOM, 1, 0xFFFFFFFF])
def test_a_32_bit_value_with_bit_0_set_is_accepted(value):
    assert idcode_value_error(value) is None


@pytest.mark.parametrize(("value", "reason"), _BAD)
def test_everything_else_is_rejected_with_a_reason(value, reason):
    assert reason in idcode_value_error(value)


@pytest.mark.parametrize(("value", "reason"), _BAD)
def test_tap_model_rejects_it(value, reason):
    with pytest.raises(TapModelError, match=reason):
        TapModel(idcode_value=value)


def test_tap_model_without_an_idcode_register_ignores_the_value():
    assert not TapModel(has_idcode=False, idcode_value=0).has_idcode


@pytest.mark.parametrize(("value", "reason"), _BAD)
def test_tap_config_rejects_it(value, reason):
    with pytest.raises(TapIntegrityError, match=reason):
        TapConfig(idcode_value=value)


@pytest.mark.parametrize(("value", "reason"), _BAD)
def test_to_bsdl_rejects_it(value, reason):
    with pytest.raises(BsdlEmitError, match=reason):
        to_bsdl("chip", tck_max_freq_hz=10e6, idcode_value=value)


@pytest.mark.parametrize(("value", "reason"), _BAD)
def test_insert_sib_network_rejects_it_before_touching_the_netlist(value, reason):
    with pytest.raises(SibInsertError, match=reason):
        insert_sib_network(None, "top", None, idcode_value=value)


def test_insert_test_access_rejects_it_before_ingesting_anything():
    with pytest.raises(SibInsertError, match="bit 0"):
        insert_test_access(["/no/such/file.v"], "top", [], idcode_value=2)


def _idcode_read_back(model: TapModel) -> int:
    for _ in range(5):
        model.tick(1)
    model.tick(0)  # Run-Test/Idle, with IDCODE selected by the reset
    (observed,) = play(model, [GotoState(TapState.SHIFT_DR), ShiftDR(32, 0)])
    return observed


def test_tap_model_shifts_the_configured_value_out_under_idcode():
    assert _idcode_read_back(TapModel(idcode_value=_CUSTOM)) == _CUSTOM


def test_tap_config_builds_a_model_with_its_value():
    assert _idcode_read_back(TapConfig(idcode_value=_CUSTOM).model()) == _CUSTOM


def test_to_bsdl_declares_a_custom_value_and_drops_the_placeholder_wording():
    text = to_bsdl("chip", tck_max_freq_hz=10e6, idcode_value=_CUSTOM)
    bsdl = parse_bsdl(text)
    assert int(bsdl.idcode_register, 2) == _CUSTOM
    assert "IDCODE value is a placeholder" not in bsdl.design_warning
    assert "(placeholder)" not in text
    # The pin map is still a placeholder and still says so.
    assert "PHYSICAL_PIN_MAP UNPACKAGED is a placeholder" in bsdl.design_warning


def test_to_bsdl_default_value_still_says_placeholder():
    text = to_bsdl("chip", tck_max_freq_hz=10e6)
    assert "IDCODE value is a placeholder" in parse_bsdl(text).design_warning
    assert "(placeholder)" in text
    assert to_bsdl("chip", tck_max_freq_hz=10e6, idcode_value=IDCODE_VALUE) == text


def _trivial_netlist(fixtures_dir, yosys_command):
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    graph, _ = build_sib_plan([InstrumentSpec("sensor", width=1, capture_value=0)], top_name="trivial")
    return Netlist.from_json(raw), graph


def test_the_default_value_keeps_the_plain_tap_core_import(fixtures_dir, yosys_command, monkeypatch):
    """Re-importing tap_core through ``chparam`` changes Yosys's output even at the default
    value, so the default must never take that path."""

    def forbidden(*args, **kwargs):
        raise AssertionError("ingest_with_params called for the default IDCODE value")

    monkeypatch.setattr(sib_insert, "ingest_with_params", forbidden)
    netlist, graph = _trivial_netlist(fixtures_dir, yosys_command)
    insert_sib_network(netlist, "trivial", graph, yosys_command=yosys_command)
    assert "tap_core" in netlist.to_json()["modules"]


def test_a_custom_value_is_baked_into_tap_core_via_chparam(fixtures_dir, yosys_command, monkeypatch):
    calls = []
    real = sib_insert.ingest_with_params

    def spy(files, top, chparams, **kwargs):
        calls.append((top, dict(chparams)))
        return real(files, top, chparams, **kwargs)

    monkeypatch.setattr(sib_insert, "ingest_with_params", spy)
    netlist, graph = _trivial_netlist(fixtures_dir, yosys_command)
    insert_sib_network(netlist, "trivial", graph, idcode_value=_CUSTOM, yosys_command=yosys_command)
    assert calls == [("tap_core", {"IDCODE_VALUE": _CUSTOM})]
    assert "tap_core" in netlist.to_json()["modules"]
