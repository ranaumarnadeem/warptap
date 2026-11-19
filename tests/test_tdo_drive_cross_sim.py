"""Cross-simulation of the inserted TAP's ``tdo`` pin, for each ``tdo_drive``
(:data:`warptap.tap_model.TDO_DRIVES`) and both insertions: a SIB network in ``real_signal``
(:func:`~warptap.sib_insert.insert_sib_network`) and a boundary-scan register in ``trivial``
(:func:`~warptap.bsr_insert.insert_bsr`). IEEE 1149.1 has TDO carry a bit only while the TAP is
resident in Shift-IR or Shift-DR, be high impedance in every other state, and change only on the
falling edge of TCK.

Every run gets one stimulus: a TRST reset, EXTEST selected (the network or the boundary register
on TDO), then a seeded random TMS/TDI walk that visits all 16 states. The testbench samples the
pin, and the enable that goes with it, three times per TCK cycle: before the rising edge, after
it, and after the falling edge.

- ``"tristate"`` (the default): the pin reads ``z`` in every state but Shift-IR/Shift-DR and a bit
  in those, ``tdo_oe`` (the ``$tribuf``'s enable) is high exactly there, and neither moves at a
  rising edge or between a falling edge and the next cycle: the pin is released and driven again
  only at falling edges.
- ``"low"``, warptap 0.0.6's wiring: never ``z``, 0 outside the shift states.
- ``"enable_port"``: ``tdo`` as with ``"low"``, and the ``tdo_oe`` output high exactly in the shift
  states, with the same timing.
- All three shift out the same bits: the mode changes nothing but the release.

Controls, so neither check passes vacuously: the tri-state check fails on the ``"low"`` pin (0.0.6's
behavior), and on a pin whose enable is a decode of the TAP state, which reads right before every
rising edge but changes just after it.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import pytest
from tdo_trace import tdo_bit

from warptap.bsr_insert import insert_bsr
from warptap.bsr_plan import build_bsr_plan
from warptap.netlist import Module, Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftIR
from warptap.tap_ir_play import to_cycles
from warptap.tap_model import OPCODE_EXTEST, TDO_DRIVES, drives_tdo
from warptap.tap_ports import TCK, TDI, TDO_OE, TMS, TRST_N
from warptap.yosys_io import ingest, write_verilog_from_json

_IR_WIDTH = 4
_WALK_TICKS = 600


class Sample(NamedTuple):
    state: TapState
    tdo: Tuple[str, str, str]  # before the rising edge, after it, after the falling edge
    oe: Tuple[str, str, str]  # the same for the enable ("x" where there is none)


def _stimulus() -> List[Tuple[int, int, int]]:
    """``(tms, tdi, trst_n)`` rows: TRST, Run-Test/Idle, EXTEST, then the random walk."""
    rows = [(0, 0, 0), (0, 0, 1)]
    select_extest = [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(_IR_WIDTH, OPCODE_EXTEST),
        GotoState(TapState.RUN_TEST_IDLE),
    ]
    rows += [(tms, tdi, 1) for tms, tdi in to_cycles(select_extest)]
    rng = random.Random(1149)
    rows += [(rng.randint(0, 1), rng.randint(0, 1), 1) for _ in range(_WALK_TICKS)]
    return rows


def _sib_inserted(fixtures_dir, yosys_command, tdo_drive) -> Tuple[Netlist, str]:
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan([InstrumentSpec("stub", width=3, capture_value=0b101)])
    insert_sib_network(
        netlist, "real_signal", graph, yosys_command=yosys_command, tdo_drive=tdo_drive
    )
    return netlist, "real_signal"


def _bsr_inserted(fixtures_dir, yosys_command, tdo_drive) -> Tuple[Netlist, str]:
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    plan = build_bsr_plan(
        netlist.module("trivial"),
        clock_ports=frozenset({"clk"}),
        excluded_ports=frozenset({"rst_n"}),
    )
    insert_bsr(netlist, "trivial", plan, yosys_command=yosys_command, tdo_drive=tdo_drive)
    return netlist, "trivial"


_INSERTIONS = {"sib": _sib_inserted, "bsr": _bsr_inserted}

# The enable as the testbench can see it: tap_core's tdo_oe, a wire of the design with "tristate",
# a port with "enable_port", unconnected with "low".
_ENABLE = {"tristate": "dut.warptap_tdo_oe", "enable_port": TDO_OE, "low": "1'bx"}


def _testbench(top: str, ports: Dict[str, dict], enable: str) -> str:
    """Drive ``top``'s JTAG pins from "stimulus.txt" rows "<tms> <tdi> <trst_n>", hold every other
    input at 0, and print ``TRACE,<state>,<tdo x3>,<enable x3>`` per row (``%b``, so a released
    pin prints ``z``)."""
    jtag_inputs = {TCK, TMS, TDI, TRST_N}
    lines = ["`timescale 1ns/1ps", "module tb_tdo_drive;"]
    lines += [f"    reg {name} = 0;" for name in (TCK, TMS, TDI)] + [f"    reg {TRST_N} = 1;"]
    for name, port in ports.items():
        width = len(port["bits"])
        vector = f"[{width - 1}:0] " if width > 1 else ""
        if port["direction"] != "input":
            lines.append(f"    wire {vector}{name};")
        elif name not in jtag_inputs:
            lines.append(f"    reg {vector}{name} = 0;")
    lines.append(f"    {top} dut ({', '.join(f'.{name}({name})' for name in ports)});")
    lines += [
        "    integer fd, code, tms_in, tdi_in, trst_n_in;",
        "    reg [3:0] state_pre;",
        "    reg tdo_pre, tdo_rise, tdo_fall, oe_pre, oe_rise, oe_fall;",
        "    initial begin",
        '        fd = $fopen("stimulus.txt", "r");',
        '        while ($fscanf(fd, "%d %d %d", tms_in, tdi_in, trst_n_in) == 3) begin',
        "            tms = tms_in[0]; tdi = tdi_in[0]; trst_n = trst_n_in[0];",
        "            #1;",
        f"            state_pre = dut.warptap_tap_state; tdo_pre = tdo; oe_pre = {enable};",
        "            tck = 1;",
        "            #1;",
        f"            tdo_rise = tdo; oe_rise = {enable};",
        "            tck = 0;",
        "            #1;",
        f"            tdo_fall = tdo; oe_fall = {enable};",
        '            $display("TRACE,%0d,%b,%b,%b,%b,%b,%b", state_pre, tdo_pre, tdo_rise,'
        " tdo_fall, oe_pre, oe_rise, oe_fall);",
        "        end",
        "        $fclose(fd);",
        "        $finish;",
        "    end",
        "endmodule",
    ]
    return "\n".join(lines) + "\n"


def _enable_from_the_state_decode(top_mod: Module) -> None:
    """Control: rewire the ``$tribuf``'s enable to a combinational decode of the TAP state
    (Shift-IR or Shift-DR). Right in every cycle once it settles, but it changes just after the
    rising edge that changes the state, not at the falling edge after it."""
    state_bits = top_mod.data["netnames"]["warptap_tap_state"]["bits"]
    hits = []
    for state in (TapState.SHIFT_IR, TapState.SHIFT_DR):
        hit = top_mod.new_wire(1)
        top_mod.add_cell(
            f"control_in_{state.name.lower()}",
            "$eq",
            parameters={"A_SIGNED": 0, "B_SIGNED": 0, "A_WIDTH": 4, "B_WIDTH": 4, "Y_WIDTH": 1},
            port_directions={"A": "input", "B": "input", "Y": "output"},
            connections={
                "A": state_bits, "B": [str((state.value >> i) & 1) for i in range(4)], "Y": hit,
            },
        )
        hits.append(hit)
    enable = top_mod.new_wire(1)
    top_mod.add_cell(
        "control_shifting",
        "$or",
        parameters={"A_SIGNED": 0, "B_SIGNED": 0, "A_WIDTH": 1, "B_WIDTH": 1, "Y_WIDTH": 1},
        port_directions={"A": "input", "B": "input", "Y": "output"},
        connections={"A": hits[0], "B": hits[1], "Y": enable},
    )
    top_mod.connect("warptap_tdo_tribuf", "EN", enable)


@pytest.fixture(scope="module")
def run(
    fixtures_dir, yosys_command, iverilog_command, vvp_command
) -> Callable[..., List[Sample]]:
    cache: Dict[tuple, List[Sample]] = {}
    stimulus = "\n".join(" ".join(map(str, row)) for row in _stimulus()) + "\n"

    def simulate(
        insertion: str, tdo_drive: str, mutate: Optional[Callable[[Module], None]] = None
    ) -> List[Sample]:
        key = (insertion, tdo_drive, mutate)
        if key not in cache:
            netlist, top = _INSERTIONS[insertion](fixtures_dir, yosys_command, tdo_drive)
            if mutate is not None:
                mutate(netlist.module(top))
            verilog = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)
            ports = netlist.to_json()["modules"][top]["ports"]
            testbench = _testbench(top, ports, _ENABLE[tdo_drive])
            with tempfile.TemporaryDirectory(prefix="warptap-tdo-drive-") as tmpdir:
                design, bench = Path(tmpdir) / f"{top}_inserted.v", Path(tmpdir) / "tb_tdo_drive.v"
                design.write_text(verilog, encoding="utf-8")
                bench.write_text(testbench, encoding="utf-8")
                stdout = run_verilog_testbench(
                    [design, bench],
                    extra_inputs={"stimulus.txt": stimulus},
                    iverilog_command=iverilog_command,
                    vvp_command=vvp_command,
                )
            samples = []
            for line in stdout.splitlines():
                if line.startswith("TRACE,"):
                    _, state, *values = line.split(",")
                    state = TapState(int(state))
                    samples.append(Sample(state, tuple(values[:3]), tuple(values[3:])))
            assert len(samples) == len(_stimulus())
            assert {s.state for s in samples} == set(TapState), "the walk must visit every TAP state"
            cache[key] = samples
        return cache[key]

    return simulate


def _check_edges(samples: List[Sample], signal: str) -> None:
    """``signal`` ("tdo" or "oe") never moves at a rising edge, nor between a falling edge and the
    next cycle's first sample: it changes only at falling edges."""
    for i, s in enumerate(samples):
        pre, rise, _fall = getattr(s, signal)
        assert rise == pre, (
            f"cycle {i} ({s.state.name}): {signal} changed at the rising edge, {pre} -> {rise}"
        )
    for i, (s, nxt) in enumerate(zip(samples, samples[1:])):
        fall, after = getattr(s, signal)[2], getattr(nxt, signal)[0]
        assert fall == after, f"cycle {i}: {signal} moved after the falling edge, {fall} -> {after}"


def _check_enable(samples: List[Sample]) -> None:
    """The enable is high exactly in the cycles spent in Shift-IR/Shift-DR, and changes only at
    falling edges."""
    for i, s in enumerate(samples):
        expected = "1" if drives_tdo(s.state) else "0"
        assert s.oe[0] == expected, f"cycle {i}: enable {s.oe[0]} in {s.state.name}"
    _check_edges(samples, "oe")


def _check_tristate(samples: List[Sample]) -> None:
    """IEEE 1149.1's rule for the pin: a bit in Shift-IR/Shift-DR, released (z) elsewhere, and
    released or driven again only at falling edges."""
    for i, s in enumerate(samples):
        pre = s.tdo[0]
        if drives_tdo(s.state):
            assert pre in ("0", "1"), f"cycle {i}: tdo reads {pre} in {s.state.name}"
        else:
            assert pre == "z", f"cycle {i}: tdo reads {pre} in {s.state.name}, not released"
    _check_edges(samples, "tdo")


def _check_driven_low(samples: List[Sample]) -> None:
    """Driven at all times: a bit in the shift states, 0 outside them, changing at falling edges."""
    for i, s in enumerate(samples):
        expected = ("0", "1") if drives_tdo(s.state) else ("0",)
        assert s.tdo[0] in expected, f"cycle {i}: tdo reads {s.tdo[0]} in {s.state.name}"
    _check_edges(samples, "tdo")


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_tristate_releases_tdo_outside_the_shift_states_at_falling_edges(insertion, run):
    samples = run(insertion, "tristate")
    _check_tristate(samples)
    _check_enable(samples)
    # Not vacuous: the pin really is driven and released, at falling edges, both ways.
    edges = {(s.tdo[1], s.tdo[2]) for s in samples}
    assert edges & {("z", "0"), ("z", "1")}, "never driven again at a falling edge"
    assert edges & {("0", "z"), ("1", "z")}, "never released at a falling edge"


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_low_drives_tdo_low_outside_the_shift_states(insertion, run):
    samples = run(insertion, "low")
    _check_driven_low(samples)
    assert all(s.oe == ("x", "x", "x") for s in samples)  # nothing carries tdo_oe out


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_enable_port_keeps_tdo_driven_and_brings_tdo_oe_out(insertion, run):
    samples = run(insertion, "enable_port")
    _check_driven_low(samples)
    _check_enable(samples)
    assert {(s.oe[1], s.oe[2]) for s in samples} >= {("0", "1"), ("1", "0")}


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_every_mode_shifts_out_the_same_bits(insertion, run):
    shifted = {
        mode: [(i, s.tdo[0]) for i, s in enumerate(run(insertion, mode)) if drives_tdo(s.state)]
        for mode in TDO_DRIVES
    }
    assert shifted["tristate"] == shifted["low"] == shifted["enable_port"]
    assert {bit for _, bit in shifted["tristate"]} == {"0", "1"}  # real data moved


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_control_the_tristate_check_fails_on_the_low_pin(insertion, run):
    """0.0.6's pin, driven low outside the shift states, fails the check the default passes."""
    with pytest.raises(AssertionError, match="not released"):
        _check_tristate(run(insertion, "low"))


@pytest.mark.parametrize("insertion", ["sib", "bsr"])
def test_control_an_enable_that_moves_at_the_rising_edge_fails_the_edge_check(insertion, run):
    samples = run(insertion, "tristate", _enable_from_the_state_decode)
    # Right before every rising edge, as the default is ...
    for s in samples:
        assert (s.tdo[0] == "z") is not drives_tdo(s.state)
    # ... but the pin moves at the rising edge, which is what the edge check is there to catch.
    with pytest.raises(AssertionError, match="changed at the rising edge"):
        _check_tristate(samples)


@pytest.mark.parametrize(
    ("state", "tdo"),
    [
        (TapState.RUN_TEST_IDLE, "0"),  # driven outside the shift states: the "low" pin
        (TapState.EXIT1_DR, "1"),
        (TapState.SHIFT_DR, "z"),  # released while shifting
        (TapState.SHIFT_IR, "x"),
        (TapState.UPDATE_IR, "x"),
    ],
)
def test_the_suite_wide_tdo_parse_refuses_what_the_rule_forbids(state, tdo):
    """``tests/tdo_trace.py``, which every other cross-sim reads ``tdo`` through."""
    with pytest.raises(AssertionError):
        tdo_bit(state.value, tdo)


def test_the_suite_wide_tdo_parse_accepts_what_the_rule_allows():
    assert tdo_bit(TapState.SHIFT_DR.value, "1") == 1
    assert tdo_bit(TapState.SHIFT_IR.value, "0") == 0
    assert tdo_bit(TapState.PAUSE_DR.value, "z") is None
