"""Boundary-scan register insertion — the impure surgery phase (implementation_plan.md §7
Stage 3). Given a :class:`~warptap.bsr_plan.BsrPlan` for a target module, imports
``tap_core.v`` and the BC cell templates as hierarchical submodules (plan §4.2's "hand-author
once, capture as JSON" mechanism realized as ordinary Yosys submodule instantiation — see
``netlist.Netlist.add_module``'s docstring for why this is simpler than flattened cloning),
instantiates one cell per :class:`~warptap.bsr_plan.BsrCell`, stitches them into a single
shift chain, and wires the chain into ``tap_core``'s ``external_dr_tdo`` input.

v1 handles ``INPUT``/``CONTROL``/``OUTPUT3`` cells only — ``BIDIR``/BC_7 support (and
``bc7_bidir.v``) land in a later increment, per the plan's sequencing recommendation: prove
the simpler path end to end first.
"""

from __future__ import annotations

from pathlib import Path

from warptap.bsr_plan import BsrFunction, BsrPlan
from warptap.netlist import Bit, Module, Netlist
from warptap.yosys_io import ingest

_RTL_DIR = Path(__file__).resolve().parent / "rtl"

_TAP_CORE = "tap_core"
_BC1_SHIFT_ONLY = "bc1_shift_only"
_BC1_FULL = "bc1_full"

# Must match rtl/tap_core.v's own parameter defaults: v1 never overrides them on the
# hierarchical instance (plan §2 — avoids an unverified per-instance-parameter-override
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
    # top module should carry that marker once everything is merged into one netlist
    # — multiple "top" modules is misleading and a plausible source of confusion for
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
) -> None:
    """Insert a TAP + boundary-scan chain into ``netlist``'s ``top`` module,
    matching ``plan`` (from ``bsr_plan.build_bsr_plan``). Adds five new top-level
    JTAG ports (tck/tms/tdi/trst_n/tdo). Every scanned port's *original* bits stay
    fully wired to whatever originally drove/read them — output3 pins get
    ``detach_port``-ed (the real pin becomes new bits driven through a tri-state
    buffer), but their old bits remain the cell's functional passthrough input, so
    normal-mode (``trst_n`` asserted) behavior is provably unchanged — see
    ``tests/test_bsr_insert_equivalence.py``.

    ``ir_width``/``opcode_extest`` must match ``rtl/tap_core.v``'s actual compiled
    parameters (its own defaults, currently — see the module-level constants above)
    since this function never overrides them on the instance.
    """
    top_mod = netlist.module(top)
    port_bits_by_name = {p.name: p.bits for p in top_mod.ports()}
    # ^ snapshot BEFORE any detach_port calls below, so output3 cells see each
    # port's ORIGINAL functional-drive bits, not a port added in this same pass.

    _import_template(netlist, _TAP_CORE, yosys_command=yosys_command)
    _import_template(netlist, _BC1_SHIFT_ONLY, yosys_command=yosys_command)
    _import_template(netlist, _BC1_FULL, yosys_command=yosys_command)

    tck_bits = top_mod.add_port("tck", "input")
    tms_bits = top_mod.add_port("tms", "input")
    tdi_bits = top_mod.add_port("tdi", "input")
    trst_n_bits = top_mod.add_port("trst_n", "input")
    tdo_bits = top_mod.add_port("tdo", "output")

    # Allocated up front (bit IDs, not drivers) so the chain-building loop below can
    # reference them before tap_core itself is instantiated at the end, once the
    # chain's final `so` — tap_core's external_dr_tdo input — is actually known.
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

    for cell in plan.cells:
        instance_name = f"warptap_bsr_{cell.cell_number}"
        cell_attrs = {
            "warptap_bsr_cell_number": cell.cell_number,
            "warptap_bsr_port_name": cell.port_name,
        }

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
                    "pi": port_bits_by_name[cell.port_name],
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
            top_mod.add_cell(
                instance_name,
                _BC1_FULL,
                port_directions={
                    "func_in": "input", "si": "input", "so": "output", "pin_out": "output",
                    "capture_dr": "input", "shift_dr": "input", "update_dr": "input",
                    "extest_mode": "input", "tck": "input", "trst_n": "input",
                },
                connections={
                    # v1: no pre-existing output-enable signal to preserve for a
                    # plain output pin — stated explicitly, not assumed.
                    "func_in": ["1"],
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

        elif cell.function is BsrFunction.OUTPUT3:
            func_in_bits = port_bits_by_name[cell.port_name]  # old bits, still fully driven
            top_mod.detach_port(cell.port_name)  # old bits kept alive via func_in_bits above
            new_pin_bits = top_mod.port_bits(cell.port_name)  # the real pin, now free to drive
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

        else:
            raise NotImplementedError(
                f"bsr_insert.py does not yet handle {cell.function} — "
                "BC_7/bidir support lands in a later increment"
            )

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
            "external_dr_tdo": prev_so,  # the chain's final `so` — "wire into the TAP's DR mux"
        },
    )
    top_mod.set_keep(cell_name="warptap_tap_core")
