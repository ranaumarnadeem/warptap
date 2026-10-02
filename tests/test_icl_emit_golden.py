"""Byte-for-byte golden tests for ``to_icl(..., include_access_link=False)``.

That mode is what downstream tools (autoMBIST's ``wrap-test-access``, and every live validation
against the vendored ``icl_parser`` in this suite) depend on, so changes to the AccessLink block
must never alter it. The golden files under ``tests/fixtures/golden_icl/`` were captured from the
emitter *before* the AccessLink instruction-name change; regenerate deliberately, never casually.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from warptap.icl_emit import to_icl
from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SibNode,
    SignalBinding,
)

_GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_icl"


def _read(name, width, capture_value, signal_bits=()):
    return InstrumentNode(
        name=name, width=width, capture_value=capture_value, signal_bits=signal_bits
    )


def _write(name, width, signal_bits):
    return InstrumentNode(
        name=name,
        width=width,
        capture_value=0,
        direction=InstrumentDirection.WRITE,
        signal_bits=signal_bits,
    )


def _flat_read_write_case():
    graph = PhysicalGraph(
        chain=(
            SibNode(sib_name="sib_sensor", instrument=_read("sensor", 3, 0b101)),
            SibNode(
                sib_name="sib_ctrl",
                instrument=_write("ctrl", 2, (SignalBinding("start"), SignalBinding("mode", 0))),
            ),
            SibNode(
                sib_name="sib_status",
                instrument=_read("status", 1, 0, (SignalBinding("done"),)),
            ),
        )
    )
    return graph, ModuleInstance(name="chip")


def _nested_sib_case():
    inner = SibNode(sib_name="sib_inner", instrument=_read("deep", 2, 0b10))
    outer = SibNode(sib_name="sib_outer", instrument=None, nested=(inner,))
    leaf = SibNode(sib_name="sib_leaf", instrument=_read("top_leaf", 1, 1))
    return PhysicalGraph(chain=(outer, leaf)), ModuleInstance(name="chip_nested")


def _scan_mux_case():
    inner_mux = ScanMuxNode(
        mux_name="mux_inner",
        select_width=1,
        arms=(
            ScanArm(values=(0,), instrument=_read("deep_a", 1, 0)),
            ScanArm(values=(1,), instrument=_read("deep_b", 2, 0b01)),
        ),
    )
    outer_mux = ScanMuxNode(
        mux_name="mux_outer",
        select_width=2,
        arms=(
            ScanArm(values=(1,), nested=(inner_mux,)),
            ScanArm(values=(2,), instrument=_read("arm_two", 3, 0b110)),
        ),
    )
    return PhysicalGraph(chain=(outer_mux,)), ModuleInstance(name="chip_mux")


CASES = {
    "flat_read_write": _flat_read_write_case,
    "nested_sib": _nested_sib_case,
    "scan_mux": _scan_mux_case,
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_include_access_link_false_output_is_byte_identical_to_golden(case):
    graph, root = CASES[case]()
    actual = to_icl(graph, root, include_access_link=False)
    expected = (_GOLDEN_DIR / f"{case}.icl").read_bytes().decode("utf-8")
    assert actual == expected


@pytest.mark.parametrize("case", sorted(CASES))
def test_golden_output_contains_no_access_link(case):
    text = (_GOLDEN_DIR / f"{case}.icl").read_text(encoding="utf-8")
    assert "AccessLink" not in text
    assert "BSDLEntity" not in text
