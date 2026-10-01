"""Cross-simulation: every behavioral claim ``warptap.bsdl_emit.to_bsdl`` makes, checked against
the real TAP RTL (``rtl/tap_core.v``) under Icarus Verilog -- both as written, with the default
BSDL, and as an IJTAG_ACCESS insertion builds it (``insert_sib_network``'s own import, with
``ijtag_access_opcode``), with the BSDL written for that opcode.

The claims are read back out of the *emitted BSDL text* (``tests/bsdl_reader.py``), never taken
from ``warptap.tap_model`` -- so this fails if the BSDL and the hardware disagree, whichever of
them is wrong. Stimulus is built from the same ``tap_ir`` ops and ``tap_ir_play`` lowering the
rest of this suite's cross-sims use, driven through ``tests/fixtures/tb_tap_core_bsdl.v``.

Checked here:

- ``INSTRUCTION_CAPTURE``: the bits Capture-IR loads, as shifted out of TDO.
- ``INSTRUCTION_LENGTH``: a 2L-bit IR scan comes back as capture pattern then the first L bits in.
- ``INSTRUCTION_OPCODE`` / ``IDCODE_REGISTER`` / ``REGISTER_ACCESS`` / BYPASS: each declared
  opcode selects the register the BSDL says it does; IDCODE reads back the declared 32-bit value
  and is exactly 32 bits; BYPASS, and every instruction ``REGISTER_ACCESS`` gives the BYPASS
  register, captures 0 and is exactly 1 bit; every undeclared opcode behaves as BYPASS.
- The network-access instructions, those with no register of their own (EXTEST, SAMPLE and
  PRELOAD by default; IJTAG_ACCESS alone with it): TDO follows ``external_dr_tdo``.
- IDCODE as the default instruction: Test-Logic-Reset selects it whether it's reached through TMS
  or through the TRST pin (active low).
- The ``DESIGN_WARNING`` claims: TDO is low outside the shift states, and TDO changes on the
  rising edge of TCK only.

Not checkable by simulation, and not checked here: the ``TAP_SCAN_CLOCK`` frequency (timing is
supplied by the integrator, not an RTL property), ``PHYSICAL_PIN_MAP``/``PIN_MAP`` (a placeholder
declaration, not behavior) and ``COMPONENT_CONFORMANCE`` (a declaration). ``TAP_SCAN_*`` pin
bindings are exercised implicitly: every scan below drives TDI/TMS/TCK and reads TDO by those names.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Callable, List, NamedTuple, Sequence, Tuple

import pytest
from bsdl_reader import Bsdl, parse_bsdl

from warptap import sim_io
from warptap.bsdl_emit import to_bsdl
from warptap.netlist import Netlist
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_fsm import TapState
from warptap.tap_ir import GotoState, ShiftDR, ShiftIR, bits_to_int
from warptap.tap_ir_play import shift_op_ranges, to_cycles
from warptap.tap_model import OPCODE_IJTAG_ACCESS
from warptap.yosys_io import ingest, write_verilog_from_json

_RTL_PATH = Path(__file__).resolve().parent.parent / "src" / "warptap" / "rtl" / "tap_core.v"

_TLR = TapState.TEST_LOGIC_RESET.value
_RTI = TapState.RUN_TEST_IDLE.value
_SHIFT_STATES = (TapState.SHIFT_DR.value, TapState.SHIFT_IR.value)

_rng = random.Random(20260929)
_EXT_BITS = [_rng.getrandbits(1) for _ in range(4096)]

_DR_SCAN_BITS = 40
_DR_PATTERN = 0x5A3C96E1B7  # < 2**40


def _ext(i: int) -> int:
    return _EXT_BITS[i]


class Sample(NamedTuple):
    state: int
    instr: int
    tdo_pre: int
    tdo_rise: int
    tdo_fall: int


Entry = Tuple  # ("reset",) | ("tick", tms, tdi)


class Result(NamedTuple):
    samples: List[Sample]
    resets: List[Tuple[int, int]]  # (state, instruction) sampled while trst_n is still low


@pytest.fixture(scope="module", params=["extest", "ijtag_access"])
def variant(request, tmp_path_factory, fixtures_dir, yosys_command) -> Tuple[Bsdl, Path]:
    """The BSDL and the tap_core it describes: ``rtl/tap_core.v`` as written, or the tap_core an
    IJTAG_ACCESS insertion imports (a specialized copy whose Verilog has no parameters left)."""
    if request.param == "extest":
        return parse_bsdl(to_bsdl("chip", tck_max_freq_hz=10e6)), _RTL_PATH
    raw = ingest([fixtures_dir / "real_signal.v"], "real_signal", yosys_command=yosys_command)
    netlist = Netlist.from_json(raw)
    graph, _root = build_sib_plan([InstrumentSpec("stub", width=1, capture_value=0)])
    insert_sib_network(
        netlist, "real_signal", graph,
        yosys_command=yosys_command, ijtag_access_opcode=OPCODE_IJTAG_ACCESS,
    )
    tap_core = {"modules": {"tap_core": netlist.to_json()["modules"]["tap_core"]}}
    path = tmp_path_factory.mktemp("ijtag_access") / "tap_core.v"
    path.write_text(write_verilog_from_json(tap_core, yosys_command=yosys_command), encoding="utf-8")
    bsdl = to_bsdl("chip", tck_max_freq_hz=10e6, ijtag_access_opcode=OPCODE_IJTAG_ACCESS)
    return parse_bsdl(bsdl), path


@pytest.fixture
def bsdl(variant) -> Bsdl:
    return variant[0]


@pytest.fixture
def sim(variant, fixtures_dir, iverilog_command, vvp_command) -> Callable[..., Result]:
    rtl_path = variant[1]

    def run(entries: Sequence[Entry], ext: Callable[[int], int] = _ext) -> Result:
        lines: List[str] = []
        tick_index = 0
        for entry in entries:
            if entry[0] == "reset":
                lines.append("1 0 0 0")
            else:
                _, tms, tdi = entry
                lines.append(f"0 {tms} {tdi} {ext(tick_index)}")
                tick_index += 1
        stdout = sim_io.run_verilog_testbench(
            [rtl_path, fixtures_dir / "tb_tap_core_bsdl.v"],
            extra_inputs={"stimulus.txt": "\n".join(lines) + "\n"},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
        samples: List[Sample] = []
        resets: List[Tuple[int, int]] = []
        for line in stdout.splitlines():
            if line.startswith("TRACE,"):
                samples.append(Sample(*(int(x) for x in line.split(",")[1:])))
            elif line.startswith("RESET,"):
                _, state, instr = line.split(",")
                resets.append((int(state), int(instr)))
        return Result(samples, resets)

    return run


# A reset, then one TMS=0 tick to reach RUN_TEST_IDLE -- tap_ir_play.to_cycles' precondition.
_LEAD_IN: List[Entry] = [("reset",), ("tick", 0, 0)]
_LEAD_IN_TICKS = 1


def _entries(ops) -> List[Entry]:
    return _LEAD_IN + [("tick", tms, tdi) for tms, tdi in to_cycles(ops)]


def _observed(result: Result, ops) -> List[int]:
    """TDO stream (LSB first) of each Shift-IR/Shift-DR op, sampled pre-edge -- the value a
    tester reads for that shift cycle."""
    return [
        bits_to_int([result.samples[_LEAD_IN_TICKS + start + k].tdo_pre for k in range(n)])
        for start, n in shift_op_ranges(ops)
    ]


def _ext_stream(ops, op_index: int, n: int) -> int:
    """The external_dr_tdo bits (LSB first) the testbench drove during shift op ``op_index``."""
    start, _ = shift_op_ranges(ops)[op_index]
    return bits_to_int([_ext(_LEAD_IN_TICKS + start + k) for k in range(n)])


def _load_ir(opcode: int, ir_width: int) -> list:
    return [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(ir_width, opcode),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _scan_dr(bits: int, tdi: int) -> list:
    return [
        GotoState(TapState.SHIFT_DR),
        ShiftDR(bits, tdi),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _opcode(bsdl: Bsdl, name: str) -> int:
    return int(bsdl.opcodes[name][0], 2)


def _network_instructions(bsdl: Bsdl) -> List[str]:
    """The declared instructions with no register of their own: neither IDCODE nor BYPASS, nor
    given a register by REGISTER_ACCESS."""
    given = {name for names in bsdl.register_access.values() for name in names}
    return sorted(set(bsdl.opcodes) - {"IDCODE", "BYPASS"} - given)


def test_instruction_capture_pattern_and_length_match_the_rtl(bsdl, sim):
    length = bsdl.instruction_length
    mask = (1 << length) - 1
    first = int("01" * length, 2) & mask  # arbitrary, and != the capture pattern
    assert first != int(bsdl.instruction_capture, 2)
    # 2L bits in: the first L out are the Capture-IR pattern, the next L are the first L in.
    # The last L in are all ones (BYPASS), so the scan leaves the TAP in a safe instruction.
    ops = [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(2 * length, first | (mask << length)),
        GotoState(TapState.RUN_TEST_IDLE),
    ]
    (observed,) = _observed(sim(_entries(ops)), ops)
    assert observed & mask == int(bsdl.instruction_capture, 2)
    assert observed >> length == first


def test_idcode_opcode_selects_a_32_bit_register_holding_the_declared_value(bsdl, sim):
    declared = int(bsdl.idcode_register, 2)
    assert len(bsdl.idcode_register) == 32
    first_in = 0xC0FFEE11
    ops = _load_ir(_opcode(bsdl, "IDCODE"), bsdl.instruction_length) + _scan_dr(
        64, first_in | (0x0BADF00D << 32)
    )
    _, dr = _observed(sim(_entries(ops)), ops)
    assert dr & 0xFFFFFFFF == declared  # captured value, LSB out first
    assert dr >> 32 == first_in  # exactly 32 bits of delay


def test_bypass_and_what_register_access_gives_it_are_one_bit_capturing_zero(bsdl, sim):
    """BYPASS's opcode, and each instruction REGISTER_ACCESS gives the BYPASS register (with
    IJTAG_ACCESS: EXTEST, SAMPLE and PRELOAD), selects a 1-bit register that captures 0."""
    tdi = 0b1011_0111
    for name in ["BYPASS", *bsdl.register_access.get("BYPASS", [])]:
        ops = _load_ir(_opcode(bsdl, name), bsdl.instruction_length) + _scan_dr(8, tdi)
        _, dr = _observed(sim(_entries(ops)), ops)
        assert dr == (tdi << 1) & 0xFF, name  # captured 0, then TDI delayed by exactly one cycle


def test_the_declared_network_instructions(bsdl, request):
    expected = {"extest": ["EXTEST", "PRELOAD", "SAMPLE"], "ijtag_access": ["IJTAG_ACCESS"]}
    assert _network_instructions(bsdl) == expected[request.node.callspec.params["variant"]]


def test_network_access_instructions_pass_external_dr_tdo_to_tdo(bsdl, sim):
    for name in _network_instructions(bsdl):
        ops = _load_ir(_opcode(bsdl, name), bsdl.instruction_length) + _scan_dr(24, 0)
        _, dr = _observed(sim(_entries(ops)), ops)
        assert dr == _ext_stream(ops, 1, 24), name
        assert dr != 0  # the stream is not trivially all zeros


def test_every_opcode_selects_what_the_bsdl_says_and_undeclared_ones_are_bypass(bsdl, sim):
    length = bsdl.instruction_length
    network_opcodes = {_opcode(bsdl, name) for name in _network_instructions(bsdl)}
    idcode_opcode = _opcode(bsdl, "IDCODE")
    idcode_value = int(bsdl.idcode_register, 2)
    scan_mask = (1 << _DR_SCAN_BITS) - 1

    ops: list = []
    for opcode in range(1 << length):
        ops += _load_ir(opcode, length) + _scan_dr(_DR_SCAN_BITS, _DR_PATTERN)
    result = sim(_entries(ops))
    observed = _observed(result, ops)  # [IR, DR, IR, DR, ...]

    for opcode in range(1 << length):
        dr_index = 2 * opcode + 1
        if opcode in network_opcodes:
            expected = _ext_stream(ops, dr_index, _DR_SCAN_BITS)
            kind = "external_dr_tdo"
        elif opcode == idcode_opcode:
            expected = idcode_value | ((_DR_PATTERN & 0xFF) << 32)
            kind = "IDCODE"
        else:
            expected = (_DR_PATTERN << 1) & scan_mask
            kind = "BYPASS"
        assert observed[dr_index] == expected, f"opcode {opcode:0{length}b} should act as {kind}"


def test_a_tms_reset_and_the_trst_pin_both_select_idcode(bsdl, sim):
    """IEEE 1149.1: Test-Logic-Reset selects IDCODE (the BSDL's default instruction), however
    it's reached. Load a network instruction and drive five TMS=1 edges: resident in
    Test-Logic-Reset, IDCODE is latched. Load it again and pulse trst_n low (TAP_SCAN_RESET,
    active low): an asynchronous reset to Test-Logic-Reset with IDCODE selected."""
    length = bsdl.instruction_length
    network = _opcode(bsdl, _network_instructions(bsdl)[0])
    idcode = _opcode(bsdl, "IDCODE")
    ops = _load_ir(network, length)
    cycles = len(to_cycles(ops))

    tms_reset = sim(
        _entries(ops)
        + [("tick", 1, 0)] * 5  # RUN_TEST_IDLE -> ... -> Test-Logic-Reset via TMS alone
        + [("tick", 0, 0)]  # sampled pre-edge while resident in Test-Logic-Reset
    )
    in_tlr = tms_reset.samples[_LEAD_IN_TICKS + cycles + 5]
    assert (in_tlr.state, in_tlr.instr) == (_TLR, idcode)

    trst = sim(_entries(ops) + [("tick", 0, 0), ("reset",), ("tick", 0, 0)])
    loaded = trst.samples[_LEAD_IN_TICKS + cycles]
    assert (loaded.state, loaded.instr) == (_RTI, network)
    trst_state, trst_instr = trst.resets[1]  # resets[0] is the lead-in reset
    assert (trst_state, trst_instr) == (_TLR, idcode)
    after_trst = trst.samples[_LEAD_IN_TICKS + cycles + 1]
    assert (after_trst.state, after_trst.instr) == (_TLR, idcode)


def test_tdo_is_low_outside_the_shift_states_and_changes_only_on_the_rising_edge(bsdl, sim):
    """Two DESIGN_WARNING claims, checked over a long run with a network instruction selected
    and external_dr_tdo held high (so any leak of it onto TDO outside a shift state is visible):
    TDO is driven low, not tri-stated or passed through, outside Shift-DR/Shift-IR; and it never
    differs between just-after-rising-edge and just-after-falling-edge (it changes only at the
    rising edge -- 1149.1 wants the falling edge)."""
    length = bsdl.instruction_length
    network = _opcode(bsdl, _network_instructions(bsdl)[0])
    ops = _load_ir(network, length) + _scan_dr(16, 0xA5A5)
    walk_rng = random.Random(7)
    walk: List[Entry] = [("tick", walk_rng.randint(0, 1), walk_rng.randint(0, 1)) for _ in range(400)]
    result = sim(_entries(ops) + walk, ext=lambda i: 1)
    samples = result.samples

    assert {s.state for s in samples} == set(range(16)), "run must visit every TAP state"
    for i, s in enumerate(samples):
        if s.state not in _SHIFT_STATES:
            assert s.tdo_pre == 0, f"TDO not low pre-edge in state {s.state} (tick {i})"
        assert s.tdo_rise == s.tdo_fall, f"TDO changed on the falling edge (tick {i})"
    for prev, nxt in zip(samples, samples[1:]):
        if nxt.state not in _SHIFT_STATES:
            assert prev.tdo_rise == 0 and prev.tdo_fall == 0
    # ... and TDO really does change at rising edges (shifting IR/DR bits), so the equality
    # above is not vacuous.
    assert any(s.tdo_pre != s.tdo_rise for s in samples)
