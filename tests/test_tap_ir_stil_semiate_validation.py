"""Live STIL grammar/semantic validation against a real, independent tool (implementation_plan.md
§7 Stage 13, §8's own testing strategy: "validated by feeding output through an existing
consumer... not a self-written parser"). Runs warptap's own STIL emitter output through the
real, pip-installable ``Semi-ATE-STIL`` parser -- proving grammar AND semantic correctness
against code warptap didn't write. This is what actually caught several real, previously-
undocumented requirements this session (see ``tap_ir_stil.py``'s own module docstring): the
mandatory ``Waveforms{}`` wrapper, the ``SignalGroups``-before-``Timing``/``PatternBurst``-and-
``PatternExec``-before-``Pattern`` block ordering, and every signal needing a WFC defined in
every ``WaveformTable`` a pattern ever selects.

Skips cleanly (via the ``semiate_stil_parser`` fixture) if ``Semi-ATE-STIL``/``lark`` aren't
importable, matching every other external-tool fixture's discipline in this project.

Real usage note, confirmed by reading ``STILParser.parse_semantic()``'s own source directly:
its return value is always ``None`` regardless of outcome -- real success is
``parser.is_parsing_done is True`` with ``parser.err_msg == ""``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.pdl_interpreter import PDLInterpreter
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import PulsePin
from warptap.tap_ir_stil import to_stil


def _assert_valid(stil_text: str, semiate_stil_parser) -> None:
    with tempfile.TemporaryDirectory(prefix="warptap-stil-") as tmpdir:
        path = Path(tmpdir) / "warptap.stil"
        path.write_text(stil_text, encoding="utf-8")
        parser = semiate_stil_parser(str(path))
        syntax_ok = parser.parse_syntax()
        assert syntax_ok, f"syntax error: {parser.err_msg}"
        parser.parse_semantic()
        assert parser.is_parsing_done, f"semantic error: {parser.err_msg}"
        assert parser.err_msg == ""


def test_empty_ops_stil_is_syntax_and_semantic_valid(semiate_stil_parser):
    _assert_valid(to_stil([]), semiate_stil_parser)


def test_pure_jtag_shift_sequence_is_valid(semiate_stil_parser):
    specs = [InstrumentSpec("sensor_a", width=3, capture_value=0b101)]
    graph, root = build_sib_plan(specs, top_name="chip")
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("sensor_a")
    pdl.iRead(0b101)
    pdl.iApply()
    _assert_valid(to_stil(pdl.program), semiate_stil_parser)


def test_load_pulse_unload_sequence_with_pulsepin_and_hold_pins_is_valid(semiate_stil_parser):
    """The exact shape Stage 14 (faultflow pattern retargeting) produces -- a load shift, a
    PulsePin with real hold_pins, and an unload/compare shift, each under its own
    WaveformTable -- the real mechanism this whole stage exists to prove out."""
    specs = [
        InstrumentSpec(
            "ctrl_write",
            width=1,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=(SignalBinding("bist_start"),),
        )
    ]
    graph, root = build_sib_plan(specs, top_name="chip")
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("ctrl_write")
    pdl.iWrite(1)
    pdl.iApply()
    pdl.program.append(PulsePin("sysclk", 3, hold_pins=(("pi_a", 1), ("pi_b", 0))))
    pdl.iTarget("ctrl_write")
    pdl.iRead(1)
    pdl.iApply()

    text = to_stil(pdl.program, jtag_period="100ns", pulse_periods={"sysclk": "20ns"})
    _assert_valid(text, semiate_stil_parser)


def test_two_distinct_pulse_ports_in_one_sequence_is_valid(semiate_stil_parser):
    """Two different PulsePin targets (two clock/PI domains) in the same ops list -- confirms
    the one-WaveformTable-per-distinct-port design (module docstring) holds for more than
    one pulse target, not just the single-target case every other test here exercises."""
    ops = [
        PulsePin("sysclk_a", 2, hold_pins=(("pi_x", 1),)),
        PulsePin("sysclk_b", 1, hold_pins=(("pi_y", 0),)),
    ]
    text = to_stil(ops, pulse_periods={"sysclk_a": "20ns", "sysclk_b": "30ns"})
    _assert_valid(text, semiate_stil_parser)


def test_fractional_edge_times_are_valid(semiate_stil_parser):
    """A 50ns period puts the strobe at 12.5ns and the fall at 37.5ns; a 0.1us pulse period
    keeps its own unit (0.05us, 0.075us)."""
    text = to_stil([PulsePin("sysclk", 1)], jtag_period="50ns", pulse_periods={"sysclk": "0.1us"})
    assert "'12.5ns'" in text and "'0.075us'" in text
    _assert_valid(text, semiate_stil_parser)


def test_a_pulse_holding_the_pins_it_does_not_list_is_valid(semiate_stil_parser):
    ops = [PulsePin("clk", 1, hold_pins=(("rst_n", 1),)), PulsePin("clk", 1)]
    text = to_stil(ops, pulse_periods={"clk": "10ns"})
    assert "rst_n=P;" in text.split("rst_n=1;")[1]
    _assert_valid(text, semiate_stil_parser)


def test_declared_inputs_and_outputs_are_valid(semiate_stil_parser):
    ops = [PulsePin("sysclk", 2, hold_pins=(("pi_a", 1),)), PulsePin("sysclk", 1)]
    text = to_stil(
        ops, pulse_periods={"sysclk": "20ns"},
        inputs={"rst_n": 1, "sysclk": 0}, outputs=["status_out", "done"],
    )
    _assert_valid(text, semiate_stil_parser)
