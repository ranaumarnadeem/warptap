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
an 11-arm, 4-bit-select mux), which is why icl_parser reaches for a graph library -- but v1's
canonical single-control-bit SIBs never produce that shape, so a plain NamedTuple tree is
sufficient, matching this codebase's existing no-dataclasses convention (bsr_plan.BsrCell/
BsrPlan).
"""

from __future__ import annotations

import enum
from typing import NamedTuple, Optional

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
    uniqueness across the whole tree, on every tree this codebase constructs."""

    sib_name: str
    instrument: Optional[InstrumentNode]
    nested: tuple["SibNode", ...] = ()


class PhysicalGraph(NamedTuple):
    chain: tuple[SibNode, ...]  # TDI-side-first order -- fixes each SIB's own bit position


class ICLModelError(WarptapError):
    """Raised by :func:`validate_physical_graph` for a structurally invalid
    :class:`PhysicalGraph` -- a :class:`SibNode` with both ``instrument`` and ``nested``
    populated, one with neither, or a ``sib_name`` that repeats anywhere else in the tree.
    Every membership-based lookup used throughout retargeting/layout (``sib_retarget.py``,
    ``sib_layout.py``, ``PDLInterpreter._currently_open``) is a flat, unqualified name-keyed
    set with no path-qualification, so a duplicate name anywhere -- not just within one
    level -- would silently corrupt those lookups rather than raise."""


def validate_physical_graph(graph: PhysicalGraph) -> None:
    """Walk the whole tree once, recursively, checking both invariants
    :class:`ICLModelError` documents. Called once at each of the two places a
    :class:`SibNode` tree is actually constructed (``sib_plan.build_sib_plan``,
    ``icl_import.import_icl``) rather than re-checked at every consumption site --
    catches a malformed tree at the one place it could have entered the system."""
    seen: dict[str, bool] = {}

    def _walk(node: SibNode) -> None:
        if node.sib_name in seen:
            raise ICLModelError(
                f"SIB name {node.sib_name!r} is not unique -- appears more than once in "
                "this tree (every sib_name must be globally unique, not just per level)"
            )
        seen[node.sib_name] = True
        if node.instrument is not None and node.nested:
            raise ICLModelError(
                f"SIB {node.sib_name!r} has both an instrument and a nested network -- "
                "exactly one of the two is allowed, never both"
            )
        if node.instrument is None and not node.nested:
            raise ICLModelError(
                f"SIB {node.sib_name!r} has neither an instrument nor a nested network -- "
                "it must gate exactly one of the two"
            )
        for child in node.nested:
            _walk(child)

    for top in graph.chain:
        _walk(top)


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
