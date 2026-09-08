"""ICL import (implementation_plan.md §7 Stage 12): parses real ``.icl`` files entirely via
the vendored ``Honza255/icl_parser`` (the same submodule Stage 10's ``icl_emit.py`` validates
against), never touching raw ICL text directly -- extending this project's established
"shell out to a real independent tool, never round-trip through a self-written parser"
discipline to *parsing*, not just validation.

**Scope, narrowed deliberately, matching the approved plan's own language**: this recognizes
only *warptap's own canonical SIB-network shape* -- a top module whose direct children are
:data:`~warptap.icl_emit.SIB_MODULE_TYPE`-typed instances chained ``InputPort SI = <prev>.SO;``
from the top module's own ``tdi`` through to its ``tdo``, each gating exactly one instrument
instance (found via that SIB's own ``fromSO`` binding) named with
:data:`~warptap.icl_emit.INSTRUMENT_MODULE_PREFIX`, OR (recursively) another
:data:`~warptap.icl_emit.SIB_MODULE_TYPE` instance -- a hierarchy SIB gating a nested
sub-network, detected structurally via that same ``fromSO`` binding, exactly mirroring
:mod:`warptap.icl_emit`'s own emission the other direction. This is *not* a general IEEE 1687
SIB-network importer -- arbitrary real-world ICL (parametrized generic instruments, bare
``ScanMux`` edge cases, an IR-decoded DR-mux TAP with no SIB pattern at all, all genuinely
different shapes found in the vendored tool's own ``tests/test_icls`` corpus) is correctly
rejected here, not silently misinterpreted -- see
``tests/test_icl_import_external_fixtures.py`` for the specific, pinned rejection reason each
one hits.

**Direction is detected structurally, not by name**: an instrument instance carrying an
``IclDataRegister`` (``DataRegister`` in ICL text) is WRITE; one with only an
``IclScanRegister`` is READ -- matches :mod:`warptap.icl_emit`'s own real ``DataRegister``-vs-
``ScanRegister`` grammar distinction (Stage 10's own hard-won finding) exactly.

**A real, permanent round-trip limitation, not a bug**: :mod:`warptap.icl_emit` records which
real host net/bit an instrument's ``CaptureSource``/``WriteDataSource`` represents *only in a
``//`` comment* (e.g. ``// CaptureSource fans out from real host port bit(s): bist_start[0]``)
-- the ``CaptureSource``/``WriteDataSource`` clause ICL text itself actually carries is always a
self-reference (``CaptureSource DR[...]``/``WriteDataSource SR[...]``), because ICL has no
confirmed mechanism for naming an external signal there that this project's own research found
(see ``icl_emit.py``'s own module docstring). ANTLR discards comments during parsing, so
:func:`import_icl` has no way to recover the original :class:`~warptap.icl_model.SignalBinding`
values, nor a READ instrument's original fixed ``capture_value`` stub (also comment-only). Every
imported :class:`~warptap.icl_model.InstrumentNode` therefore always has ``signal_bits=()`` and
``capture_value=0``, regardless of what the original network actually had -- stated here
permanently rather than silently returning plausible-looking but fabricated values. Chain
order, instrument names, widths, and READ/WRITE direction all round-trip exactly.

**A real bug in the vendored tool's own retargeting-graph construction, found, precisely
diagnosed, and fixed directly in the vendored submodule** (``third_party/icl_parser``, not a
warptap-side workaround): ``Ijtag(...)`` used to raise a bare ``AssertionError`` from
``IclRegisterModel`` for any network containing a width>1 instrument, regardless of instrument
count or READ/WRITE direction. Root cause was three separate assumptions in
``icl_register_model.py``/``icl_items.py`` that a bit-serial scan port (a SIB's 1-bit
``SI``/``SO``/``fromSO``/``toSI``) connects only to a same-width signal, when real SIB
connectivity requires it to bind to a register of *any* other width in both directions
(narrower driving wider: replicate; wider driving narrower: pick one representative bit) --
including a whole-port ``ScanInSource`` binding (e.g. ``ScanInSource SI[4:0];`` on a 5-bit
register), which the tool's own hand-written test fixtures never exercise (they only ever bind
a single explicit bit, e.g. ``ScanInSource TDI[0];``). Fixed in
``get_element_driver``/``get_scanin_named_index`` to resolve a scan port's driver at its own
natural width and treat a whole-port ``ScanInSource`` reference's bit 0 as the register's MSB
entry point. A separate, independent performance bug in the same construction path --
``icl_process.py`` computing ``inspect.stack()[0][3]`` (a full stack capture with per-frame
source-file resolution) purely to log each parse handler's own already-known name -- made any
network large enough to be interesting impractically slow; replaced with the equivalent but
allocation-free ``sys._getframe().f_code.co_name`` (roughly 15x faster on the affected paths,
confirmed by profiling). ``Ijtag(...)`` also raises ``ValueError`` unconditionally for any
``AccessLink`` block, regardless of content (a real, still-open gap in the vendored tool, not
warptap's). Both are caught here and re-raised as :class:`IclImportError` naming the real
cause, rather than letting a bare third-party traceback surface as this project's own failure;
the ``AssertionError`` branch remains as a safety net for any *other*, not-yet-diagnosed
internal assertion the vendored tool might still raise for a shape this project hasn't hit.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

from warptap.errors import WarptapError
from warptap.icl_emit import INSTRUMENT_MODULE_PREFIX, SIB_INSTANCE_PREFIX, SIB_MODULE_TYPE
from warptap.icl_model import (
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    SibNode,
)
from warptap.tap_ports import TDI, TDO


class IclImportError(WarptapError):
    """Raised when the given ``.icl`` file(s) don't describe a network :func:`import_icl` can
    recognize -- not a warptap-shaped SIB network at all (flat or nested), or a real gap in the
    vendored ``icl_parser`` itself (its "Not supported" ``AccessLink`` gap, or some other
    not-yet-diagnosed internal ``AssertionError`` in its retargeting-graph construction -- see
    module docstring for the width>1 instrument case that used to live here and is now fixed)
    -- rather than a bare, undiagnosed exception from third-party code."""


def _icl_item_classes(icl_parser_module):
    """``IclInstance``/``IclDataRegister``/``IclScanRegister`` aren't part of ``Ijtag``'s own
    public surface, but the module ``Ijtag`` (``icl_parser_module``) is defined in re-exports
    them as plain module attributes via its own ``from .icl_process import *`` chain
    (``icl_process.py`` itself does ``from .icl_items import *``) -- pulled off that already-
    imported module object directly rather than a second, separate absolute import, so this
    works regardless of how the caller set ``sys.path`` up to make ``icl_parser_module``
    importable in the first place (see ``tests/conftest.py``'s ``icl_parser_module`` fixture)."""
    mod = sys.modules[icl_parser_module.__module__]
    return mod.IclInstance, mod.IclDataRegister, mod.IclScanRegister


def _resolve_single(concat_sig):
    """A ``ConcatSig`` (an ICL binding's right-hand side) resolved to the single real
    ``IclItem``/port it names -- every binding warptap's own emitter writes is exactly one
    signal wide (no concatenation), so anything else here means this isn't warptap's own
    shape."""
    items = concat_sig.get_all_icl_items()
    if len(items) != 1:
        raise IclImportError(
            f"expected a single-signal binding, got {len(items)} items in {items!r} -- "
            "not a shape this importer recognizes"
        )
    return items[0]


def _input_binding(instance, port_name: str):
    """The resolved source item bound to ``instance``'s own ``InputPort <port_name> = ...;``,
    or ``None`` if no such binding exists."""
    for connection in instance.connections:
        (sig, concat_sig) = next(iter(connection.items()))
        if sig.get_name() == port_name:
            return _resolve_single(concat_sig)
    return None


def _instrument_node(instr_instance, icl_data_register_type, icl_scan_register_type) -> InstrumentNode:
    """Recover an :class:`~warptap.icl_model.InstrumentNode`'s name/width/direction from its
    parsed module instance -- see this module's own docstring for why ``signal_bits``/
    ``capture_value`` can never be recovered and are always the empty/zero default."""
    instance_name = instr_instance.get_name()
    if not instance_name.startswith(INSTRUMENT_MODULE_PREFIX):
        raise IclImportError(
            f"instrument instance {instance_name!r} doesn't start with "
            f"{INSTRUMENT_MODULE_PREFIX!r} -- this importer only recovers an instrument's "
            "original name from warptap's own naming convention"
        )
    name = instance_name[len(INSTRUMENT_MODULE_PREFIX) :]

    data_registers = instr_instance.get_icl_item_type(icl_data_register_type)
    if data_registers:
        width = data_registers[0].get_vector_size()
        direction = InstrumentDirection.WRITE
    else:
        scan_registers = instr_instance.get_icl_item_type(icl_scan_register_type)
        if len(scan_registers) != 1:
            raise IclImportError(
                f"instrument instance {instance_name!r} has {len(scan_registers)} "
                "ScanRegisters, expected exactly 1 for a READ instrument -- not a shape this "
                "importer recognizes"
            )
        width = scan_registers[0].get_vector_size()
        direction = InstrumentDirection.READ

    return InstrumentNode(name=name, width=width, capture_value=0, direction=direction)


def _sib_name_of(sib_instance) -> str:
    instance_name = sib_instance.get_name()
    return (
        instance_name[len(SIB_INSTANCE_PREFIX) :]
        if instance_name.startswith(SIB_INSTANCE_PREFIX)
        else instance_name
    )


def _walk_chain(
    remaining: List, current, terminal_source, icl_data_register_type, icl_scan_register_type
) -> List[SibNode]:
    """Walk ``current`` -> SIB -> SIB -> ... -> ``terminal_source`` via each SIB instance's own
    ``SI`` binding, TDI-side-first, matching :mod:`warptap.icl_emit`'s own ``prev_so``
    threading exactly -- claiming SIBs from the shared ``remaining`` pool as they're found, so
    a SIB claimed by a nested recursive call (see :func:`_build_node`) can't also be claimed by
    an enclosing level's own walk. ``current``/``terminal_source`` are the top module's own
    ``tdi``/``tdo``-fed source at the top level, or a hierarchy SIB's own ``toSI``/``fromSO``-
    bound target one level deeper. Raises :class:`IclImportError` if the chain doesn't run
    cleanly from ``current`` to ``terminal_source``."""
    ordered: List[SibNode] = []
    while current is not terminal_source:
        next_sib = next(
            (sib for sib in remaining if _input_binding(sib, "SI") is current), None
        )
        if next_sib is None:
            raise IclImportError(
                f"{len(remaining)} {SIB_MODULE_TYPE!r} instance(s) remain unclaimed and this "
                "chain never reaches its expected end -- not a flat-or-nested, TDI-to-TDO SIB "
                "chain this importer recognizes"
            )
        remaining.remove(next_sib)
        ordered.append(
            _build_node(remaining, next_sib, icl_data_register_type, icl_scan_register_type)
        )
        current = next_sib.get_icl_item_name("SO")
    return ordered


def _build_node(
    remaining: List, sib_instance, icl_data_register_type, icl_scan_register_type
) -> SibNode:
    """One already-claimed SIB instance -> one :class:`~warptap.icl_model.SibNode`. Detected
    structurally, not by name (matching this module's own READ-vs-WRITE convention): a
    ``fromSO`` binding that resolves to another :data:`~warptap.icl_emit.SIB_MODULE_TYPE`
    instance is a hierarchy SIB, recursed into (entry point its own ``toSI``, terminal the
    resolved nested instance itself, exactly mirroring how :mod:`warptap.sib_insert` wires a
    hierarchy slot's own ``nested_si``/``nested_so``); anything else is a leaf instrument."""
    sib_name = _sib_name_of(sib_instance)
    from_so_target = _input_binding(sib_instance, "fromSO")
    if from_so_target is None:
        raise IclImportError(
            f"SIB instance {sib_instance.get_name()!r} has no fromSO binding -- not a "
            "warptap-shaped SIB instance"
        )

    target_instance = from_so_target.get_instance()
    if target_instance.get_module_scope() == SIB_MODULE_TYPE:
        entry = sib_instance.get_icl_item_name("toSI")
        nested = _walk_chain(
            remaining, entry, from_so_target, icl_data_register_type, icl_scan_register_type
        )
        return SibNode(sib_name=sib_name, instrument=None, nested=tuple(nested))

    instrument = _instrument_node(target_instance, icl_data_register_type, icl_scan_register_type)
    return SibNode(sib_name=sib_name, instrument=instrument)


def _collect_instrument_children(chain) -> List[ModuleInstance]:
    """Recursively walk a chain (and any nested sub-chains) collecting each leaf instrument as
    a flat :class:`~warptap.icl_model.ModuleInstance` sibling -- mirrors
    :mod:`warptap.sib_plan`'s own flat ``ModuleInstance`` tree regardless of SIB-nesting depth,
    the direction ``icl_emit.py``'s own ``_collect_instruments`` walks."""
    children: List[ModuleInstance] = []
    for node in chain:
        if node.nested:
            children.extend(_collect_instrument_children(node.nested))
        else:
            children.append(ModuleInstance(name=node.instrument.name))
    return children


def import_icl(
    icl_paths: List[Path], top_module: str, *, icl_parser_module
) -> Tuple[PhysicalGraph, ModuleInstance]:
    """Parse ``icl_paths`` entirely via ``icl_parser_module`` (the vendored ``Ijtag`` class,
    injected the same way ``tests/conftest.py``'s ``icl_parser_module`` fixture already
    provides it to Stage 10's validation tests) and convert the result into warptap's own
    :class:`~warptap.icl_model.PhysicalGraph`/:class:`~warptap.icl_model.ModuleInstance`.

    Raises :class:`IclImportError` for: a real ``icl_parser`` gap (its own retargeting-graph
    ``AssertionError``, or its "Not supported" ``AccessLink`` rejection -- both already
    documented above); a top module with no :data:`~warptap.icl_emit.SIB_MODULE_TYPE`
    instances at all; a SIB chain (flat or nested) that doesn't run cleanly from ``tdi`` to
    ``tdo``; an instrument instance not named per
    :data:`~warptap.icl_emit.INSTRUMENT_MODULE_PREFIX` convention; or a READ instrument with
    other than exactly one ``ScanRegister``. A hierarchy SIB (one whose ``fromSO`` resolves to
    another SIB instance rather than an instrument) is detected structurally and recursed into
    -- matching :mod:`warptap.icl_emit`'s own emission the other direction.
    """
    try:
        ijtag = icl_parser_module(top_module, [str(p) for p in icl_paths])
    except AssertionError as exc:
        raise IclImportError(
            "icl_parser's own retargeting-graph construction (IclRegisterModel) failed for "
            f"module {top_module!r} -- a real, unresolved issue in the vendored tool itself, "
            "confirmed to be triggered by any width>1 instrument regardless of instrument "
            "count or READ/WRITE direction (implementation_plan.md Stage 10/12); a network "
            "built entirely from width=1 instruments is confirmed to build cleanly"
        ) from exc
    except ValueError as exc:
        if "AccessLink" in str(exc):
            raise IclImportError(
                "icl_parser does not support AccessLink blocks at all (a real, confirmed gap "
                "in the vendored tool, not of the ICL itself -- see implementation_plan.md "
                "Stage 10); re-emit/re-author the file with include_access_link=False"
            ) from exc
        raise

    top = ijtag.icl_instance
    IclInstance, IclDataRegister, IclScanRegister = _icl_item_classes(icl_parser_module)

    children = top.get_icl_item_type(IclInstance)
    sib_instances = [c for c in children if c.get_module_scope() == SIB_MODULE_TYPE]
    if not sib_instances:
        raise IclImportError(
            f"no {SIB_MODULE_TYPE!r}-typed instances found in top module {top_module!r} -- "
            "this ICL file doesn't describe a warptap-shaped SIB network"
        )

    tdo_port = top.get_icl_item_name(TDO)
    tdo_source = _resolve_single(next(iter(tdo_port.sources.values())))
    remaining = list(sib_instances)
    chain_nodes = _walk_chain(
        remaining, top.get_icl_item_name(TDI), tdo_source, IclDataRegister, IclScanRegister
    )
    if remaining:
        raise IclImportError(
            f"{len(remaining)} of {len(sib_instances)} {SIB_MODULE_TYPE!r} instance(s) never "
            "chain back to tdi via a prior SIB's SO -- not a flat-or-nested, TDI-to-TDO SIB "
            "chain this importer recognizes"
        )

    graph = PhysicalGraph(chain=tuple(chain_nodes))
    instrument_children = _collect_instrument_children(chain_nodes)
    root = ModuleInstance(name=top_module, children=tuple(instrument_children))
    return graph, root
