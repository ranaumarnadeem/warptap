"""Pure ICL (Instrument Connectivity Language) data model (implementation_plan.md §7 Stage 4,
§3.2). Two genuinely separate structures, confirmed independently by real ICL sources and by
Honza255/icl_parser's own design:

  1. :class:`PhysicalGraph` — the physical shift-topology graph. What actually sits on the scan
     path, in TDI-to-TDO order, and which SIB gates which nested segment.
  2. :class:`ModuleInstance` — the module-instantiation tree. Pure lexical containment, used
     only for dotted-address resolution (``top.wrapper.instrument``), never for shift-order
     math. A SIB's own instance name normally does NOT appear in the dotted address of the
     instrument it gates -- confirmed against real ICL examples that SIB and instrument are
     siblings under the same enclosing module, not parent/child.

No ``networkx`` dependency: real ICL's ``ScanMux ... SelectedBy <expr>`` grammar permits
arbitrary multi-arm, multi-bit combinational selects (a real fixture examined this session had
an 11-arm, 4-bit-select mux), which is why icl_parser reaches for a graph library -- but every
arm of a :class:`ScanMuxNode` (this project's own N-arm generalization of the binary
self-select :class:`SibNode`) still nests its own independent, non-shared sub-chain, so the
whole physical graph stays a pure tree (no cross-branch sharing, no cycles) regardless of how
many arms a mux has. A plain NamedTuple tree remains sufficient, matching this codebase's
existing no-dataclasses convention (bsr_plan.BsrCell/BsrPlan).
"""

from __future__ import annotations

import enum
from typing import NamedTuple, Optional, Union

from warptap.errors import WarptapError


class InstrumentDirection(enum.Enum):
    """Which way an instrument's bits move real design data (implementation_plan.md §7
    Stage 9). One direction per instrument, not per-bit and not real ICL's bidirectional
    DataInPort+DataOutPort-on-one-TDR shape -- every real signal this project binds so far
    is cleanly either read-only status or write-only control, never both."""

    READ = "read"
    WRITE = "write"


class SignalBinding(NamedTuple):
    """One instrument bit's real host-net binding: bit ``bit`` of top-level port
    ``port_name`` on the module being inserted into. An ``InstrumentNode`` with no
    ``signal_bits`` has no real binding -- v1's original stub behavior, unchanged."""

    port_name: str
    bit: int = 0


class Alias(NamedTuple):
    """A named, contiguous sub-range within an instrument's own width -- real ICL's
    ``Alias <name>[hi:lo] = <reg>[hi:lo];`` construct (confirmed real, e.g. a real fixture's
    own ``Alias okay = DO[0];``/``Alias mode[3:0] = DI[6:5],DI[3:2];``). v1 scopes this to a
    single contiguous range referencing one instrument's own register -- real ICL's
    concat-of-multiple-sources ``Alias`` form (multiple comma-joined ranges in one alias) is
    not modeled here; no real caller needs it yet."""

    name: str
    low_bit: int
    high_bit: int  # inclusive; high_bit >= low_bit


class InstrumentNode(NamedTuple):
    name: str
    width: int
    capture_value: int  # fixed deterministic stub, mirrors tap_model.IDCODE_VALUE's role
    direction: InstrumentDirection = InstrumentDirection.READ
    signal_bits: tuple[SignalBinding, ...] = ()  # () or exactly `width` long
    aliases: tuple[Alias, ...] = ()  # named sub-fields PDL's iWrite/iRead can address by name


class SibNode(NamedTuple):
    """One SIB plus everything it gates (§3.2 structure 1). Exactly one of ``instrument``/
    ``nested`` is ever populated: a leaf SIB gates one instrument directly; a hierarchy SIB
    (``instrument=None``) gates a nested sub-network instead, recursively -- real IEEE 1687
    (a SIB's ``fromSO`` binds to either an instrument's ``SO`` or another SIB's ``SO``, never
    both). :func:`validate_physical_graph` enforces this invariant, plus global ``sib_name``
    uniqueness across the whole tree, on every tree this codebase constructs.

    ``nested`` widened to ``tuple["ChainSlot", ...]`` for the multi-arm ScanMux feature --
    annotation-only, no runtime behavior change (no mypy in this repo, and ``NamedTuple``
    fields aren't runtime-type-checked): every tree built before ``ScanMuxNode`` existed still
    only ever puts ``SibNode``s here."""

    sib_name: str
    instrument: Optional[InstrumentNode]
    nested: tuple["ChainSlot", ...] = ()


class ScanArm(NamedTuple):
    """One arm of a :class:`ScanMuxNode` -- the value(s) of the mux's own select field that
    route to it, plus exactly one of ``instrument``/``nested``, the same leaf-xor-hierarchy
    invariant :class:`SibNode` enforces (and for the same reason: real IEEE 1687 has a
    ScanMux arm bind to either one instrument or one further SIB/mux, never both).

    ``values`` is a tuple, not a single ``int``, to stay forward-compatible with real ICL's
    multi-value catch-all-arm form (e.g. a real vendored fixture's own ``mux_3`` mapping 6
    distinct values to one destination arm) without a future breaking change to this field's
    shape -- but v1 requires ``len(values) == 1`` everywhere an arm is actually consumed (this
    module's own :func:`validate_physical_graph`, plus RTL parameter derivation, insertion,
    retargeting, and ICL emit/import), so the *behavior* stays single-value-only until a later
    feature deliberately relaxes that one check."""

    values: tuple[int, ...]
    instrument: Optional[InstrumentNode] = None
    nested: tuple["ChainSlot", ...] = ()


class ScanMuxNode(NamedTuple):
    """An N-arm generalization of :class:`SibNode`'s binary self-select splice -- real IEEE
    1687 ``ScanMux ... SelectedBy <expr> { <value> : <arm>; ... }``. Exactly one arm is ever
    physically live at a time, chosen by comparing the mux's own ``select_width``-bit self-
    select register against each arm's claimed value(s); a value matching no declared arm is
    implicit bypass, directly generalizing a closed :class:`SibNode`'s own never-modeled
    "closed" state.

    ``select_width`` is always caller-specified, independent of ``len(arms)`` -- a real select
    register is often an existing architectural register reused as a selector, not necessarily
    sized to exactly fit the arm count (the vendored ``mux_3`` fixture itself demonstrates this
    pattern: an 11-arm mux selected by a 4-bit field, wider than ``len(arms)`` strictly needs).
    ``arms`` must have at least 2 entries (validated) -- a 1-arm mux isn't a real mux."""

    mux_name: str
    select_width: int
    arms: tuple[ScanArm, ...]


ChainSlot = Union[SibNode, ScanMuxNode]


def slot_name(slot: "ChainSlot") -> str:
    """The globally-unique name identifying one chain slot regardless of kind -- what every
    name-keyed consumer (this module's own :func:`validate_physical_graph`, ``sib_layout.py``,
    ``sib_retarget.py``, ``PDLInterpreter._currently_open``) uses as its lookup key, so a
    ``SibNode`` and a ``ScanMuxNode`` can share one flat namespace without every call site
    needing its own ``isinstance`` check."""
    if isinstance(slot, ScanMuxNode):
        return slot.mux_name
    return slot.sib_name


class PhysicalGraph(NamedTuple):
    chain: tuple[ChainSlot, ...]  # TDI-side-first order -- fixes each slot's own bit position


class ICLModelError(WarptapError):
    """Raised by :func:`validate_physical_graph` for a structurally invalid
    :class:`PhysicalGraph` -- a :class:`SibNode`/:class:`ScanArm` with both ``instrument`` and
    ``nested`` populated, one with neither, a ``sib_name``/``mux_name`` that repeats anywhere
    else in the tree, or one of :class:`ScanMuxNode`'s own additional invariants (at least 2
    arms, a valid ``select_width``, every arm's value(s) in range and unique within that mux,
    exactly one value per arm in v1). Every membership-based lookup used throughout
    retargeting/layout (``sib_retarget.py``, ``sib_layout.py``,
    ``PDLInterpreter._currently_open``) is a flat, unqualified name-keyed set with no
    path-qualification, so a duplicate name anywhere -- not just within one level -- would
    silently corrupt those lookups rather than raise."""


def validate_physical_graph(graph: PhysicalGraph) -> None:
    """Walk the whole tree once, recursively, checking every invariant :class:`ICLModelError`
    documents. Called once at each of the places a :class:`PhysicalGraph` is actually
    constructed (``sib_plan.build_sib_plan``, ``icl_import.import_icl``) rather than
    re-checked at every consumption site -- catches a malformed tree at the one place it
    could have entered the system."""
    seen: dict[str, bool] = {}

    def _require_leaf_xor_nested(owner: str, instrument: Optional[InstrumentNode], nested: tuple) -> None:
        if instrument is not None and nested:
            raise ICLModelError(
                f"{owner} has both an instrument and a nested network -- "
                "exactly one of the two is allowed, never both"
            )
        if instrument is None and not nested:
            raise ICLModelError(
                f"{owner} has neither an instrument nor a nested network -- "
                "it must gate exactly one of the two"
            )

    def _walk_slot(slot: ChainSlot) -> None:
        name = slot_name(slot)
        if name in seen:
            raise ICLModelError(
                f"name {name!r} is not unique -- appears more than once in this tree "
                "(every sib_name/mux_name must be globally unique, not just per level)"
            )
        seen[name] = True
        if isinstance(slot, ScanMuxNode):
            _walk_scan_mux(slot)
        else:
            _require_leaf_xor_nested(f"SIB {slot.sib_name!r}", slot.instrument, slot.nested)
            for child in slot.nested:
                _walk_slot(child)

    def _walk_scan_mux(mux: ScanMuxNode) -> None:
        if mux.select_width < 1:
            raise ICLModelError(
                f"ScanMux {mux.mux_name!r} has select_width={mux.select_width} -- "
                "must be at least 1"
            )
        if len(mux.arms) < 2:
            raise ICLModelError(
                f"ScanMux {mux.mux_name!r} has {len(mux.arms)} arm(s) -- "
                "a ScanMux must have at least 2 arms"
            )
        value_limit = 1 << mux.select_width
        claimed_by: dict[int, int] = {}  # value -> index of the arm that first claimed it
        for arm_index, arm in enumerate(mux.arms):
            if not arm.values:
                raise ICLModelError(
                    f"ScanMux {mux.mux_name!r} arm {arm_index} claims no values -- "
                    "every arm must claim at least one value"
                )
            if len(arm.values) != 1:
                raise ICLModelError(
                    f"ScanMux {mux.mux_name!r} arm {arm_index} claims {len(arm.values)} "
                    "values -- v1 requires exactly one value per arm (a real multi-value "
                    "catch-all arm is not yet supported end-to-end; see ScanArm's own "
                    "docstring)"
                )
            for value in arm.values:
                if not (0 <= value < value_limit):
                    raise ICLModelError(
                        f"ScanMux {mux.mux_name!r} arm {arm_index} claims value {value}, "
                        f"outside the range its {mux.select_width}-bit select field can "
                        f"represent (0..{value_limit - 1})"
                    )
                if value in claimed_by:
                    raise ICLModelError(
                        f"ScanMux {mux.mux_name!r} arms {claimed_by[value]} and {arm_index} "
                        f"both claim value {value} -- every arm's value(s) must be unique "
                        "within one mux"
                    )
                claimed_by[value] = arm_index
            _require_leaf_xor_nested(
                f"ScanMux {mux.mux_name!r} arm {arm_index}", arm.instrument, arm.nested
            )
            for child in arm.nested:
                _walk_slot(child)

    for top in graph.chain:
        _walk_slot(top)


class ModuleInstance(NamedTuple):
    """One node in the module-instantiation tree (§3.2 structure 2) -- pure lexical
    containment for dotted addressing only. v1's tree is flat by construction (every
    instrument a direct sibling child of the root), matching the confirmed sibling-addressing
    convention, not a simplification of some deeper structure."""

    name: str
    children: tuple["ModuleInstance", ...] = ()


class ICLAddressError(WarptapError):
    """Raised by :func:`resolve_dotted_address` when a path segment doesn't resolve --
    matches BsrInsertError's convention of naming the exact bad input rather than
    returning ``None``."""


def resolve_dotted_address(root: ModuleInstance, dotted: str) -> ModuleInstance:
    """Walk ``root``'s children by each dot-separated segment of ``dotted`` in turn.
    ``dotted`` does not include ``root.name`` itself -- it names the path *below* root,
    matching how a real ICL dotted address is relative to the enclosing top module."""
    node = root
    walked = [root.name]
    for segment in dotted.split("."):
        match = next((c for c in node.children if c.name == segment), None)
        if match is None:
            raise ICLAddressError(
                f"no child named {segment!r} under {'.'.join(walked)!r} "
                f"(resolving {dotted!r} from root {root.name!r})"
            )
        node = match
        walked.append(segment)
    return node
