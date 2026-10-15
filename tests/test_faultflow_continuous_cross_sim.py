"""RTL cross-simulation of the continuous decompressor's pattern retargeting (W1): a REAL
``faultflow.scan.compression.insert_continuous_compression()`` netlist, SIB-inserted by
warptap, run through Icarus Verilog. Proves ``retarget_continuous_faultflow_patterns`` -- solve a
joint set of channel words, then per shift write that shift's word to the channel instrument and
pulse the clock -- actually loads the chains on synthesized hardware, not just against the
numeric model it was derived from (test_faultflow_continuous.py).

Needs a faultflow checkout that has the continuous decompressor (WARPTAP_FAULTFLOW_DIR /
faultflow_dir fixture); only its netlist-generation path is used, as in the one-shot tier
(test_faultflow_compression_cross_sim.py, whose helpers this module reuses).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from test_faultflow_compression_cross_sim import (
    _RESET_LEAD_IN,
    _blackbox_stub_verilog,
    _correlate_skipping_pulses,
    _render_stimulus,
    _select_extest_ops,
    _stimulus_rows,
    _two_chain_core_json,
)

from warptap.faultflow_continuous import (
    ContinuousDecompressor,
    retarget_continuous_faultflow_patterns,
)
from warptap.faultflow_compression import LfsrPolynomial
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.netlist import Netlist
from warptap.pdl_verify import check_reads
from warptap.sib_insert import insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.sim_io import run_verilog_testbench
from warptap.yosys_io import ingest, write_verilog_from_json

_NUM_CHANNELS = 4
_MAX_CHAIN_LENGTH = 6
_CHAIN_INSTRUMENTS = {0: "scan_out_0", 1: "scan_out_1"}


def _numeric_scan_ins(poly, taps, injectors, words):
    """The ring bit by bit (see test_faultflow_continuous.py)."""
    width = poly.width
    state = [0] * width
    out = []
    for word in words:
        out.append([sum(state[k] for k in tap) & 1 for tap in taps])
        fb = state[width - 1]
        state = [fb] + [
            state[i - 1] ^ (fb if i in poly.taps else 0) for i in range(1, width)
        ]
        for channel, stage in enumerate(injectors):
            state[stage] ^= word[channel]
    return out


@pytest.fixture
def continuous_fixture(faultflow_dir: Path, yosys_command: str):
    src_dir = str(faultflow_dir)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from faultflow.scan.compression import insert_continuous_compression
    except ImportError as exc:
        pytest.skip(f"faultflow's continuous decompressor not importable: {exc}")

    with tempfile.TemporaryDirectory(prefix="warptap-continuous-fixture-") as tmpdir:
        tmp = Path(tmpdir)
        core_json_path = tmp / "core.json"
        core_json_path.write_text(json.dumps(_two_chain_core_json()), encoding="utf-8")
        liberty = (
            faultflow_dir / "cells" / "sky130" / "sky130_fd_sc_hd__tt_025C_1v80.lib"
        )
        output_json_path, decompressor = insert_continuous_compression(
            core_json_path,
            "core_top",
            ["scan_in_0", "scan_in_1"],
            _NUM_CHANNELS,
            liberty,
            tmp / "compressed.json",
            workdir=tmp / "work",
        )
        composed_json = json.loads(output_json_path.read_text(encoding="utf-8"))
        composed_verilog = write_verilog_from_json(
            composed_json, yosys_command=yosys_command
        )
        persistent = Path(tempfile.mkdtemp(prefix="warptap-continuous-fixture-out-"))
        composed_v = persistent / "core_top_compressed.v"
        composed_v.write_text(composed_verilog, encoding="utf-8")
        stub_v = persistent / "sky130_blackbox_stub.v"
        stub_v.write_text(
            _blackbox_stub_verilog(composed_json, set(composed_json.get("modules", {}))),
            encoding="utf-8",
        )
        real_v = persistent / "sky130_fd_sc_hd.v"
        real_v.write_text(
            "`define FUNCTIONAL\n"
            + (faultflow_dir / "cells" / "sky130" / "sky130_fd_sc_hd.v").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        yield composed_v, stub_v, real_v, decompressor


def _instrument_specs() -> list:
    return [
        InstrumentSpec(
            "tdi_channel",
            width=_NUM_CHANNELS,
            capture_value=0,
            direction=InstrumentDirection.WRITE,
            signal_bits=tuple(
                SignalBinding("ff_tdi_channel", k) for k in range(_NUM_CHANNELS)
            ),
        ),
        InstrumentSpec(
            "scan_out_0",
            width=1,
            capture_value=0,
            signal_bits=(SignalBinding("scan_out_0", 0),),
        ),
        InstrumentSpec(
            "scan_out_1",
            width=1,
            capture_value=0,
            signal_bits=(SignalBinding("scan_out_1", 0),),
        ),
    ]


def test_continuous_retargeting_loads_the_solved_words_on_real_rtl(
    continuous_fixture, yosys_command, iverilog_command, vvp_command
):
    composed_v, stub_v, real_sky130_v, real = continuous_fixture
    # faultflow's decompressor, as the manifest would describe it to warptap.
    decompressor = ContinuousDecompressor(
        poly=LfsrPolynomial(real.polynomial.width, frozenset(real.polynomial.taps)),
        phase_shifter_taps=[list(taps) for taps in real.phase_shifter_taps],
        num_channels=real.num_channels,
        warmup=real.warmup,
    )

    raw = ingest([composed_v, stub_v], "core_top_compressed", yosys_command=yosys_command)
    for name, mod_data in list(raw.get("modules", {}).items()):
        if mod_data.get("attributes", {}).get("blackbox"):
            del raw["modules"][name]
    netlist = Netlist.from_json(raw)
    netlist.module("core_top_compressed").rename_port("tdi", "ff_tdi_channel")
    graph, root = build_sib_plan(_instrument_specs(), top_name="core_top_compressed")
    insert_sib_network(
        netlist, "core_top_compressed", graph, yosys_command=yosys_command
    )

    # A load the hardware can make: channel words chosen here, its scan-ins from the
    # numeric ring (written in this file), then the solver must find words of its own.
    shifts = decompressor.shifts(_MAX_CHAIN_LENGTH)
    words = [
        [(t * 3 + c * 5 + (t * c)) % 2 for c in range(_NUM_CHANNELS)]
        for t in range(shifts)
    ]
    scan_ins = _numeric_scan_ins(
        decompressor.poly,
        decompressor.phase_shifter_taps,
        decompressor.injectors,
        words,
    )
    load_seqs = {
        str(chain): [
            bool(scan_ins[decompressor.warmup + t][chain])
            for t in range(_MAX_CHAIN_LENGTH)
        ]
        for chain in (0, 1)
    }
    # Each chain is one flop deep: only the last shifted-in value survives the load.
    patterns = [
        {
            "load_seqs": load_seqs,
            "expected_unload": {
                "0": [load_seqs["0"][-1]],
                "1": [load_seqs["1"][-1]],
            },
            "capture_pi_values": {},
        }
    ]
    ir_ops = _select_extest_ops() + retarget_continuous_faultflow_patterns(
        patterns,
        _CHAIN_INSTRUMENTS,
        "tdi_channel",
        graph,
        root,
        decompressor=decompressor,
        max_chain_length=_MAX_CHAIN_LENGTH,
        clock_port="clk",
        scan_enable_port="scan_en",
    )

    rows_stim = _RESET_LEAD_IN + _stimulus_rows(ir_ops)
    inserted_verilog = write_verilog_from_json(
        netlist.to_json(), yosys_command=yosys_command
    )
    with tempfile.TemporaryDirectory(prefix="warptap-continuous-cross-sim-") as tmpdir:
        path = Path(tmpdir) / "core_top_compressed_sib_inserted.v"
        path.write_text(inserted_verilog, encoding="utf-8")
        fixtures_dir = Path(__file__).parent / "fixtures"
        stdout = run_verilog_testbench(
            [path, real_sky130_v, fixtures_dir / "tb_compression_two_chain.v"],
            extra_inputs={"stimulus.txt": _render_stimulus(rows_stim)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )

    tdo_by_cycle = []
    for line in stdout.splitlines():
        if not line.startswith("TRACE,"):
            continue
        _, _state, _instr, _cap, _shift, _upd, tdo, _so0, _so1 = line.split(",")
        # Before the first clock edge the decompressor's unreset registers are X in
        # gate-level sim; nothing asserted here reads that early (see the one-shot tier).
        tdo_by_cycle.append(0 if tdo == "x" else int(tdo))
    tdo_by_cycle = tdo_by_cycle[len(_RESET_LEAD_IN) :]

    results = check_reads(ir_ops, _correlate_skipping_pulses(ir_ops, tdo_by_cycle))
    assert len(results) == 2
    for result in results:
        assert result.passed, (
            "real RTL did not end up holding the continuous-loaded content: "
            f"expected={result.expected}, observed={result.observed}, mask={result.mask}"
        )
