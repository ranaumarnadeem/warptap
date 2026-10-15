"""warptap.icl_emit_1500: the ICL of an IEEE 1500 wrapper and of wrappers chained on a SoC.

Byte-for-byte goldens for one wrapper and a chain (regenerate deliberately, never
casually), the shape of the text a retargeting tool reads, the instruction values ICL
gives the WIR, and -- the real check -- both goldens through the vendored ``icl_parser``
(its structural check and its retargeting-graph build), a tool warptap didn't write."""

from __future__ import annotations

from pathlib import Path

import pytest

from warptap.icl_emit_1500 import (
    Icl1500Error,
    icl_literal,
    icl_name,
    to_icl_1500,
    to_icl_1500_chain,
)
from warptap.wsp1500_model import (
    DEFAULT_INSTRUCTIONS,
    Instruction,
    WbrCell,
    Wrapper1500,
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
PINS = {"wrstn": ("wrst", True)}


def _chain() -> str:
    return to_icl_1500_chain([BLOCK, OTHER], top="soc", pins=PINS)


def test_the_block_matches_its_golden() -> None:
    assert to_icl_1500(BLOCK) == (GOLDEN / "wsp1500_block.icl").read_text("utf-8")


def test_the_chain_matches_its_golden() -> None:
    assert _chain() == (GOLDEN / "wsp1500_chain.icl").read_text("utf-8")


@pytest.mark.parametrize(
    ("code", "literal"),
    [
        (0b000, "3'b000"),
        (0b001, "3'b100"),
        (0b010, "3'b010"),
        (0b011, "3'b110"),
        (0b100, "3'b001"),
        (0b101, "3'b101"),
    ],
)
def test_the_wir_values_are_the_codes_bits_reversed(code: int, literal: str) -> None:
    """A ScanRegister's scan path enters its MSB and leaves its LSB (IEEE 1687), and a
    wrapper shifts its code in last bit first: the first bit shifted in ends nearest
    WSO."""
    assert icl_literal(code, 3) == literal


def test_the_enum_names_the_instructions_by_those_values() -> None:
    text = to_icl_1500(BLOCK)
    for ins in DEFAULT_INSTRUCTIONS:
        assert f"{ins.name} = {icl_literal(ins.code, 3)};" in text
    assert "RefEnum blk_ops;" in text and "ResetValue 3'b000;" in text


def test_the_wsp_and_the_wrapper_scan_access_are_scan_interfaces() -> None:
    text = to_icl_1500(BLOCK)
    assert "SelectPort selectwir;" in text
    assert "ResetPort wrstn { ActivePolarity 0; }" in text
    assert (
        "ScanInterface WSP { Port wsi; Port wso; Port shiftwr; Port capturewr; "
        "Port updatewr; Port selectwir; Port wrck; Port wrstn; }" in text
    )
    assert "Chain c0 { Port si0; Port so0; } Chain c1 { Port si1; Port so1; }" in text


def test_the_boundary_register_runs_head_to_tail_msb_to_lsb() -> None:
    text = to_icl_1500(BLOCK)
    # Chain 0 is a (head, MSB) then b (tail, LSB): WS_EXTEST (3'b100) captures the
    # ports, WS_INTEST (3'b001) holds the input cells.
    assert "3'b100 : a, b;" in text
    assert "3'b001 : WBR0[1], WBR0[0];" in text
    # Chain 1's scan input is chain 0's tail in a serial instruction; WSO is chain 1's.
    assert "1'b1 : WBR0[0];" in text and "ScanOutPort so0 { Source WBR0[0]; }" in text
    assert "ScanMux WdrMux SelectedBy serial {\n        1'b1 : WBR1;" in text
    assert "3'b001 : core.y;" in text  # WS_INTEST: the output cell captures the core


def test_the_mode_makes_a_cell_safe_hold_or_pass_its_signal() -> None:
    text = to_icl_1500(BLOCK)
    # An input cell: safe (0) in EXTEST, its flop in INTEST, else the port.
    assert (
        "DataMux In_a SelectedBy WIR {\n"
        "        3'b100 | 3'b110 : 1'b0;\n"
        "        3'b010 | 3'b001 : WBR0[1];\n"
        "        3'b000 | 3'b101 | 3'b011 | 3'b111 : a;" in text
    )
    # An output cell: safe in INTEST, its flop in EXTEST, else the core's output.
    assert (
        "DataMux Out_y SelectedBy WIR {\n"
        "        3'b010 | 3'b001 : 1'b0;\n"
        "        3'b100 | 3'b110 : WBR1;\n"
        "        3'b000 | 3'b101 | 3'b011 | 3'b111 : core.y;" in text
    )


def test_the_model_rides_in_attributes_for_the_importer() -> None:
    text = to_icl_1500(BLOCK)
    assert 'Attribute warptap_format = "warptap-1500-icl-v1";' in text
    assert 'Attribute warptap_instr_1 = "WS_EXTEST,1,1,0,1";' in text
    assert 'Attribute warptap_cell_1_0 = "output,y";' in text
    assert 'Attribute warptap_chain_ports = "si0:so0,si1:so1";' in text


def test_the_chain_wires_wso_to_the_next_wsi_under_shared_controls() -> None:
    text = _chain()
    assert "Module soc {" in text and text.count("Module blk {") == 1
    assert "ScanOutPort wso { Source u1.wso; }" in text
    u0 = next(l for l in text.splitlines() if l.startswith("    Instance u0 Of blk {"))
    u1 = next(l for l in text.splitlines() if l.startswith("    Instance u1 Of blk2 {"))
    assert "InputPort wsi = wsi;" in u0
    assert "InputPort wsi = u0.wso;" in u1
    # The reset pin drives the wrappers through an inverter.
    assert "ResetPort wrst { ActivePolarity 0; }" in text
    assert text.count("InputPort wrstn = ~wrst;") == 2
    assert text.count("InputPort selectwir = selectwir;") == 2
    # Each wrapper's data inputs and scan access are the SoC's ports.
    assert "DataInPort u0_a;" in text and "InputPort a = u0_a;" in text
    assert "DataInPort u1_d_0;" in text
    assert "ScanInterface u1_WPP {" in text


def test_a_label_becomes_an_icl_name_without_colliding() -> None:
    taken: set[str] = set()
    assert icl_name("d[3]", taken) == "d_3"
    assert icl_name("Source", taken) == "p_Source"  # an ICL word
    assert icl_name("7seg", taken) == "p_7seg"  # starts with a digit
    with pytest.raises(Icl1500Error, match="two ports"):
        icl_name("d_3", taken)


def test_unsupported_wrappers_and_chains_are_refused() -> None:
    with pytest.raises(Icl1500Error, match="every chain"):
        to_icl_1500(Wrapper1500("w", chains=((),)))
    with pytest.raises(Icl1500Error, match="needs a wrapper"):
        to_icl_1500_chain([], top="soc")
    with pytest.raises(Icl1500Error, match="unknown WSP roles"):
        to_icl_1500_chain([BLOCK], top="soc", pins={"bogus": ("x", False)})
    renamed = Wrapper1500("blk", chains=((WbrCell("z", "input"),),))
    with pytest.raises(Icl1500Error, match="two different wrappers"):
        to_icl_1500_chain([BLOCK, renamed], top="soc")


def test_port_names_and_extra_instructions_are_the_wrappers_own() -> None:
    wrapper = Wrapper1500(
        name="w4",
        chains=((WbrCell("a", "input"), WbrCell("y", "output")),),
        ir_bits=4,
        instructions=(
            *DEFAULT_INSTRUCTIONS,
            Instruction("WS_SAFE", 0b0111, False, (0, 1)),
        ),
        ports=WspPorts(wsi="tdi_w", wrck="wclk", chain_in=("scan_a",), chain_out=("scan_z",)),
    )
    text = to_icl_1500(wrapper)
    assert "ScanInPort tdi_w;" in text and "TCKPort wclk;" in text
    assert "ScanOutPort scan_z { Source WBR0[0]; }" in text
    assert "WS_SAFE = 4'b1110;" in text
    assert "WIR_SR[3:0]" in text


def test_a_wrapper_without_a_serial_instruction_has_no_boundary_access_from_wsp() -> None:
    wrapper = Wrapper1500(
        name="par",
        chains=((WbrCell("a", "input"),),),
        instructions=tuple(i for i in DEFAULT_INSTRUCTIONS if not i.serial),
    )
    text = to_icl_1500(wrapper)
    assert "LogicSignal serial" not in text and "WdrMux" not in text
    assert "ScanMux WsoMux SelectedBy selectwir {\n        1'b1 : WIR_SR[0];\n        1'b0 : WBY;" in text


# --- the vendored icl_parser, a tool warptap didn't write ----------------------------


@pytest.mark.parametrize(
    ("make", "top"),
    [
        (lambda: to_icl_1500(BLOCK), "blk"),
        (_chain, "soc"),
        (
            lambda: to_icl_1500(
                Wrapper1500(
                    name="par",
                    chains=((WbrCell("a", "input"), WbrCell("y", "output")),),
                    instructions=tuple(i for i in DEFAULT_INSTRUCTIONS if not i.serial),
                )
            ),
            "par",
        ),
        (
            lambda: to_icl_1500(
                Wrapper1500(
                    name="wide",
                    chains=(
                        tuple(WbrCell(f"d[{k}]", "input") for k in range(4)),
                        tuple(WbrCell(f"q[{k}]", "output") for k in range(3)),
                    ),
                    ir_bits=4,
                )
            ),
            "wide",
        ),
    ],
    ids=["block", "chain", "no_serial_instruction", "wide_four_bit_wir"],
)
def test_the_vendored_parser_checks_and_builds_the_retargeting_graph(
    icl_parser_module, tmp_path: Path, make, top: str
) -> None:
    """Both phases of ``Ijtag(...)``: the structural check and the retargeting-graph
    build, with no exception at all (not even the one test_icl_emit_iclparser_validation
    tolerates)."""
    path = tmp_path / f"{top}.icl"
    path.write_text(make(), encoding="utf-8")
    assert icl_parser_module(top, [str(path)]) is not None
