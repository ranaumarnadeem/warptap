"""SIB network insertion — the impure surgery phase (implementation_plan.md §7 Stage 4).
Given a :class:`~warptap.icl_model.PhysicalGraph` (from ``sib_plan.build_sib_plan``), imports
``tap_core.v``, ``sib_cell.v``, ``bc1_shift_only.v``, and ``instrument_write.v`` as
hierarchical submodules (the same "hand-author once, capture as JSON" mechanism
``bsr_insert.py`` established), instantiates one SIB per :class:`~warptap.icl_model.SibNode`
plus one chained instrument-bit cell per instrument bit, and wires the whole network into
``tap_core``'s ``external_dr_tdo`` input, exactly as ``insert_bsr()`` does for the
boundary-scan chain. Each instrument is either READ (a ``bc1_shift_only`` cell, ``pi`` tied to
a fixed-value stub or fanned out from a real host port) or WRITE (an ``instrument_write`` cell,
``pin_out`` wired onto a real host port in place of that port's prior drivers) --
implementation_plan.md §7 Stage 9's real (non-stub) functional-instrument extension.

Deliberately fully independent of ``bsr_insert.py``/``tap_core.v`` (implementation_plan.md
§7 Stage 4's own scoping decision): reuses ``external_dr_tdo`` directly, no new TAP
instruction, zero edits to any Stage 1-3 file. ``_import_template`` is duplicated rather than
shared (exactly two occurrences -- the textbook "rule of three" case for not extracting yet).

Unlike ``bsr_insert.py``, no ``extest_mode`` decode cell is built here: ``sib_cell.v`` has no
``extest_mode`` port at all (it isn't a pin-driving cell gated by which TAP instruction is
active -- it shifts/captures/updates purely as a function of ``capture_dr``/``shift_dr``/
``update_dr``, which are already instruction-independent per-state strobes from ``tap_core``),
so there is nothing in this insertion pass to consume it.
"""

from __future__ import annotations

from pathlib import Path

from warptap.icl_model import InstrumentDirection, PhysicalGraph
from warptap.netlist import Bit, Module, Netlist
from warptap.tap_ports import TCK, TDI, TDO, TMS, TRST_N
from warptap.yosys_io import ingest

_RTL_DIR = Path(__file__).resolve().parent / "rtl"

_TAP_CORE = "tap_core"
_BC1_SHIFT_ONLY = "bc1_shift_only"
_SIB_CELL = "sib_cell"
_INSTRUMENT_WRITE = "instrument_write"

# Must match rtl/tap_core.v's own parameter default: v1 never overrides it on the
# hierarchical instance (same reasoning as bsr_insert.py's DEFAULT_IR_WIDTH), so this is
# the actual value in effect, not just a convenient default to relax later.
DEFAULT_IR_WIDTH = 4


class SibInsertError(RuntimeError):
    """Raised when a :class:`~warptap.icl_model.PhysicalGraph` asks for something this v1
    insertion pass doesn't support -- a SIB with no instrument, a nested SIB-gating-SIB
    network (implementation_plan.md §7 Stage 4 explicitly scopes v1 to flat/static
    networks), a zero-width instrument, a WRITE instrument with no ``signal_bits`` (nothing
    for it to drive), or a ``signal_bits`` length that doesn't match the instrument's
    ``width`` -- rather than silently emitting something structurally broken."""


def _import_template(netlist: Netlist, module_name: str, *, yosys_command: str | None) -> Module:
    rtl_path = _RTL_DIR / f"{module_name}.v"
    raw = ingest([rtl_path], module_name, yosys_command=yosys_command)
    mod = netlist.add_module(module_name, raw["modules"][module_name])
    # Each template is ingested standalone, so Yosys marks it `(* top = 1 *)` in its own
    # JSON -- only the actual target design's top module should carry that marker once
    # everything is merged into one netlist (bsr_insert.py's identical fix).
    mod.data.get("attributes", {}).pop("top", None)
    mod.set_module_attribute("keep_hierarchy", 1)
    mod.set_module_attribute("keep", 1)
    return mod


def insert_sib_network(
    netlist: Netlist,
    top: str,
    graph: PhysicalGraph,
    *,
    ir_width: int = DEFAULT_IR_WIDTH,
    yosys_command: str | None = None,
) -> None:
    """Insert a TAP + SIB network into ``netlist``'s ``top`` module, matching ``graph``
    (from ``sib_plan.build_sib_plan``). Adds five new top-level JTAG ports (tck/tms/tdi/
    trst_n/tdo) -- ``top`` must not already have a TAP inserted (this function doesn't
    coexist with ``bsr_insert.insert_bsr`` on the same module; implementation_plan.md §7
    Stage 4 §1's documented scoping decision).

    ``top`` should be the same string passed as ``sib_plan.build_sib_plan``'s own
    ``top_name`` when that graph's accompanying :class:`~warptap.icl_model.ModuleInstance`
    tree was built (Stage 10's ``icl_emit.to_icl()`` renders that tree's root name as the
    emitted ICL file's real module name) -- nothing here enforces that the two agree.

    Each slot in ``graph.chain`` becomes one ``sib_cell`` instance (its ``select`` tied to
    the constant 1 -- every v1 network is a flat list of top-level, unconditionally
    reachable SIBs) plus ``slot.instrument.width`` chained instrument-bit cells wired
    between the SIB's ``nested_si``/``nested_so`` (implementation_plan.md §7 Stage 9): a
    READ instrument gets ``bc1_shift_only`` cells, each ``pi`` either tied to one bit of
    the fixed ``capture_value`` stub (no ``signal_bits``) or fanned out from one real host
    port bit (``signal_bits`` given); a WRITE instrument gets ``instrument_write`` cells,
    each ``pin_out`` wired directly onto one bit of its target host port -- ``detach_port``
    once per distinct port name, so every existing internal driver/reader of that port's old
    bits is now driven by JTAG instead. A WRITE cell's ``select`` is wired to its own SIB's
    ``nested_select`` output (``sib_cell.v``'s previously-unconsumed "future nested-SIB use"
    port, now redefined as ``po & shift_ff`` -- open both before *and* after the current
    edge) so its update-latch only ever commits on an edge that leaves that SIB open, never
    one that opens or closes it -- without it, an unrelated ``iApply``'s phase-1 retargeting
    shift (which walks every instrument cell's ``capture_dr``/``shift_dr``/``update_dr``
    unconditionally, matching every other v1 cell) would commit garbage into this
    instrument's real host signal, including while retargeting *away* from it. Mutates
    ``netlist`` in place and returns
    ``None`` -- matching ``insert_bsr()``'s convention; the caller already has the
    :class:`~warptap.icl_model.ModuleInstance` tree from ``build_sib_plan`` for dotted-
    address resolution, since instrument naming (unlike Yosys cell instance naming) is
    decided entirely at planning time, not here.

    ``ir_width`` must match ``rtl/tap_core.v``'s actual compiled ``IR_WIDTH`` parameter
    (its own default, currently) since this function never overrides it on the instance.
    """
    top_mod = netlist.module(top)
    # Snapshot before any detach_port() calls below -- mirrors bsr_insert.py's own
    # "read top_mod.ports() before mutating any of them" precaution. A READ instrument
    # bound to a host port taps these bit ids directly (no detach needed, since observing
    # doesn't touch driver wiring); a WRITE instrument detaches its target port instead
    # (see the per-slot loop below).
    port_bits_by_name: dict[str, list[Bit]] = {p.name: p.bits for p in top_mod.ports()}

    _import_template(netlist, _TAP_CORE, yosys_command=yosys_command)
    _import_template(netlist, _BC1_SHIFT_ONLY, yosys_command=yosys_command)
    _import_template(netlist, _SIB_CELL, yosys_command=yosys_command)
    _import_template(netlist, _INSTRUMENT_WRITE, yosys_command=yosys_command)

    tck_bits = top_mod.add_port(TCK, "input")
    tms_bits = top_mod.add_port(TMS, "input")
    tdi_bits = top_mod.add_port(TDI, "input")
    trst_n_bits = top_mod.add_port(TRST_N, "input")
    tdo_bits = top_mod.add_port(TDO, "output")

    tap_state_bits = top_mod.new_wire(4, name="warptap_tap_state")
    current_instruction_bits = top_mod.new_wire(ir_width, name="warptap_current_instruction")
    capture_dr_bits = top_mod.new_wire(1, name="warptap_capture_dr")
    shift_dr_bits = top_mod.new_wire(1, name="warptap_shift_dr")
    update_dr_bits = top_mod.new_wire(1, name="warptap_update_dr")

    prev_so: list[Bit] = tdi_bits
    # Memoized once per port name (not once per bit): a second detach_port() call on an
    # already-detached port would overwrite the f"{name}_pre_bsr" netname entry the first
    # call created, corrupting it.
    detached_bits_by_port: dict[str, list[Bit]] = {}

    for slot in graph.chain:
        if slot.nested:
            raise SibInsertError(
                f"SIB {slot.sib_name!r} has a nested network -- insert_sib_network only "
                "supports flat/static networks in v1 (implementation_plan.md §7 Stage 4)"
            )
        if slot.instrument is None:
            raise SibInsertError(
                f"SIB {slot.sib_name!r} has no instrument -- insert_sib_network requires "
                "every v1 slot to gate a real instrument stub"
            )
        if slot.instrument.width < 1:
            raise SibInsertError(
                f"instrument {slot.instrument.name!r} (gated by {slot.sib_name!r}) has "
                f"width {slot.instrument.width} -- an instrument needs at least 1 bit"
            )

        sib_instance = f"warptap_{slot.sib_name}"
        sib_so_bits = top_mod.new_wire(1, name=f"{sib_instance}_so")
        nested_si_bits = top_mod.new_wire(1, name=f"{sib_instance}_nested_si")
        nested_so_bits = top_mod.new_wire(1, name=f"{sib_instance}_nested_so")
        nested_select_bits = top_mod.new_wire(1, name=f"{sib_instance}_nested_select")

        top_mod.add_cell(
            sib_instance,
            _SIB_CELL,
            port_directions={
                "si": "input", "so": "output",
                "nested_so": "input", "nested_si": "output", "nested_select": "output",
                "select": "input",
                "capture_dr": "input", "shift_dr": "input", "update_dr": "input",
                "tck": "input", "trst_n": "input",
            },
            connections={
                "si": prev_so, "so": sib_so_bits,
                "nested_so": nested_so_bits, "nested_si": nested_si_bits,
                "nested_select": nested_select_bits,
                "select": ["1"],
                "capture_dr": capture_dr_bits, "shift_dr": shift_dr_bits,
                "update_dr": update_dr_bits,
                "tck": tck_bits, "trst_n": trst_n_bits,
            },
            attributes={
                "warptap_sib_name": slot.sib_name,
                "warptap_instrument_name": slot.instrument.name,
            },
        )
        top_mod.set_keep(cell_name=sib_instance)

        width = slot.instrument.width
        direction = slot.instrument.direction
        capture_value = slot.instrument.capture_value
        signal_bits = slot.instrument.signal_bits
        if signal_bits and len(signal_bits) != width:
            raise SibInsertError(
                f"instrument {slot.instrument.name!r} (gated by {slot.sib_name!r}) has "
                f"{len(signal_bits)} signal_bits but width {width}"
            )
        if direction is InstrumentDirection.WRITE and not signal_bits:
            raise SibInsertError(
                f"instrument {slot.instrument.name!r} (gated by {slot.sib_name!r}) is a "
                "WRITE instrument with no signal_bits -- nothing for it to drive"
            )

        prev_inst_so: list[Bit] = nested_si_bits
        for k in range(width):
            inst_instance = f"{sib_instance}_inst_{k}"
            is_last = k == width - 1
            inst_so_bits = (
                nested_so_bits if is_last else top_mod.new_wire(1, name=f"{inst_instance}_so")
            )
            common_connections = {
                "si": prev_inst_so, "so": inst_so_bits,
                "capture_dr": capture_dr_bits, "shift_dr": shift_dr_bits,
                "tck": tck_bits, "trst_n": trst_n_bits,
            }
            attributes = {
                "warptap_sib_name": slot.sib_name,
                "warptap_instrument_bit": k,
            }
            if direction is InstrumentDirection.WRITE:
                binding = signal_bits[k]
                if binding.port_name not in detached_bits_by_port:
                    detached_bits_by_port[binding.port_name] = top_mod.detach_port(
                        binding.port_name
                    )
                old_bits = detached_bits_by_port[binding.port_name]
                top_mod.add_cell(
                    inst_instance,
                    _INSTRUMENT_WRITE,
                    port_directions={
                        "si": "input", "so": "output", "pin_out": "output", "select": "input",
                        "capture_dr": "input", "shift_dr": "input", "update_dr": "input",
                        "tck": "input", "trst_n": "input",
                    },
                    connections={
                        **common_connections,
                        "pin_out": [old_bits[binding.bit]],
                        "select": nested_select_bits,
                        "update_dr": update_dr_bits,
                    },
                    attributes=attributes,
                )
            else:
                if signal_bits:
                    binding = signal_bits[k]
                    pi_bits: list[Bit] = [port_bits_by_name[binding.port_name][binding.bit]]
                else:
                    pi_bits = [str((capture_value >> k) & 1)]
                top_mod.add_cell(
                    inst_instance,
                    _BC1_SHIFT_ONLY,
                    port_directions={
                        "pi": "input", "si": "input", "so": "output",
                        "capture_dr": "input", "shift_dr": "input",
                        "tck": "input", "trst_n": "input",
                    },
                    connections={**common_connections, "pi": pi_bits},
                    attributes=attributes,
                )
            top_mod.set_keep(cell_name=inst_instance)
            prev_inst_so = inst_so_bits

        prev_so = sib_so_bits

    top_mod.add_cell(
        "warptap_tap_core",
        _TAP_CORE,
        port_directions={
            "tck": "input", "tms": "input", "tdi": "input", "trst_n": "input",
            "tdo": "output", "tap_state": "output", "current_instruction": "output",
            "capture_dr": "output", "shift_dr": "output", "update_dr": "output",
            "external_dr_tdo": "input",
        },
        connections={
            "tck": tck_bits, "tms": tms_bits, "tdi": tdi_bits, "trst_n": trst_n_bits,
            "tdo": tdo_bits, "tap_state": tap_state_bits,
            "current_instruction": current_instruction_bits,
            "capture_dr": capture_dr_bits, "shift_dr": shift_dr_bits,
            "update_dr": update_dr_bits,
            "external_dr_tdo": prev_so,
        },
    )
    top_mod.set_keep(cell_name="warptap_tap_core")
