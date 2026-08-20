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


class InstrumentNode(NamedTuple):
    name: str
    width: int
    capture_value: int  # fixed deterministic stub, mirrors tap_model.IDCODE_VALUE's role
    direction: InstrumentDirection = InstrumentDirection.READ
    signal_bits: tuple[SignalBinding, ...] = ()  # () or exactly `width` long


class SibNode(NamedTuple):
    """One SIB plus everything it gates (§3.2 structure 1). ``nested`` is always empty in v1
    (flat/static network per implementation_plan.md §7 Stage 4's scope) but is a real tuple,
    not ``None`` -- ``sib_retarget.open_path_to`` is genuinely recursive code that only needs
    deepening, not rewriting, once nested SIB-gating-SIB networks land. Matches
    tap_model.py's own "give the parameter now" precedent."""

    sib_name: str
    instrument: Optional[InstrumentNode]
    nested: tuple["SibNode", ...] = ()


class PhysicalGraph(NamedTuple):
    chain: tuple[SibNode, ...]  # TDI-side-first order -- fixes each SIB's own bit position


class ModuleInstance(NamedTuple):
    """One node in the module-instantiation tree (§3.2 structure 2) -- pure lexical
    containment for dotted addressing only. v1's tree is flat by construction (every
    instrument a direct sibling child of the root), matching the confirmed sibling-addressing
    convention, not a simplification of some deeper structure."""

    name: str
    children: tuple["ModuleInstance", ...] = ()


class ICLAddressError(RuntimeError):
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
