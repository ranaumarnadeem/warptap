"""Pure planning phase for boundary-scan register insertion (implementation_plan.md §7
Stage 3, §3.1). Walks a target module's top-level ports and produces an ordered list of
:class:`BsrCell` records — the ``(cell_number, cell_type, port_name, function, safe_value,
disable_cell_index)`` tuple §3.1 specifies, directly modeled on BSDL's own
``BOUNDARY_REGISTER`` attribute. No Yosys involved here; the impure surgery that actually
inserts cells into a netlist lives in ``bsr_insert.py``.
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


class BsrPlan(NamedTuple):
    cells: list[BsrCell]
    skipped_ports: list[str]  # excluded ports, never silently dropped — always echoed back


def build_bsr_plan(
    module: Module,
    *,
    clock_ports: frozenset[str] = frozenset(),
    excluded_ports: frozenset[str] = frozenset(),
) -> BsrPlan:
    """Walk ``module``'s top-level ports (declaration order — see ``Module.ports()``)
    and build the BSR cell list.

    ``clock_ports``/``excluded_ports`` are explicit and mandatory to opt a port out
    of scanning — Yosys JSON gives no reliable "this is a clock" signal on a plain
    port, so nothing is inferred by name or heuristic (matches the project's
    "document limitations loudly" discipline). An excluded port gets no cell and no
    ``cell_number`` at all: a clock pin never has a shift-register position.

    Every port is handled by exactly one of: skip, or one of the three direction
    branches below — an unrecognized ``direction`` raises immediately rather than
    silently dropping the port, which is what actually guarantees no port goes
    unaccounted for (an *inline* completeness guarantee, not a separate post-hoc
    check — the latter would be dead code here, since this walk's own control flow
    already can't fall through without either skipping or emitting a cell for every
    port).

    ``safe_value`` is uniformly 0 in v1: under the mux polarity ``bsr_insert.py``
    wires (``pin_out = extest_mode ? po : func_in``, a control cell's ``pin_out``
    tied directly to its paired tri-state buffer's ``EN``, no inversion), ``po=0``
    on a control cell *is* the value that disables (Z-states) its paired driver —
    so the uniform-0 default is the safe one by construction, not by coincidence
    (see ``tests/test_bsr_plan.py``'s dedicated proof of this equivalence).

    Control cells are always emitted immediately before the output3/bidir cell they
    gate (``disable_cell_index == cell_number - 1``), matching the convention real
    BSDL files use (confirmed against a live-fetched Lattice ECP5 BSDL file) — not
    an IEEE mandate, a de facto convention worth matching so warptap's own output
    looks normal to any real boundary-scan tool.
    """
    skip = clock_ports | excluded_ports
    cells: list[BsrCell] = []
    skipped: list[str] = []
    cell_number = 0

    for port in module.ports():
        if port.name in skip:
            skipped.append(port.name)
            continue

        if port.direction == "input":
            cells.append(BsrCell(cell_number, "BC_1", port.name, BsrFunction.INPUT, 0, None))
            cell_number += 1
        elif port.direction in ("output", "inout"):
            control_number = cell_number
            cells.append(
                BsrCell(
                    control_number, "BC_1", f"{port.name}_oe", BsrFunction.CONTROL, 0, None
                )
            )
            cell_number += 1
            driven_type = "BC_1" if port.direction == "output" else "BC_7"
            driven_function = BsrFunction.OUTPUT3 if port.direction == "output" else BsrFunction.BIDIR
            cells.append(
                BsrCell(cell_number, driven_type, port.name, driven_function, 0, control_number)
            )
            cell_number += 1
        else:
            raise ValueError(
                f"port {port.name!r} has unrecognized direction {port.direction!r} "
                "(expected 'input', 'output', or 'inout')"
            )

    return BsrPlan(cells=cells, skipped_ports=skipped)
