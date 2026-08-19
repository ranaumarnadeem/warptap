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

from typing import Any, NamedTuple, Union

Bit = Union[int, str]  # int = real per-module net id; "0"/"1"/"x"/"z" = constant


class PortInfo(NamedTuple):
    name: str
    direction: str  # "input" | "output" | "inout"
    bits: list[Bit]


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

    def ports(self) -> list[PortInfo]:
        """Enumerate this module's top-level ports in declaration order. Yosys's own
        ``ports`` dict already preserves Verilog source declaration order (confirmed
        against ``kernel/rtlil.cc``'s port-sort comparator, keyed on ``port_id``,
        which the frontend assigns in source order) and Python dicts preserve
        insertion order, so this is a plain unsorted walk — no re-sort needed, and
        one would actively discard the ordering guarantee plan §3.1's deterministic
        BSR walk depends on."""
        return [
            PortInfo(name, p["direction"], list(p["bits"]))
            for name, p in self.data.get("ports", {}).items()
        ]

    def port_bits(self, name: str) -> list[Bit]:
        """Current bit-vector for port ``name`` — a small read accessor
        complementing ``add_port``/``detach_port`` for callers (e.g. ``bsr_insert.py``)
        that need a port's *current* bits without re-walking ``ports()``."""
        return list(self.data["ports"][name]["bits"])

    def add_port(self, name: str, direction: str, width: int = 1) -> list[int]:
        """Register a brand-new top-level port (e.g. plan §7 Stage 3's tck/tms/tdi/
        trst_n/tdo) with freshly allocated bits."""
        ports = self.data.setdefault("ports", {})
        if name in ports:
            raise ValueError(f"port {name!r} already exists on module {self.name!r}")
        bits = self.alloc_bits(width)
        ports[name] = {"direction": direction, "bits": bits}
        return bits

    def detach_port(self, name: str) -> list[Bit]:
        """Replace an existing port's bits with freshly allocated ones, returning
        the *old* bits. Every existing internal driver/reader of the old bits is
        left wired exactly as before — only the ``ports`` entry changes. Used for
        output3/bidir pins (plan §7 Stage 3): the old bits become the
        functional/intended-drive signal untouched, the new bits become the real
        pin, now free for the caller to wire through a tri-state driver.

        Gives the detached (old) bits a fresh netname (``f"{name}_pre_bsr"``) so
        emitted Verilog doesn't end up with two differently-wired signals sharing
        one name. Also re-points any *existing* ``netnames[name]`` entry (Yosys
        typically creates one per port, mirroring the ``ports`` entry) at the new
        bits — leaving it stale would mean ``name`` refers to two different
        bit-vectors depending on whether ``ports`` or ``netnames`` is consulted,
        which ``write_verilog`` would render as two conflicting drivers of the same
        signal name."""
        port = self.data["ports"][name]
        old_bits = list(port["bits"])
        new_bits = self.alloc_bits(len(old_bits))
        port["bits"] = new_bits
        netnames = self.data.setdefault("netnames", {})
        netnames[f"{name}_pre_bsr"] = {
            "hide_name": 0,
            "bits": old_bits,
            "attributes": {},
        }
        if name in netnames:
            netnames[name]["bits"] = new_bits
        return old_bits

    def set_module_attribute(self, key: str, value: Any = 1) -> None:
        """Tag this module's own top-level ``attributes`` dict (distinct from
        ``set_keep``'s wire/cell targets) — used for ``keep``/``keep_hierarchy`` on
        hand-authored cell templates (plan §4.3), so module-level tagging covers
        every hierarchical instance of this module automatically."""
        self.data.setdefault("attributes", {})[key] = value


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

    def add_module(self, name: str, data: dict[str, Any]) -> Module:
        """Import another module's raw JSON dict (typically captured via
        ``yosys_io.ingest()`` on a small hand-authored cell-template ``.v`` file,
        plan §4.2) into this netlist's ``modules`` dict, so a cell anywhere else in
        this netlist can reference it by ``type=name`` as an ordinary hierarchical
        instance. This is completely standard Yosys JSON/RTLIL — a cell whose
        ``type`` names a module present in the same ``modules`` dict just *is* a
        submodule instance, no bit-cloning of the template's internals required
        (confirmed empirically: a plain instance's JSON carries only its own
        top-level connection bits, referencing the submodule purely by name)."""
        modules = self.data.setdefault("modules", {})
        if name in modules:
            raise ValueError(f"module {name!r} already exists in this netlist")
        modules[name] = data
        mod = Module(name, data)
        self._modules[name] = mod
        return mod
