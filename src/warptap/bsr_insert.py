"""Boundary-scan register insertion: the impure surgery phase (implementation_plan.md §7
Stage 3). Given a :class:`~warptap.bsr_plan.BsrPlan` for a target module, imports
``tap_core.v`` and the BC cell templates as hierarchical submodules (plan §4.2's "hand-author
once, capture as JSON" mechanism realized as ordinary Yosys submodule instantiation; see
``netlist.Netlist.add_module``'s docstring for why this is simpler than flattened cloning),
instantiates one cell per :class:`~warptap.bsr_plan.BsrCell`, stitches them into a single
shift chain, and wires the chain into ``tap_core``'s ``external_dr_tdo`` input.

BIDIR/BC_7 cells reuse the target design's own existing tri-state driver for their port
(``func_in``/enable), found by pattern-matching a ``$mux`` cell with one operand tied to the
constant ``"z"``: confirmed empirically that Yosys's default ``proc`` lowering of the standard
``assign io = enable ? drive_val : 1'bz;`` idiom produces exactly this shape, *not* a dedicated
``$tribuf`` cell (which only appears from passes this project's ingest pipeline doesn't run).
"""

from __future__ import annotations

from pathlib import Path

from warptap.bsr_plan import BsrFunction, BsrPlan
from warptap.errors import WarptapError
from warptap.netlist import Bit, Module, Netlist, PortInfo
from warptap.tap_model import (
    DEFAULT_TDO_DRIVE,
    check_tdo_drive,
)
from warptap.tap_ports import TCK, TDI, TMS, TRST_N, add_tdo_pin
from warptap.yosys_io import ingest

_RTL_DIR = Path(__file__).resolve().parent / "rtl"

_TAP_CORE = "tap_core"
_BC1_SHIFT_ONLY = "bc1_shift_only"
_BC1_FULL = "bc1_full"
_BC7_BIDIR = "bc7_bidir"


class BsrInsertError(WarptapError):
    """Raised when the target design's structure doesn't match what a Stage 3
    insertion step needs, e.g. no existing tri-state driver found for a bidir
    port, rather than guessing and silently emitting something wrong."""


def _find_existing_tristate_driver(top_mod: Module, bit: Bit) -> tuple[list[Bit], list[Bit]]:
    """Find the cell driving port bit ``bit`` through the standard
    ``assign io = enable ? drive_val : 1'bz;`` idiom: a ``$mux`` whose ``Y`` includes the bit
    (a multi-bit ``io`` is one wide ``$mux`` sharing its ``S``). Returns
    ``(drive_value_bits, enable_bits)``, one bit each. Raises :class:`BsrInsertError`,
    naming the bit, if no such driver is found or its polarity isn't the expected
    ``S ? B : A``-with-``A=="z"`` shape (any other shape is a real, distinct case
    this v1 doesn't attempt to guess at)."""
    for cell in top_mod.data.get("cells", {}).values():
        if cell.get("type") != "$mux":
            continue
        connections = cell.get("connections", {})
        y_bits = connections.get("Y", [])
        if bit not in y_bits:
            continue
        lane = y_bits.index(bit)
        a_bits, b_bits, s_bits = (
            connections.get("A", []),
            connections.get("B", []),
            connections.get("S", []),
        )
        if a_bits[lane] == "z":
            return [b_bits[lane]], s_bits  # Y = S ? B : z -- driven (by B) when S=1
        raise BsrInsertError(
            f"found a $mux driving bit {bit} but its 'z' operand isn't in "
            "the expected position (A) -- polarity not handled by this v1"
        )
    raise BsrInsertError(
        f"no existing tri-state driver ($mux with a 'z' operand) found for bit "
        f"{bit} -- bidir port insertion needs the design's own drive-value/"
        "enable signals to preserve, and none could be found"
    )


def _bit_position(port: PortInfo, bit: int | None) -> int:
    """Where port bit ``bit`` (an HDL bit number, or None for a 1-bit port) sits in
    ``port.bits``."""
    if bit is None:
        if len(port.bits) != 1:
            raise BsrInsertError(
                f"a cell for port {port.name!r} names no bit, but the port is "
                f"{len(port.bits)} bits wide: build the plan with build_bsr_plan"
            )
        return 0
    index = bit - port.offset
    position = len(port.bits) - 1 - index if port.upto else index
    if not 0 <= position < len(port.bits):
        raise BsrInsertError(f"port {port.name!r} has no bit {bit}")
    return position

# Must match rtl/tap_core.v's own parameter defaults: v1 never overrides them on the
# hierarchical instance (plan §2: avoids an unverified per-instance-parameter-override
# JSON round-trip risk), so these are the actual values in effect, not just a convenient
# default to relax later.
DEFAULT_IR_WIDTH = 4
DEFAULT_OPCODE_EXTEST = 0b0000


def _import_template(netlist: Netlist, module_name: str, *, yosys_command: str | None) -> Module:
    rtl_path = _RTL_DIR / f"{module_name}.v"
    raw = ingest([rtl_path], module_name, yosys_command=yosys_command)
    mod = netlist.add_module(module_name, raw["modules"][module_name])
    # Each template is ingested standalone (its own `hierarchy -top <name>` run), so
    # Yosys marks it `(* top = 1 *)` in its own JSON. Only the actual target design's
    # top module should carry that marker once everything is merged into one netlist:
    # multiple "top" modules is misleading and a plausible source of confusion for
    # any later pass that doesn't pass an explicit `-top` override.
    mod.data.get("attributes", {}).pop("top", None)
    mod.set_module_attribute("keep_hierarchy", 1)
    mod.set_module_attribute("keep", 1)
    return mod


def insert_bsr(
    netlist: Netlist,
    top: str,
    plan: BsrPlan,
    *,
    ir_width: int = DEFAULT_IR_WIDTH,
    opcode_extest: int = DEFAULT_OPCODE_EXTEST,
    yosys_command: str | None = None,
    tdo_drive: str = DEFAULT_TDO_DRIVE,
) -> None:
    """Insert a TAP + boundary-scan chain into ``netlist``'s ``top`` module,
    matching ``plan`` (from ``bsr_plan.build_bsr_plan``). Adds five new top-level
    JTAG ports (tck/tms/tdi/trst_n/tdo, plus tdo_oe with ``tdo_drive="enable_port"``).
    Every scanned port's *original* bits stay fully wired to whatever originally
    drove/read them: output3 pins get
    ``detach_port``-ed (the real pin becomes new bits driven through a tri-state
    buffer), but their old bits remain the cell's functional passthrough input, so
    normal-mode (``trst_n`` asserted) behavior is provably unchanged; see
    ``tests/test_bsr_insert_equivalence.py``. Each cell serves one port bit, so a
    multi-bit port is scanned bit by bit.

    ``ir_width``/``opcode_extest`` must match ``rtl/tap_core.v``'s actual compiled
    parameters (its own defaults, currently; see the module-level constants above)
    since this function never overrides them on the instance.

    ``tdo_drive`` says how the ``tdo`` pin is driven, exactly as for
    :func:`warptap.sib_insert.insert_sib_network`: ``"tristate"`` (the default, high
    impedance outside Shift-IR/Shift-DR), ``"low"`` (warptap 0.0.6's behavior) or
    ``"enable_port"`` (driven, plus a ``tdo_oe`` output for a pad cell). Anything else raises
    ValueError.
    """
    check_tdo_drive(tdo_drive)
    top_mod = netlist.module(top)
    port_info_by_name = {p.name: p for p in top_mod.ports()}
    port_bits_by_name = {name: p.bits for name, p in port_info_by_name.items()}
    # ^ snapshot BEFORE any detach_port calls below, so output3 cells see each
    # port's ORIGINAL functional-drive bits, not a port added in this same pass.

    _import_template(netlist, _TAP_CORE, yosys_command=yosys_command)
    _import_template(netlist, _BC1_SHIFT_ONLY, yosys_command=yosys_command)
    _import_template(netlist, _BC1_FULL, yosys_command=yosys_command)
    _import_template(netlist, _BC7_BIDIR, yosys_command=yosys_command)

    tck_bits = top_mod.add_port(TCK, "input")
    tms_bits = top_mod.add_port(TMS, "input")
    tdi_bits = top_mod.add_port(TDI, "input")
    trst_n_bits = top_mod.add_port(TRST_N, "input")
    tdo_connections = add_tdo_pin(top_mod, tdo_drive)

    # Allocated up front (bit IDs, not drivers) so the chain-building loop below can
    # reference them before tap_core itself is instantiated at the end, once the
    # chain's final `so`, tap_core's external_dr_tdo input, is actually known.
    tap_state_bits = top_mod.new_wire(4, name="warptap_tap_state")
    current_instruction_bits = top_mod.new_wire(ir_width, name="warptap_current_instruction")
    capture_dr_bits = top_mod.new_wire(1, name="warptap_capture_dr")
    shift_dr_bits = top_mod.new_wire(1, name="warptap_shift_dr")
    update_dr_bits = top_mod.new_wire(1, name="warptap_update_dr")

    extest_mode_bits = top_mod.new_wire(1, name="warptap_extest_mode")
    opcode_bits: list[Bit] = [str((opcode_extest >> i) & 1) for i in range(ir_width)]
    top_mod.add_cell(
        "warptap_extest_decode",
        "$eq",
        parameters={
            "A_SIGNED": 0, "B_SIGNED": 0,
            "A_WIDTH": ir_width, "B_WIDTH": ir_width, "Y_WIDTH": 1,
        },
        port_directions={"A": "input", "B": "input", "Y": "output"},
        connections={"A": current_instruction_bits, "B": opcode_bits, "Y": extest_mode_bits},
    )

    prev_so: list[Bit] = tdi_bits
    control_pin_out: dict[int, list[Bit]] = {}  # cell_number -> that control cell's pin_out
    control_instance_by_number: dict[int, str] = {}  # cell_number -> its own instance name

    # A control cell is stitched *before* the output3/bidir cell it pairs with (plan
    # ordering), but a bidir pairing's func_in is the port's own pre-existing enable
    # signal, only discoverable once the paired cell is reached and its tri-state
    # driver is searched for. Look ahead once so the control cell's own add_cell call
    # can wire a harmless placeholder now and know to expect a real reconnect later,
    # rather than guessing the pairing's shape mid-loop.
    paired_function_by_control_number = {
        c.disable_cell_index: c.function for c in plan.cells if c.disable_cell_index is not None
    }

    detached_pins: dict[str, list[Bit]] = {}

    def _real_pin(port_name: str) -> list[Bit]:
        """The port's bits as the pin itself, detached from the design's own driver the first
        time one of its output3/bidir cells asks: detaching again would strand the bits the
        earlier cells' tri-state buffers drive."""
        if port_name not in detached_pins:
            top_mod.detach_port(port_name)  # the old bits stay alive via port_bits_by_name
            detached_pins[port_name] = top_mod.port_bits(port_name)
        return detached_pins[port_name]

    for cell in plan.cells:
        instance_name = f"warptap_bsr_{cell.cell_number}"
        cell_attrs = {
            "warptap_bsr_cell_number": cell.cell_number,
            "warptap_bsr_port_name": cell.port_name,
        }
        if cell.function is not BsrFunction.CONTROL:
            position = _bit_position(port_info_by_name[cell.port_name], cell.bit)

        if cell.function is BsrFunction.INPUT:
            so_bits = top_mod.new_wire(1, name=f"{instance_name}_so")
            top_mod.add_cell(
                instance_name,
                _BC1_SHIFT_ONLY,
                port_directions={
                    "pi": "input", "si": "input", "so": "output",
                    "capture_dr": "input", "shift_dr": "input",
                    "tck": "input", "trst_n": "input",
                },
                connections={
                    "pi": [port_bits_by_name[cell.port_name][position]],
                    "si": prev_so, "so": so_bits,
                    "capture_dr": capture_dr_bits, "shift_dr": shift_dr_bits,
                    "tck": tck_bits, "trst_n": trst_n_bits,
                },
                attributes=cell_attrs,
            )
            top_mod.set_keep(cell_name=instance_name)
            prev_so = so_bits

        elif cell.function is BsrFunction.CONTROL:
            so_bits = top_mod.new_wire(1, name=f"{instance_name}_so")
            pin_out_bits = top_mod.new_wire(1, name=f"{instance_name}_pin_out")
            paired_function = paired_function_by_control_number.get(cell.cell_number)
            # v1: plain output3 pairing has no pre-existing output-enable signal to
            # preserve, so func_in is the fixed constant 1 (stated explicitly, not
            # assumed). A bidir pairing DOES have one (the port's own original
            # enable) but it isn't known yet: wire the same harmless placeholder
            # and let the BIDIR branch below reconnect it once found.
            func_in_connection: list[Bit] = ["1"]
            top_mod.add_cell(
                instance_name,
                _BC1_FULL,
                port_directions={
                    "func_in": "input", "si": "input", "so": "output", "pin_out": "output",
                    "capture_dr": "input", "shift_dr": "input", "update_dr": "input",
                    "extest_mode": "input", "tck": "input", "trst_n": "input",
                },
                connections={
                    "func_in": func_in_connection,
                    "si": prev_so, "so": so_bits, "pin_out": pin_out_bits,
                    "capture_dr": capture_dr_bits, "shift_dr": shift_dr_bits,
                    "update_dr": update_dr_bits, "extest_mode": extest_mode_bits,
                    "tck": tck_bits, "trst_n": trst_n_bits,
                },
                attributes=cell_attrs,
            )
            top_mod.set_keep(cell_name=instance_name)
            prev_so = so_bits
            control_pin_out[cell.cell_number] = pin_out_bits
            if paired_function is BsrFunction.BIDIR:
                control_instance_by_number[cell.cell_number] = instance_name

        elif cell.function is BsrFunction.OUTPUT3:
            # The old bit, still fully driven by the design, and the real pin bit, now free.
            func_in_bits = [port_bits_by_name[cell.port_name][position]]
            new_pin_bits = [_real_pin(cell.port_name)[position]]
            so_bits = top_mod.new_wire(1, name=f"{instance_name}_so")
            cell_pin_out_bits = top_mod.new_wire(1, name=f"{instance_name}_pin_out")
            top_mod.add_cell(
                instance_name,
                _BC1_FULL,
                port_directions={
                    "func_in": "input", "si": "input", "so": "output", "pin_out": "output",
                    "capture_dr": "input", "shift_dr": "input", "update_dr": "input",
                    "extest_mode": "input", "tck": "input", "trst_n": "input",
                },
                connections={
                    "func_in": func_in_bits,
                    "si": prev_so, "so": so_bits, "pin_out": cell_pin_out_bits,
                    "capture_dr": capture_dr_bits, "shift_dr": shift_dr_bits,
                    "update_dr": update_dr_bits, "extest_mode": extest_mode_bits,
                    "tck": tck_bits, "trst_n": trst_n_bits,
                },
                attributes=cell_attrs,
            )
            top_mod.set_keep(cell_name=instance_name)
            en_bits = control_pin_out[cell.disable_cell_index]
            top_mod.add_cell(
                f"{instance_name}_tribuf",
                "$tribuf",
                parameters={"WIDTH": 1},
                port_directions={"A": "input", "EN": "input", "Y": "output"},
                connections={"A": cell_pin_out_bits, "EN": en_bits, "Y": new_pin_bits},
            )
            prev_so = so_bits

        elif cell.function is BsrFunction.BIDIR:
            old_bit = port_bits_by_name[cell.port_name][position]
            func_in_bits, orig_enable_bits = _find_existing_tristate_driver(top_mod, old_bit)
            new_pin_bits = [_real_pin(cell.port_name)[position]]  # the real pin, now free
            so_bits = top_mod.new_wire(1, name=f"{instance_name}_so")
            cell_pin_out_bits = top_mod.new_wire(1, name=f"{instance_name}_pin_out")
            top_mod.add_cell(
                instance_name,
                _BC7_BIDIR,
                port_directions={
                    "pin_in": "input", "func_in": "input", "si": "input", "so": "output",
                    "pin_out": "output", "capture_dr": "input", "shift_dr": "input",
                    "update_dr": "input", "extest_mode": "input",
                    "tck": "input", "trst_n": "input",
                },
                connections={
                    "pin_in": new_pin_bits,  # always observes the real pin, drive-direction aside
                    "func_in": func_in_bits,
                    "si": prev_so, "so": so_bits, "pin_out": cell_pin_out_bits,
                    "capture_dr": capture_dr_bits, "shift_dr": shift_dr_bits,
                    "update_dr": update_dr_bits, "extest_mode": extest_mode_bits,
                    "tck": tck_bits, "trst_n": trst_n_bits,
                },
                attributes=cell_attrs,
            )
            top_mod.set_keep(cell_name=instance_name)

            # Fix up the paired control cell's func_in, now that the port's own
            # original enable signal is known -- normal-mode (non-EXTEST) drive
            # behavior must keep matching the design's own original enable, not a
            # constant, unlike a plain output3 pairing (plan §3 step 6).
            control_instance = control_instance_by_number[cell.disable_cell_index]
            top_mod.connect(control_instance, "func_in", orig_enable_bits)

            en_bits = control_pin_out[cell.disable_cell_index]
            top_mod.add_cell(
                f"{instance_name}_tribuf",
                "$tribuf",
                parameters={"WIDTH": 1},
                port_directions={"A": "input", "EN": "input", "Y": "output"},
                connections={"A": cell_pin_out_bits, "EN": en_bits, "Y": new_pin_bits},
            )
            prev_so = so_bits

        else:
            raise NotImplementedError(f"bsr_insert.py does not yet handle {cell.function}")

    top_mod.add_cell(
        "warptap_tap_core",
        _TAP_CORE,
        port_directions={
            "tck": "input", "tms": "input", "tdi": "input", "trst_n": "input",
            **dict.fromkeys(tdo_connections, "output"),
            "tap_state": "output", "current_instruction": "output",
            "capture_dr": "output", "shift_dr": "output", "update_dr": "output",
            "external_dr_tdo": "input",
        },
        connections={
            "tck": tck_bits, "tms": tms_bits, "tdi": tdi_bits, "trst_n": trst_n_bits,
            **tdo_connections, "tap_state": tap_state_bits,
            "current_instruction": current_instruction_bits,
            "capture_dr": capture_dr_bits, "shift_dr": shift_dr_bits,
            "update_dr": update_dr_bits,
            "external_dr_tdo": prev_so,  # the chain's final `so`: "wire into the TAP's DR mux"
        },
    )
    top_mod.set_keep(cell_name="warptap_tap_core")
