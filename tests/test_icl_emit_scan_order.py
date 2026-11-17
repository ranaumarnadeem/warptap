"""The scan path the ICL describes through a multi-bit instrument is the one the RTL shifts.

warptap's per-bit cells take SI into bit 0 and shift out of bit ``width - 1``. In ICL a
``ScanRegister``'s ``ScanInSource`` feeds its leftmost declared bit, so the register is declared
``[0:width-1]`` (``icl_emit._scan_register_range``). The vendored ``icl_parser`` (code warptap
didn't write) builds the scan path from that text; each test reads the path it walks, TDI side
first, from the bit names of the vectors it generates. Its solved bit values are not used: its
retargeting solver is not reliable on these networks, its path building is.

Control: the 0.0.5 declaration (``[width-1:0]``) makes the parser walk one bit of the register,
the one SI and SO both named, so it sees a 2-bit chain where the RTL has 4.
"""

from __future__ import annotations

import pytest

from warptap.icl_emit import to_icl
from warptap.icl_model import InstrumentDirection, SignalBinding
from warptap.sib_plan import InstrumentSpec, build_sib_plan

_WIDTH = 3


def _network(direction: InstrumentDirection):
    spec = InstrumentSpec(
        "x", width=_WIDTH, capture_value=0, direction=direction,
        signal_bits=tuple(SignalBinding("pins", k) for k in range(_WIDTH)),
    )
    return build_sib_plan([spec], top_name="chip")


def _walked_bits(ijtag_class, text: str, tmp_path, direction: InstrumentDirection) -> list:
    """The bit names of the parser's DR scan for one access to instrument ``x``."""
    path = tmp_path / "chip.icl"
    path.write_text(text, encoding="utf-8")
    ijtag = ijtag_class("chip", [str(path)])
    if direction is InstrumentDirection.WRITE:
        ijtag.iWrite("warptap_instr_x.DR", "0b110")
    else:
        ijtag.iRead("warptap_instr_x.DR", "0b110")
    ijtag.iApply()
    steps = ijtag.getiApplyVectors()
    assert steps and all(step.type_of_chain == "DR" for step in steps)
    (chain,) = steps[-1].chain.values()
    return list(chain.in_data_names)


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
