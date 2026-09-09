"""Pure-Python tests for SibNetworkRegister's N-arm ScanMux generalization (multi-arm ScanMux
plan Phase 3). Drives ``capture()``/``shift()``/``update()`` directly (no TapModel, no RTL --
that's Phase 5's job, cross-simulating a real inserted network against this same oracle) using
``sib_layout.compose_bits``'s own bit construction, exactly as ``PDLInterpreter.iApply`` will
once Phase 4 wires it up. Mirrors ``test_sib_layout.py``'s own ``_mux_graph()`` shape and
values where convenient, so the two files' numbers cross-check each other.
"""

from __future__ import annotations

from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SibNode,
)
from warptap.sib_layout import compose_bits, layout_bit_length
from warptap.sib_model import SibNetworkRegister


def _instrument(
    name: str, width: int, *, capture_value: int = 0,
    direction: InstrumentDirection = InstrumentDirection.READ,
) -> InstrumentNode:
    return InstrumentNode(name=name, width=width, capture_value=capture_value, direction=direction)


def _shift_round(reg: SibNetworkRegister, bits_position_order: list[int]) -> list[int]:
    """One Capture-Shift-Update round. ``bits_position_order`` and the returned TDO stream are
    both in ``compose_bits``'s own TDI-nearest-first convention (position order) -- converted
    to/from the chronological feed order ``shift()`` itself uses via ``reversed()``, exactly
    the relationship ``tap_ir.py`` documents between the two."""
    reg.capture()
    tdo_chronological = [reg.shift(b) for b in reversed(bits_position_order)]
    reg.update()
    return list(reversed(tdo_chronological))


def _write_mux_graph() -> PhysicalGraph:
    mux = ScanMuxNode(
        "mux_a", select_width=2,
        arms=(
            ScanArm(values=(1,), instrument=_instrument("arm1", 3, direction=InstrumentDirection.WRITE)),
            ScanArm(values=(2,), instrument=_instrument("arm2", 2, direction=InstrumentDirection.WRITE)),
        ),
    )
    return PhysicalGraph(chain=(mux,))


def test_bypassing_mux_shift_length_matches_select_width():
    graph = _write_mux_graph()
    reg = SibNetworkRegister(graph)
    reg.reset()
    tdo = _shift_round(reg, [0, 0])
    assert len(tdo) == 2 == layout_bit_length(graph, {})


def test_write_then_read_round_trip_through_matched_arm():
    """The core write/read property: open arm1, deliver a real payload to its WRITE
    instrument, then read it back on a later round -- exactly the round-trip
    instrument_write.v's own self-capture (`shift_ff <= po`) exists to support."""
    graph = _write_mux_graph()
    reg = SibNetworkRegister(graph)
    reg.reset()

    _shift_round(reg, compose_bits(graph, {}, {"mux_a": 1}))  # bypass -> arm1
    _shift_round(
        reg, compose_bits(graph, {"mux_a": 1}, {"mux_a": 1}, target_sib="mux_a", payload_value=0b101)
    )  # deliver payload to arm1 (now physically open)

    tdo = _shift_round(reg, compose_bits(graph, {"mux_a": 1}, {"mux_a": 1}))
    assert tdo[:3] == [1, 0, 1]  # arm1's own committed content, LSB first, TDI-nearest
    assert tdo[3:] == [0, 1]  # mux_a's own select field, still asserting arm1 (value 1 = 0b01,
    # MSB first: bit1=0, bit0=1)


def test_switching_arms_directly_abandons_old_arms_write_and_reaches_new_one():
    """The single most novel case (RTL spike Phase 0, sib_layout Phase 2, now the oracle
    too): arm1 is open with a committed WRITE value; a round switches DIRECTLY to arm2 --
    arm1's value must NOT change (that round only ever feeds it don't-care 0 content, and
    instrument_write.v's own update-latch gating must reject an abandoning edge's garbage).

    Delivering a payload to arm2 needs its OWN, separate, LATER round -- arm2's content isn't
    reachable in the SAME round that switches to it, exactly the same "closed/unmatched
    content isn't physically part of the live chain yet" rule that makes opening a nested SIB
    need 2 rounds. An earlier version of this test tried to combine the switch and the
    delivery in one round (target_sib="mux_a" while switching) and silently delivered the
    payload into arm1's own (still-current-this-round) content instead -- caught by this file's
    own tests, not assumed correct."""
    graph = _write_mux_graph()
    reg = SibNetworkRegister(graph)
    reg.reset()

    _shift_round(reg, compose_bits(graph, {}, {"mux_a": 1}))  # bypass -> arm1
    _shift_round(
        reg, compose_bits(graph, {"mux_a": 1}, {"mux_a": 1}, target_sib="mux_a", payload_value=0b110)
    )  # arm1 <- 0b110 (deliberately NOT what we'll check for arm2, to catch cross-talk)

    # Switch arm1 -> arm2 directly (no payload yet -- arm1's own 3 content bits are don't-care
    # here, per compose_bits' documented 0-fill).
    _shift_round(reg, compose_bits(graph, {"mux_a": 1}, {"mux_a": 2}, target_sib=None, payload_value=0))

    # NOW deliver arm2's own payload, on its own separate round.
    _shift_round(
        reg, compose_bits(graph, {"mux_a": 2}, {"mux_a": 2}, target_sib="mux_a", payload_value=0b01)
    )

    tdo = _shift_round(reg, compose_bits(graph, {"mux_a": 2}, {"mux_a": 2}))
    assert tdo[:2] == [1, 0]  # arm2's own committed content (0b01, LSB first)
    assert tdo[2:] == [1, 0]  # mux_a's select field, now asserting arm2 (value 2 = 0b10, MSB
    # first: bit1=1, bit0=0)

    # And arm1's own committed value must be UNCHANGED by the abandoning switch -- switch back
    # and read.
    _shift_round(reg, compose_bits(graph, {"mux_a": 2}, {"mux_a": 1}))
    tdo_arm1 = _shift_round(reg, compose_bits(graph, {"mux_a": 1}, {"mux_a": 1}))
    assert tdo_arm1[:3] == [0, 1, 1]  # still 0b110, untouched by the switch-away round


def test_scan_mux_with_a_nested_arm_recurses_like_a_hierarchy_sib():
    """A mux arm can itself gate a nested SIB -- reaching it needs its own separate round,
    exactly mirroring test_sib_layout.py's own finding for this same shape."""
    inner = SibNode("sib_inner", _instrument("deep", 2, capture_value=0b10))
    mux = ScanMuxNode(
        "mux_a", select_width=1,
        arms=(ScanArm(values=(0,), nested=(inner,)), ScanArm(values=(1,), instrument=_instrument("b", 1))),
    )
    graph = PhysicalGraph(chain=(mux,))
    reg = SibNetworkRegister(graph)
    reg.reset()

    _shift_round(reg, compose_bits(graph, {}, {"mux_a": 0}))  # bypass -> arm0 (mux_a matched)
    _shift_round(reg, compose_bits(graph, {"mux_a": 0}, {"mux_a": 0, "sib_inner": 1}))  # open sib_inner too

    tdo = _shift_round(reg, [0] * layout_bit_length(graph, {"mux_a": 0, "sib_inner": 1}))
    assert tdo[:2] == [0, 1]  # sib_inner's own capture_value 0b10, LSB first
