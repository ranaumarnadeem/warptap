"""Python behavioral model of an inserted boundary-scan chain (implementation_plan.md §7
Stage 3), implementing ``warptap.tap_model``'s ``DataRegister`` protocol so it can be plugged
into ``TapModel.register_data_register(Instruction.EXTEST, ...)`` — the extension point
``TapModelError`` already names. Used by ``tests/test_bsr_insert_cross_sim.py`` to
cross-simulate against the real inserted RTL; not used by ``bsr_insert.py`` itself, which only
ever emits RTL.
"""

from __future__ import annotations

from warptap.bsr_plan import BsrCell, BsrFunction, BsrPlan


class _CellState:
    __slots__ = ("cell", "shift_ff", "po")

    def __init__(self, cell: BsrCell):
        self.cell = cell
        self.shift_ff = 0
        self.po = 0


class BoundaryScanRegister:
    """Models the whole inserted chain as one ``DataRegister``, matching how
    ``tap_core.v`` sees it: a single serial path from TDI, through every
    :class:`~warptap.bsr_plan.BsrCell` in ``cell_number`` order, to
    ``external_dr_tdo``. ``plan.cells`` must contain only INPUT/CONTROL/OUTPUT3
    cells (v1 scope — BIDIR isn't modeled here yet, matching ``bsr_insert.py``)."""

    def __init__(self, plan: BsrPlan):
        for cell in plan.cells:
            if cell.function not in (
                BsrFunction.INPUT,
                BsrFunction.CONTROL,
                BsrFunction.OUTPUT3,
            ):
                raise NotImplementedError(
                    f"BoundaryScanRegister does not yet model {cell.function}"
                )
        self._states = [_CellState(c) for c in plan.cells]
        self._functional_inputs: dict[str, int] = {}

    def reset(self) -> None:
        """Zero every cell's shift/update state, matching rtl/bc1_full.v's and
        rtl/bc1_shift_only.v's own `trst_n` reset behavior. Callers driving this
        model alongside a TapModel should call this whenever they call
        TapModel.reset() — TapModel.reset() only resets its own FSM/IR state, it
        has no way to know which DataRegister protocol implementations need
        resetting too, since not all of them do (BypassRegister/IdcodeRegister's
        backing rtl/tap_core.v registers have no explicit reset at all)."""
        for state in self._states:
            state.shift_ff = 0
            state.po = 0

    def set_functional_inputs(self, values: dict[str, int]) -> None:
        """Set the live value each INPUT/OUTPUT3 cell observes, keyed by the cell's
        real ``port_name``. Call this every cycle before ticking the owning
        ``TapModel``, mirroring how the real RTL's ``pi``/``func_in`` ports are
        continuously live, not just sampled once at capture time."""
        self._functional_inputs = values

    def _observed_value(self, cell: BsrCell) -> int:
        if cell.function is BsrFunction.CONTROL:
            return 1  # matches bsr_insert.py's fixed func_in for v1 control cells
        return self._functional_inputs.get(cell.port_name, 0)

    def capture(self) -> None:
        for state in self._states:
            state.shift_ff = self._observed_value(state.cell)

    def shift(self, tdi: int) -> int:
        if not self._states:
            return tdi
        tdo = self._states[-1].shift_ff
        incoming = tdi
        for state in self._states:
            incoming, state.shift_ff = state.shift_ff, incoming
        return tdo

    def update(self) -> None:
        for state in self._states:
            if state.cell.function in (BsrFunction.CONTROL, BsrFunction.OUTPUT3):
                state.po = state.shift_ff

    def pin_drive_value(self, port_name: str, *, extest_mode: bool) -> int | str | None:
        """What ``port_name``'s real pin currently reads: the driven ``0``/``1``
        value, or ``"z"`` if tri-stated (its paired control cell's effective
        enable is 0). ``None`` if ``port_name`` isn't an OUTPUT3 port at all.

        Mirrors ``y = EN ? A : 1'bz`` with both ``A`` (the output3 cell's
        ``pin_out``) and ``EN`` (the control cell's ``pin_out``) independently
        following ``extest_mode ? po : func_in`` — outside EXTEST the control
        cell's ``func_in`` is the fixed constant ``1`` (v1, matching
        ``bsr_insert.py``), so the driver is always enabled in normal mode; under
        EXTEST, whether it's enabled depends on whatever was last shifted into the
        control cell's ``po`` via Update-DR — which starts disabled on reset
        (``safe_value=0``'s whole point), not enabled by default."""
        output_state = None
        control_state = None
        for state in self._states:
            if state.cell.function is BsrFunction.OUTPUT3 and state.cell.port_name == port_name:
                output_state = state
            elif (
                state.cell.function is BsrFunction.CONTROL
                and state.cell.port_name == f"{port_name}_oe"
            ):
                control_state = state
        if output_state is None:
            return None

        a_value = output_state.po if extest_mode else self._observed_value(output_state.cell)
        en_value = control_state.po if (extest_mode and control_state is not None) else 1
        return a_value if en_value else "z"
