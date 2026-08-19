"""Structural cross-check of warptap.tap_fsm.TRANSITIONS against two independently-sourced
public TAP implementations (implementation_plan.md §8: "TAP FSM table cross-check against at
least two independent references... off-by-one edge transitions are the realistic bug class
here"). Every IEEE-1149.1-conformant TAP has the *identical* 16-state/32-edge FSM regardless
of IR width or instruction set, so any mismatch here is a transcription bug — in either
warptap's own table or the reference being compared against.

Both reference tables below are transcribed directly from the actual project sources (fetched
2026-08-19), not derived from warptap.tap_fsm.TRANSITIONS — the whole point is that they were
written down independently.
"""

from __future__ import annotations

from warptap.tap_fsm import TapState, TRANSITIONS

# Transcribed from freecores/jtag's tap/rtl/verilog/tap_top.v
# (https://github.com/freecores/jtag/blob/master/tap/rtl/verilog/tap_top.v). That file has no
# single next-state table — instead, one `always @(posedge tck_pad_i or posedge trst_pad_i)`
# block per state, each of the shape `if (tms_pad_i & (A | B | ...)) target <= 1'b1;` (or
# `~tms_pad_i & (...)` for the tms=0 case). This dict is that structure transcribed as-is:
# target_state -> [(tms value required, source state that asserts it), ...]. The actual
# per-state (next0, next1) table is *derived* from this below, not hand-computed here, so this
# test exercises a genuinely independent representation, not just a renamed copy of
# warptap.tap_fsm.TRANSITIONS.
_FREECORES_ASSERTED_BY: dict[str, list[tuple[int, str]]] = {
    "TEST_LOGIC_RESET": [(1, "TEST_LOGIC_RESET"), (1, "SELECT_IR_SCAN")],
    "RUN_TEST_IDLE": [
        (0, "TEST_LOGIC_RESET"),
        (0, "RUN_TEST_IDLE"),
        (0, "UPDATE_DR"),
        (0, "UPDATE_IR"),
    ],
    "SELECT_DR_SCAN": [(1, "RUN_TEST_IDLE"), (1, "UPDATE_DR"), (1, "UPDATE_IR")],
    "CAPTURE_DR": [(0, "SELECT_DR_SCAN")],
    "SHIFT_DR": [(0, "CAPTURE_DR"), (0, "SHIFT_DR"), (0, "EXIT2_DR")],
    "EXIT1_DR": [(1, "CAPTURE_DR"), (1, "SHIFT_DR")],
    "PAUSE_DR": [(0, "EXIT1_DR"), (0, "PAUSE_DR")],
    "EXIT2_DR": [(1, "PAUSE_DR")],
    "UPDATE_DR": [(1, "EXIT1_DR"), (1, "EXIT2_DR")],
    "SELECT_IR_SCAN": [(1, "SELECT_DR_SCAN")],
    "CAPTURE_IR": [(0, "SELECT_IR_SCAN")],
    "SHIFT_IR": [(0, "CAPTURE_IR"), (0, "SHIFT_IR"), (0, "EXIT2_IR")],
    "EXIT1_IR": [(1, "CAPTURE_IR"), (1, "SHIFT_IR")],
    "PAUSE_IR": [(0, "EXIT1_IR"), (0, "PAUSE_IR")],
    "EXIT2_IR": [(1, "PAUSE_IR")],
    "UPDATE_IR": [(1, "EXIT1_IR"), (1, "EXIT2_IR")],
}


def _derive_transitions_from_assertions(
    asserted_by: dict[str, list[tuple[int, str]]],
) -> dict[str, tuple[str, str]]:
    """Invert a target->[(tms, source), ...] assertion map into source->(next0, next1) —
    i.e. actually compute the transition table implied by the fetched boolean logic,
    rather than trusting a hand-derivation of it."""
    by_source: dict[str, dict[int, str]] = {name: {} for name in asserted_by}
    for target, sources in asserted_by.items():
        for tms_value, source in sources:
            by_source[source][tms_value] = target
    return {name: (edges[0], edges[1]) for name, edges in by_source.items()}


FREECORES_TRANSITIONS = _derive_transitions_from_assertions(_FREECORES_ASSERTED_BY)

# Transcribed verbatim from KadiChandu/JTAG-TAP-Controller's tap_controller_design.v
# (https://github.com/KadiChandu/JTAG-TAP-Controller, fetched 2026-08-19) — a plain
# `case (state) STATE: next_state = tms ? A : B; ...` block, so this is a direct
# state -> (next if tms=0, next if tms=1) transcription of that case statement.
KADICHANDU_TRANSITIONS: dict[str, tuple[str, str]] = {
    "TEST_LOGIC_RESET": ("RUN_TEST_IDLE", "TEST_LOGIC_RESET"),
    "RUN_TEST_IDLE": ("RUN_TEST_IDLE", "SELECT_DR_SCAN"),
    "SELECT_DR_SCAN": ("CAPTURE_DR", "SELECT_IR_SCAN"),
    "CAPTURE_DR": ("SHIFT_DR", "EXIT1_DR"),
    "SHIFT_DR": ("SHIFT_DR", "EXIT1_DR"),
    "EXIT1_DR": ("PAUSE_DR", "UPDATE_DR"),
    "PAUSE_DR": ("PAUSE_DR", "EXIT2_DR"),
    "EXIT2_DR": ("SHIFT_DR", "UPDATE_DR"),
    "UPDATE_DR": ("RUN_TEST_IDLE", "SELECT_DR_SCAN"),
    "SELECT_IR_SCAN": ("CAPTURE_IR", "TEST_LOGIC_RESET"),
    "CAPTURE_IR": ("SHIFT_IR", "EXIT1_IR"),
    "SHIFT_IR": ("SHIFT_IR", "EXIT1_IR"),
    "EXIT1_IR": ("PAUSE_IR", "UPDATE_IR"),
    "PAUSE_IR": ("PAUSE_IR", "EXIT2_IR"),
    "EXIT2_IR": ("SHIFT_IR", "UPDATE_IR"),
    "UPDATE_IR": ("RUN_TEST_IDLE", "SELECT_DR_SCAN"),
}


def _warptap_transitions_by_name() -> dict[str, tuple[str, str]]:
    return {
        state.name: (TRANSITIONS[state][0].name, TRANSITIONS[state][1].name)
        for state in TapState
    }


def test_reference_tables_cover_all_16_states():
    """Sanity check on the references themselves before trusting a comparison against
    them: transcription mistakes that drop a state would otherwise show up as a
    missing-key error deep inside the comparison tests, not as a clear failure here."""
    assert set(FREECORES_TRANSITIONS) == {s.name for s in TapState}
    assert set(KADICHANDU_TRANSITIONS) == {s.name for s in TapState}


def test_transitions_match_freecores_tap_top_v():
    assert _warptap_transitions_by_name() == FREECORES_TRANSITIONS


def test_transitions_match_kadichandu_tap_controller():
    assert _warptap_transitions_by_name() == KADICHANDU_TRANSITIONS


def test_the_two_independent_references_agree_with_each_other():
    """Not strictly necessary (both already being checked against warptap's own table
    implies this transitively), but a direct check makes a disagreement between the two
    *references* immediately diagnosable instead of looking like a warptap bug."""
    assert FREECORES_TRANSITIONS == KADICHANDU_TRANSITIONS


def test_exit2_states_return_to_shift_not_capture_on_tms_0():
    """The specific bug class implementation_plan.md §7 names explicitly: EXIT2_DR/IR
    with TMS=0 must return to SHIFT_DR/SHIFT_IR, not back to CAPTURE_DR/CAPTURE_IR."""
    assert TRANSITIONS[TapState.EXIT2_DR][0] is TapState.SHIFT_DR
    assert TRANSITIONS[TapState.EXIT2_IR][0] is TapState.SHIFT_IR
