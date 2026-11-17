"""The scan path the ICL describes through a multi-bit instrument is the one the RTL shifts.

warptap's per-bit cells take SI into bit 0 and shift out of bit ``width - 1``. In ICL a
``ScanRegister``'s ``ScanInSource`` feeds its leftmost declared bit, so the register is declared
``[0:width-1]`` (``icl_emit._scan_register_range``). The vendored ``icl_parser`` (code warptap
didn't write) builds the scan path from that text; each test reads the path it walks, TDI side
first, from the bit names of the vectors it generates. Its solved bit values are not used: its
retargeting solver is not reliable on these networks, its path building is.

Each walk runs in a Python process of its own. The parser keeps state between ``Ijtag``
objects in one process: after ``test_icl_emit_1500.py``'s networks it walks only the SIB bit of
this one, so in the suite's own process the result would depend on what ran before.

Control: the 0.0.5 declaration (``[width-1:0]``) makes the parser walk one bit of the register,
the one SI and SO both named, so it sees a 2-bit chain where the RTL has 4.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from warptap.icl_emit import to_icl
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan

_WIDTH = 3

_WALK_SCRIPT = r"""
import contextlib, io, json, sys
sys.path.insert(0, sys.argv[1])
from src.ijtag import Ijtag
icl_path, operation, target = sys.argv[2:5]
with contextlib.redirect_stdout(io.StringIO()):
    ijtag = Ijtag("chip", [icl_path])
    getattr(ijtag, operation)(target, "0b110")
    ijtag.iApply()
    steps = ijtag.getiApplyVectors()
print("@@STEPS@@" + json.dumps(
    [[s.type_of_chain, [list(c.in_data_names) for c in s.chain.values()]] for s in steps]
))
"""


def _network(direction: InstrumentDirection):
    spec = InstrumentSpec(
        "x", width=_WIDTH, capture_value=0, direction=direction,
        signal_bits=tuple(SignalBinding("pins", k) for k in range(_WIDTH)),
    )
    return build_sib_plan([spec], top_name="chip")


def _walked_bits(ijtag_class, text: str, tmp_path, direction: InstrumentDirection) -> list:
    """The bit names of the parser's DR scan for one access to instrument ``x``, from a fresh
    process. ``ijtag_class`` (the ``icl_parser_module`` fixture) locates the parser and makes
    the test fail or skip as every other parser test does when it is missing."""
    parser_root = Path(sys.modules[ijtag_class.__module__].__file__).resolve().parents[1]
    path = tmp_path / "chip.icl"
    path.write_text(text, encoding="utf-8")
    operation = "iWrite" if direction is InstrumentDirection.WRITE else "iRead"
    proc = subprocess.run(
        [sys.executable, "-c", _WALK_SCRIPT, str(parser_root), str(path), operation,
         "warptap_instr_x.DR"],
        capture_output=True, text=True, timeout=300,
    )
    marker = [line for line in proc.stdout.splitlines() if line.startswith("@@STEPS@@")]
    assert proc.returncode == 0 and marker, proc.stdout + proc.stderr
    steps = json.loads(marker[-1][len("@@STEPS@@"):])
    assert steps and all(chain_type == "DR" for chain_type, _ in steps), steps
    (names,) = steps[-1][1]
    return names


@pytest.mark.parametrize(
    "direction, register",
    [(InstrumentDirection.WRITE, "SR"), (InstrumentDirection.READ, "DR")],
    ids=["write", "read"],
)
def test_the_parser_walks_every_instrument_bit_in_the_rtl_order(
    direction, register, icl_parser_module, tmp_path
):
    graph, root = _network(direction)
    text = to_icl(graph, root, include_access_link=False)
    assert f"ScanRegister {register}[0:{_WIDTH - 1}] {{" in text
    walked = _walked_bits(icl_parser_module, text, tmp_path, direction)
    expected = [f"warptap_instr_x.{register}_{k:04d}" for k in range(_WIDTH)]
    assert walked == expected + ["warptap_sib_x.SR_0000"]


def test_the_0_0_5_declaration_hides_all_but_one_bit(icl_parser_module, tmp_path):
    graph, root = _network(InstrumentDirection.WRITE)
    text = to_icl(graph, root, include_access_link=False)
    old = text.replace("SR[0:2]", "SR[2:0]")
    assert old != text
    walked = _walked_bits(icl_parser_module, old, tmp_path, InstrumentDirection.WRITE)
    assert walked == ["warptap_instr_x.SR_0002", "warptap_sib_x.SR_0000"]
