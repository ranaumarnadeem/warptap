"""warptap.icl_import_1500: the ICL warptap writes for an IEEE 1500 wrapper, or a chain of
them, back into the model -- exactly, or refused.

The importer reads the model from the ``warptap_*`` attributes and then writes the ICL
from it again: any text that isn't that ICL, byte for byte (a SIB network, a wrapper with
its WIR or WBY edited out, an Enum value changed), is refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from warptap.icl_emit_1500 import to_icl_1500, to_icl_1500_chain
from warptap.icl_import_1500 import Icl1500ImportError, import_icl_1500
from warptap.wsp1500_model import (
    DEFAULT_INSTRUCTIONS,
    Instruction,
    WbrCell,
    Wrapper1500,
    WspChainDesign,
    WspPorts,
)

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden_icl"
BLOCK = Wrapper1500(
    name="blk",
    chains=(
        (WbrCell("a", "input"), WbrCell("b", "input")),
        (WbrCell("y", "output"),),
    ),
)
OTHER = Wrapper1500(
    name="blk2", chains=((WbrCell("d[0]", "input"), WbrCell("q", "output")),)
)
WIDE = Wrapper1500(
    name="wide",
    chains=(
        tuple(WbrCell(f"d[{k}]", "input") for k in range(4)),
        tuple(WbrCell(f"q[{k}]", "output") for k in range(3)),
    ),
    ir_bits=4,
    instructions=(*DEFAULT_INSTRUCTIONS, Instruction("WS_SAFE", 0b0111, False, (0, 1))),
    ports=WspPorts(
        wsi="tdi_w",
        wrck="wclk",
        scan_enable="se",
        chain_in=("scan_a", "scan_b"),
        chain_out=("scan_y", "scan_z"),
    ),
)
PARALLEL_ONLY = Wrapper1500(
    name="par",
    chains=((WbrCell("a", "input"),),),
    instructions=tuple(i for i in DEFAULT_INSTRUCTIONS if not i.serial),
)


@pytest.mark.parametrize("wrapper", [BLOCK, OTHER, WIDE, PARALLEL_ONLY], ids=lambda w: w.name)
def test_a_wrapper_round_trips_through_its_icl(wrapper: Wrapper1500) -> None:
    assert import_icl_1500(to_icl_1500(wrapper)) == wrapper


def test_a_chain_round_trips_with_its_pins() -> None:
    design = WspChainDesign(
        top="soc",
        wrappers=(BLOCK, OTHER, BLOCK),
        pins={"wrstn": ("wrst", True), "wsi": ("soc_wsi", False)},
    )
    text = to_icl_1500_chain(
        design.wrappers, top=design.top, pins=design.pins
    )
    found = import_icl_1500(text)
    assert isinstance(found, WspChainDesign)
    assert found.top == "soc" and found.wrappers == design.wrappers
    assert found.pins["wrstn"] == ("wrst", True)
    assert found.pins["wsi"] == ("soc_wsi", False)
    # A pin the chain didn't name is the first wrapper's own port, direct.
    assert found.pins["wrck"] == ("wrck", False)


def test_the_goldens_import() -> None:
    block = import_icl_1500((GOLDEN / "wsp1500_block.icl").read_text("utf-8"))
    assert block == BLOCK
    chain = import_icl_1500((GOLDEN / "wsp1500_chain.icl").read_text("utf-8"))
    assert isinstance(chain, WspChainDesign)
    assert [w.name for w in chain.wrappers] == ["blk", "blk2"]
    assert chain.pins["wrstn"] == ("wrst", True)


def test_a_sib_network_is_not_a_wrapper() -> None:
    sib = (GOLDEN / "flat_read_write.icl").read_text("utf-8")
    with pytest.raises(Icl1500ImportError, match="not warptap's IEEE 1500 ICL"):
        import_icl_1500(sib)


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("ScanRegister WIR_SR[2:0] { ScanInSource wsi; }\n", "", "no WIR"),
        (
            "    ScanRegister WBY { ScanInSource wsi; CaptureSource 1'b0; ResetValue 1'b0; }\n",
            "",
            "no WBY",
        ),
        ("WS_EXTEST = 3'b100;", "WS_EXTEST = 3'b001;", "an Enum value"),
        ("ResetValue 3'b000;", "ResetValue 3'b111;", "the WIR's reset value"),
        ("1'b0 : WBY;", "1'b0 : WBR1;", "a WDR mux without WBY"),
        ('"WS_EXTEST,1,1,0,1"', '"WS_EXTEST,1,0,0,1"', "an instruction attribute"),
        ('"input,b"', '"output,b"', "a cell attribute"),
        ("Module blk {", "Module blk2 {", "a renamed module"),
    ],
)
def test_icl_that_isnt_exactly_what_the_emitter_writes_is_refused(
    old: str, new: str, why: str
) -> None:
    text = to_icl_1500(BLOCK)
    assert old in text, why
    with pytest.raises(Icl1500ImportError):
        import_icl_1500(text.replace(old, new, 1))


def test_text_without_a_wrapper_or_cut_short_is_refused() -> None:
    with pytest.raises(Icl1500ImportError, match="no module"):
        import_icl_1500("")
    text = to_icl_1500(BLOCK)
    with pytest.raises(Icl1500ImportError):
        import_icl_1500(text[: len(text) // 2])


def test_a_chain_naming_a_wrapper_it_doesnt_define_is_refused() -> None:
    text = to_icl_1500_chain([BLOCK, OTHER], top="soc")
    start = text.index("Module blk2_core")
    end = text.index("Module soc")
    with pytest.raises(Icl1500ImportError):
        import_icl_1500(text[:start] + text[end:])
