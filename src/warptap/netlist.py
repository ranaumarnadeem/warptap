"""Thin object-model layer over Yosys's JSON netlist format (plan §4.2).

Yosys JSON shape: ``{"modules": {name: {"attributes", "ports", "cells", "memories",
"netnames"}}}``. Every module has its own flat namespace of small integer bit IDs,
shared between ports, cells' connections, and netnames — two references to the same
integer are the same net. A constant-driven bit is instead one of the strings
``"0"``/``"1"``/``"x"``/``"z"`` and never collides with a real integer ID.

This module never invents its own schema — it edits the exact dicts
``read_json``/``write_json`` already produce (``backends/json/json.cc``). Critically,
wrapping a dict in :class:`Module`/:class:`Netlist` must never itself mutate that dict
(no ``setdefault``-style "fill in the missing keys" on load) — only an explicit edit
call (``new_wire``, ``add_cell``, ``connect``, ``set_keep``) may change it. A wrap that
silently normalized missing/empty keys on load would make the golden-file no-op
round-trip test (plan §4.3) worthless, since "loaded and immediately dumped, unedited"
needs to be byte-for-byte identical to the input.
"""

from __future__ import annotations

from typing import Any, Union

Bit = Union[int, str]  # int = real per-module net id; "0"/"1"/"x"/"z" = constant


class Module:
    """Wraps one module's raw JSON dict (a value from ``netlist["modules"]``)."""

    def __init__(self, name: str, data: dict[str, Any]):
        self.name = name
        self.data = data
        self._next_bit = self._scan_max_bit() + 1

    def _scan_max_bit(self) -> int:
        max_bit = -1
        for port in self.data.get("ports", {}).values():
            for b in port.get("bits", []):
                if isinstance(b, int):
                    max_bit = max(max_bit, b)
        for cell in self.data.get("cells", {}).values():
            for bits in cell.get("connections", {}).values():
                for b in bits:
                    if isinstance(b, int):
                        max_bit = max(max_bit, b)
        for net in self.data.get("netnames", {}).values():
            for b in net.get("bits", []):
                if isinstance(b, int):
                    max_bit = max(max_bit, b)
        return max_bit

    def alloc_bit(self) -> int:
        """Allocate one fresh, module-unique integer bit id."""
        bit = self._next_bit
        self._next_bit += 1
        return bit

    def alloc_bits(self, width: int) -> list[int]:
        return [self.alloc_bit() for _ in range(width)]

    def new_wire(
        self,
        width: int,
        *,
        name: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> list[int]:
        """Allocate ``width`` fresh bits. If ``name`` is given, register them under
        ``netnames`` so the wire survives ``write_verilog`` with a legible name."""
        bits = self.alloc_bits(width)
        if name is not None:
            self.data.setdefault("netnames", {})[name] = {
                "hide_name": 0,
                "bits": bits,
                "attributes": attributes or {},
            }
        return bits

    def add_cell(
        self,
        cell_name: str,
        cell_type: str,
        *,
        parameters: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
        port_directions: dict[str, str] | None = None,
        connections: dict[str, list[Bit]] | None = None,
    ) -> dict[str, Any]:
        """Register a new cell instance. ``connections`` maps port name to
        bit-vector — pass already-allocated bits (from ``new_wire``/``alloc_bits``)
        or constant strings."""
        cells = self.data.setdefault("cells", {})
        if cell_name in cells:
            raise ValueError(f"cell {cell_name!r} already exists in module {self.name!r}")
        cell: dict[str, Any] = {
            "hide_name": 0,
            "type": cell_type,
            "parameters": parameters or {},
            "attributes": attributes or {},
            "port_directions": port_directions or {},
            "connections": {k: list(v) for k, v in (connections or {}).items()},
        }
        cells[cell_name] = cell
        return cell

    def connect(self, cell_name: str, port: str, bits: list[Bit]) -> None:
        """(Re)wire one port of an existing cell to the given bit-vector."""
        self.data["cells"][cell_name]["connections"][port] = list(bits)

    def set_keep(self, *, wire_name: str | None = None, cell_name: str | None = None) -> None:
        """Tag a wire (by netname) or cell with Yosys's ``keep`` attribute (plan
        §4.3) — the documented idiom for a checkpoint that survives ABC technology
        mapping. Exactly one of ``wire_name``/``cell_name`` should be given."""
        if wire_name is not None:
            self.data["netnames"][wire_name].setdefault("attributes", {})["keep"] = 1
        if cell_name is not None:
            self.data["cells"][cell_name].setdefault("attributes", {})["keep"] = 1

    def cell_count(self) -> int:
        return len(self.data.get("cells", {}))


class Netlist:
    """Wraps a full Yosys JSON document (one or more modules)."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self._modules: dict[str, Module] = {
            name: Module(name, mod_data) for name, mod_data in data.get("modules", {}).items()
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Netlist":
        return cls(raw)

    def to_json(self) -> dict[str, Any]:
        return self.data

    def module(self, name: str) -> Module:
        return self._modules[name]

    def modules(self) -> list[Module]:
        return list(self._modules.values())

    def module_names(self) -> list[str]:
        return list(self._modules.keys())
