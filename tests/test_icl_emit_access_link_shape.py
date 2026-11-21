"""The two forms ``to_icl`` writes describe the same chain.

Without an AccessLink, the top module chains the top-level slots itself: ``tdi`` into the first
slot's ``SI``, each slot's ``SO`` into the next one's, the last one's into ``tdo``. With one, the
top module has no TAP ports, the top-level slots' ``SI`` are unbound, and the AccessLink's
``ScanInterface`` list makes those connections, first-listed nearest TDI. These tests check that
the list follows the explicit chain's order and that nothing else differs: the module types are
the same text, and each instance statement is the same apart from a top-level slot's ``SI``.
"""

from __future__ import annotations

import re

import pytest
from test_icl_emit_golden import CASES

from warptap.icl_emit import to_icl
from warptap.sib_plan import InstrumentSpec, build_sib_plan

_INSTANCE = re.compile(r"^    Instance (\w+) Of \w+ \{ (.*) \}$", re.MULTILINE)
_SCAN_IN = re.compile(r"InputPort SI = ([\w.]+); ?")
_LIST = re.compile(r"\{ ScanInterface \{\n(.*?)\n        \} \}", re.DOTALL)


def _long_flat_case():
    specs = [InstrumentSpec(f"leaf_{k}", width=1 + k % 3, capture_value=0) for k in range(40)]
    return build_sib_plan(specs, top_name="chip_long")


_SHAPES = {**CASES, "long_flat": _long_flat_case}


def _split(text: str, top: str) -> tuple[str, str]:
    at = text.index(f"Module {top} {{")
    return text[:at], text[at:]


def _instances(top_module: str) -> dict[str, str]:
    return dict(_INSTANCE.findall(top_module))


def _explicit_order(top_module: str) -> list[str]:
    """Walk the explicit form from ``tdi``: the slot whose ``SI`` is ``tdi``, then the slot whose
    ``SI`` is that slot's ``SO``, and so on."""
    fed_by = {}
    for name, body in _INSTANCE.findall(top_module):
        if match := _SCAN_IN.search(body):
            fed_by[match.group(1)] = name
    order, at = [], "tdi"
    while at in fed_by:
        order.append(fed_by[at])
        at = f"{fed_by[at]}.SO"
    return order


def _listed(text: str) -> list[str]:
    (block,) = _LIST.findall(text)
    return [line.strip().removesuffix(".client;") for line in block.splitlines()]


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_the_access_link_lists_the_explicit_chain_in_order(shape):
    graph, root = _SHAPES[shape]()
    _, explicit_top = _split(to_icl(graph, root, include_access_link=False), root.name)
    order = _explicit_order(explicit_top)
    assert len(order) == len(graph.chain)
    assert _listed(to_icl(graph, root, include_access_link=True)) == order


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_the_forms_differ_only_in_the_tap_ports_and_the_top_level_scan_ins(shape):
    graph, root = _SHAPES[shape]()
    explicit_types, explicit_top = _split(to_icl(graph, root, include_access_link=False), root.name)
    linked_types, linked_top = _split(to_icl(graph, root, include_access_link=True), root.name)
    assert linked_types == explicit_types

    top_level = set(_explicit_order(explicit_top))
    explicit, linked = _instances(explicit_top), _instances(linked_top)
    assert linked.keys() == explicit.keys()
    for name, body in explicit.items():
        expected = _SCAN_IN.sub("", body, count=1) if name in top_level else body
        assert linked[name] == expected, name
    assert len(linked_top.splitlines()) == len(linked) + len(top_level) + 8


def test_the_walk_sees_a_broken_chain():
    """Control: drop one top-level binding from the explicit form and the walk stops there, so
    the order check above would fail instead of passing vacuously."""
    graph, root = CASES["flat_read_write"]()
    _, explicit_top = _split(to_icl(graph, root, include_access_link=False), root.name)
    broken = explicit_top.replace("InputPort SI = warptap_sib_sensor.SO; ", "", 1)
    assert broken != explicit_top
    assert _explicit_order(broken) == ["warptap_sib_sensor"]
