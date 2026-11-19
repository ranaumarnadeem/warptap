"""Pure planning phase for boundary-scan register insertion (implementation_plan.md §7
Stage 3, §3.1). Walks a target module's top-level ports and produces an ordered list of
:class:`BsrCell` records: the ``(cell_number, cell_type, port_name, function, safe_value,
disable_cell_index)`` tuple §3.1 specifies, directly modeled on BSDL's own
``BOUNDARY_REGISTER`` attribute, plus the ``bit`` of a multi-bit port the cell serves. No Yosys
involved here; the impure surgery that actually inserts cells into a netlist lives in
``bsr_insert.py``.

**Cell numbers count from TDI.** Cell 0 is the first cell after TDI, the order the chain is
built in. BSDL numbers the other way, cell 0 nearest TDO;
:func:`warptap.bsdl_emit.to_bsdl` renumbers.
"""

from __future__ import annotations

import enum
from typing import NamedTuple

from warptap.netlist import Module


class BsrFunction(enum.Enum):
    INPUT = "input"
    OUTPUT3 = "output3"
    CONTROL = "control"
    INTERNAL = "internal"  # reserved for BSDL-schema completeness; v1 never emits this
    CLOCK = "clock"  # reserved for BSDL-schema completeness; v1 never emits this
    BIDIR = "bidir"


class BsrCell(NamedTuple):
    cell_number: int
    cell_type: str  # "BC_1" | "BC_7" in v1; open string since BC_4/8/9/10 are deferred, not rejected
    port_name: str  # a real top-level port name, or f"{port}_oe" for a control cell
    function: BsrFunction
    safe_value: int  # 0 or 1
    disable_cell_index: int | None  # the paired control cell's cell_number, else None
    # The HDL bit number of the port bit this cell serves (its paired output's, for a control
    # cell), as the port is declared: d[3] of ``input [3:0] d`` is 3. None for a 1-bit port.
    bit: int | None = None


class BsrPort(NamedTuple):
    """A top-level port as it was before insertion: its direction and declared range."""

    name: str
    direction: str  # "input" | "output" | "inout"
    width: int
    offset: int = 0  # the lowest HDL bit number
    upto: bool = False  # declared [lo:hi] rather than [hi:lo]

    @property
    def is_vector(self) -> bool:
        """Declared with a range: anything but a plain 1-bit ``input x``."""
        return self.width > 1 or self.offset != 0 or self.upto

    def hdl_bit(self, position: int) -> int:
        """The HDL bit number of the port's ``position``-th bit in Yosys order, which is the
        order of the value's integer bits: position 0 is the least significant."""
        return self.offset + (self.width - 1 - position if self.upto else position)

    def position(self, hdl_bit: int) -> int:
        """Inverse of :meth:`hdl_bit`."""
        index = hdl_bit - self.offset
        return self.width - 1 - index if self.upto else index


class BsrPlan(NamedTuple):
    cells: list[BsrCell]
    skipped_ports: list[str]  # excluded ports, never silently dropped: always echoed back
    # Every top-level port of the module the plan was built from, skipped ones included, in
    # declaration order: what a BSDL port list declares (:func:`warptap.bsdl_emit.to_bsdl`).
    ports: tuple[BsrPort, ...] = ()


def build_bsr_plan(
    module: Module,
    *,
    clock_ports: frozenset[str] = frozenset(),
    excluded_ports: frozenset[str] = frozenset(),
) -> BsrPlan:
    """Walk ``module``'s top-level ports (declaration order; see ``Module.ports()``)
    and build the BSR cell list: one cell per input bit, and a control cell then a driven
    cell per output or inout bit. A multi-bit port gets cells for every bit, its least
    significant first, each with its HDL ``bit`` number.

    ``clock_ports``/``excluded_ports`` are explicit and mandatory to opt a port out
    of scanning: Yosys JSON gives no reliable "this is a clock" signal on a plain
    port, so nothing is inferred by name or heuristic (matches the project's
    "document limitations loudly" discipline). An excluded port gets no cell and no
    ``cell_number`` at all: a clock pin never has a shift-register position.

    Every port is handled by exactly one of: skip, or one of the three direction
    branches below. An unrecognized ``direction`` raises immediately rather than
    silently dropping the port, which is what actually guarantees no port goes
    unaccounted for (an *inline* completeness guarantee, not a separate post-hoc
    check; the latter would be dead code here, since this walk's own control flow
    already can't fall through without either skipping or emitting a cell for every
    port).

    ``safe_value`` is uniformly 0 in v1: under the mux polarity ``bsr_insert.py``
    wires (``pin_out = extest_mode ? po : func_in``, a control cell's ``pin_out``
    tied directly to its paired tri-state buffer's ``EN``, no inversion), ``po=0``
    on a control cell *is* the value that disables (Z-states) its paired driver,
    so the uniform-0 default is the safe one by construction, not by coincidence
    (see ``tests/test_bsr_plan.py``'s dedicated proof of this equivalence).

    Control cells are always emitted immediately before the output3/bidir cell they
    gate (``disable_cell_index == cell_number - 1``), matching the convention real
    BSDL files use (confirmed against a live-fetched Lattice ECP5 BSDL file): not
    an IEEE mandate, a de facto convention worth matching so warptap's own output
    looks normal to any real boundary-scan tool. Each bit of a multi-bit output gets its
    own control cell: an inout bus's bits can each have their own enable.
    """
    skip = clock_ports | excluded_ports
    cells: list[BsrCell] = []
    skipped: list[str] = []
    ports: list[BsrPort] = []
    cell_number = 0

    for port in module.ports():
        bsr_port = BsrPort(port.name, port.direction, len(port.bits), port.offset, port.upto)
        ports.append(bsr_port)
        if port.name in skip:
            skipped.append(port.name)
            continue
        if port.direction not in ("input", "output", "inout"):
            raise ValueError(
                f"port {port.name!r} has unrecognized direction {port.direction!r} "
                "(expected 'input', 'output', or 'inout')"
            )

        bits = (
            [bsr_port.hdl_bit(p) for p in range(bsr_port.width)] if bsr_port.is_vector else [None]
        )
        for bit in bits:
            if port.direction == "input":
                cells.append(
                    BsrCell(cell_number, "BC_1", port.name, BsrFunction.INPUT, 0, None, bit)
                )
                cell_number += 1
                continue
            control_number = cell_number
            cells.append(
                BsrCell(
                    control_number, "BC_1", f"{port.name}_oe", BsrFunction.CONTROL, 0, None, bit
                )
            )
            cell_number += 1
            driven_type = "BC_1" if port.direction == "output" else "BC_7"
            driven_function = BsrFunction.OUTPUT3 if port.direction == "output" else BsrFunction.BIDIR
            cells.append(
                BsrCell(
                    cell_number, driven_type, port.name, driven_function, 0, control_number, bit
                )
            )
            cell_number += 1

    return BsrPlan(cells=cells, skipped_ports=skipped, ports=tuple(ports))
