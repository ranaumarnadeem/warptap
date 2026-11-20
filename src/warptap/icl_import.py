"""ICL import (implementation_plan.md §7 Stage 12): parses real ``.icl`` files entirely via
the vendored ``Honza255/icl_parser`` (the same submodule Stage 10's ``icl_emit.py`` validates
against), never touching raw ICL text directly -- extending this project's established
"shell out to a real independent tool, never round-trip through a self-written parser"
discipline to *parsing*, not just validation.

**Which copy of the parser**: warptap ships one inside its own package (:mod:`warptap._vendor`,
a byte-for-byte copy of the submodule's, checked by ``tests/test_vendored_icl_parser.py``), and
the importers use it unless given another ``Ijtag`` class. It needs only ANTLR's Python runtime
(the ``import`` extra): the importers never build the parser's z3-based retargeting model.

**What is recognized, by structure rather than by name**: the importer follows the scan path
backward from the top module's scan-out to its scan-in. Ports are only wires to it, so a module
boundary, including a wrapper instance around an element, changes nothing. On that path it
recognizes:

* a **SIB**: a 1-bit scan register fed by a 2-way ScanMux that the register itself selects,
  the ``0`` arm a straight bypass and the ``1`` arm the gated segment (the SIB_mux_pre shape,
  which :mod:`warptap.icl_emit` writes);
* a **ScanMux slot**: any other ScanMux whose output goes straight into its own select
  register (which may sit in another instance, as in an SCB), one select value per arm. Arms
  that hold nothing are bypass, as unclaimed values are in
  :class:`~warptap.icl_model.ScanMuxNode`;
* in a SIB's segment or a ScanMux arm: exactly one scan register (an instrument), or a chain
  of further SIBs and ScanMux slots.

Names are instance paths joined with ``__`` (``m1.sib2`` becomes ``m1__sib2``); a top-level
instance named with one of warptap's prefixes (:data:`~warptap.icl_emit.SIB_INSTANCE_PREFIX`,
:data:`~warptap.icl_emit.SCAN_MUX_MODULE_PREFIX`,
:data:`~warptap.icl_emit.INSTRUMENT_MODULE_PREFIX`) has it stripped, so warptap's own ICL
imports back to the names it was emitted from. A register is read from its scan-in end,
whichever way it is declared (``[0:w-1]`` or ``[w-1:0]``): the path must leave it from its last
declared bit. The result is checked with :func:`~warptap.icl_model.validate_physical_graph`.

Everything else is rejected with an :class:`IclImportError` whose message starts with one of
:data:`REJECTION_REASONS`, naming the construct: a scan register outside any SIB or ScanMux (an
always-on register: the model has no slot for one), several registers in series in one segment,
a SIB whose register comes before its mux (SIB_mux_post), a SIB that opens on 0, a ScanMux
selected by part of a register, by a register that isn't right after it, or by something that
isn't a scan register (an IR decode, the TAP, a port, logic), an arm selected by several
values, a path that isn't a tree, or ICL the parser rejects. ``tests/test_icl_import_itc16.py``
runs every network of the ITC'16 IJTAG benchmark set through this, and
``tests/test_icl_import_external_fixtures.py`` the vendored parser's own fixtures.

**Direction is detected structurally, not by name**: an instrument whose wrapper instance
holds a ``DataRegister`` anywhere inside it (with no wrapper: whose own module holds one) is
WRITE; otherwise it is READ, matching :mod:`warptap.icl_emit`'s ``DataRegister``-vs-
``ScanRegister`` distinction.

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
entry point.

**A real, important correction to the paragraph above, found via a real upstream maintainer's
own review** (``Honza255/icl_parser`` PR #1): the crash's actual trigger was narrower than
first diagnosed. Confirmed directly (checked out the vendored tool's own pre-fix commit and
tested both shapes against it): a width>1 *register* alone never crashes the retargeting-graph
build, as long as the ``ScanOutPort``/``ScanInPort`` touching it stays scalar (1 bit), which
is what every real ICL fixture in the tool's own corpus already does -- only warptap's own
*emitter*, at the time, additionally widened those scan ports to match the register's own
width (``icl_emit.py``'s own module docstring covers the fix), which is what actually
triggered the crash. So this fix's own value is now genuinely in question: it may never be
needed for any ICL this project emits going forward, now that :func:`~warptap.icl_emit.
render_instrument_module` no longer produces the non-standard shape that required it. Left
here, not rewritten, as an honest record of the original (over-broad) diagnosis -- see the
vendored fork's own PR #1 thread for how this played out with its maintainer. A separate,
independent performance bug in the same construction path --
``icl_process.py`` computing ``inspect.stack()[0][3]`` (a full stack capture with per-frame
source-file resolution) purely to log each parse handler's own already-known name -- made any
network large enough to be interesting impractically slow; replaced with the equivalent but
allocation-free ``sys._getframe().f_code.co_name`` (roughly 15x faster on the affected paths,
confirmed by profiling).

**A much larger, separate performance finding**: ``Ijtag(...)``'s constructor always built a
full Z3 SMT-solver-based retargeting-vector model (``IclRegisterModel``/``create_retageter``,
used for that tool's own ``iWrite``/``iRead``/``iApply`` vector generation) as an inseparable
part of construction, with no way to opt out through the tool's own original public API.
Profiling found this -- not the graph traversal above -- is the *real* scaling bottleneck:
roughly linear in instrument count, but a network of just 5 instruments at width 4 exceeded
five minutes, while the exact same shape completes in well under a second once this step is
skipped. :func:`import_icl` never uses ``ijtag.ijtag_reg_model``/``ijtag.icl_retargeter`` at
all -- only ``ijtag.icl_instance`` (the structural parse tree, fully built *before* this step
even starts) -- so this cost was always being paid for nothing. Fixed by adding an additive
``build_register_model: bool = True`` parameter to ``Ijtag.__init__`` (default preserves every
existing caller's behavior exactly, including ``icl_emit.py``'s own validation tests, which
deliberately want the full retargeting-graph build); :func:`import_icl` passes ``False``.
Confirmed empirically: a 100-instrument, width-16 network (1600 total bits, comfortably
industrial-scale) imports in ~7 seconds with this fix, versus not completing at all within five
minutes at a small fraction of that size beforehand.

**Parser failures become an** :class:`IclImportError` **too**, carrying the parser's own
message: a syntax error anywhere in a file (the vendored parser reads each file to its end and
raises on one; it used to stop without an error at the first construct it couldn't parse and
drop the rest of the file), a name it can't resolve, a failed ``assert`` in its own consistency
checks (the message names the file and line), and a construct it reads but doesn't elaborate
(:data:`NOT_ELABORATED`: an ``AccessLink``, a ``ClockMux``, a ``ToClockPort`` with a ``Source``,
an addressed instance, a broadcast, an instance or port source in a ``OneHotDataGroup``), none
of which warptap's model holds either. An ``AccessLink`` is only a problem where the network uses it: one in a module the top
doesn't instantiate is read and left alone.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path
from typing import List, NamedTuple, Tuple

from warptap import _vendor
from warptap.errors import WarptapError
from warptap.icl_emit import (
    INSTRUMENT_MODULE_PREFIX,
    ONE_HOT_GROUP_MODULE_PREFIX,
    SCAN_MUX_MODULE_PREFIX,
    SIB_INSTANCE_PREFIX,
)
from warptap.icl_model import (
    ICLModelError,
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    OneHotDataGroup,
    OneHotDataRegister,
    PhysicalGraph,
    ScanArm,
    ScanMuxNode,
    SibNode,
    validate_physical_graph,
)


class IclImportError(WarptapError):
    """Raised when the given ``.icl`` files don't describe a network warptap's model can hold,
    or the parser can't read them. A rejection's message starts with one of
    :data:`REJECTION_REASONS`; the other messages are for a top module the files don't define
    and a network that fails :func:`~warptap.icl_model.validate_physical_graph`."""


# Why a network can't be imported: a construct warptap's model has no place for, or ICL the
# parser rejects. Every rejection's message starts with one of these.
INVALID_ICL = "invalid ICL"
NO_SCAN_PATH = "no scan path from the top module's scan-in to its scan-out"
UNSUPPORTED_SCAN_ELEMENT = (
    "a scan path through something other than scan registers, ScanMuxes and scan ports"
)
PARTIAL_REGISTER = "a scan path that leaves a scan register before its last bit"
REGISTER_OUTSIDE_SLOT = "a scan register outside any SIB or ScanMux"
REGISTERS_IN_SERIES = "more than one scan register in one SIB segment or ScanMux arm"
SIB_REGISTER_BEFORE_MUX = "a SIB whose register comes before its ScanMux (SIB_mux_post)"
SIB_OPENS_ON_ZERO = "a SIB that opens on 0"
FOREIGN_SELECT = "a ScanMux selected by a register that is not its own select register"
NON_REGISTER_SELECT = "a ScanMux selected by something other than a scan register"
MULTI_VALUE_ARM = "a ScanMux arm selected by more than one value"
TOO_FEW_ARMS = "a ScanMux with fewer than two arms that hold anything"
NOT_A_TREE = "a scan path that is not a tree"
NOT_ELABORATED = "an ICL construct the vendored parser doesn't elaborate"

REJECTION_REASONS = (
    INVALID_ICL, NO_SCAN_PATH, UNSUPPORTED_SCAN_ELEMENT, PARTIAL_REGISTER, REGISTER_OUTSIDE_SLOT,
    REGISTERS_IN_SERIES, SIB_REGISTER_BEFORE_MUX, SIB_OPENS_ON_ZERO, FOREIGN_SELECT,
    NON_REGISTER_SELECT, MULTI_VALUE_ARM, TOO_FEW_ARMS, NOT_A_TREE, NOT_ELABORATED,
)
"""Every reason :func:`import_icl` rejects a network for, the start of each such message."""


class _Classes(NamedTuple):
    """The parser's item classes, taken from the module ``Ijtag`` is defined in (it re-exports
    them through its ``from .icl_process import *`` chain), so this works however the caller
    made that module importable."""

    instance: type
    scan_register: type
    scan_mux: type
    scan_in_port: type
    scan_out_port: type
    data_register: type
    data_in_port: type
    number: type

    @classmethod
    def of(cls, icl_parser_module) -> "_Classes":
        mod = sys.modules[icl_parser_module.__module__]
        return cls(
            mod.IclInstance, mod.IclScanRegister, mod.IclScanMux, mod.IclScanInPort,
            mod.IclScanOutPort, mod.IclDataRegister, mod.IclDataInPort, mod.IclNumber,
        )


_TOP_SCAN_IN = object()  # the top module's scan-in, where the scan path starts


class _PartialSelect(NamedTuple):
    """A ScanMux select that is only some of a scan register's bits."""

    register: object


class _Register(NamedTuple):
    """A scan register on the path, and the ports the path takes from it back to whatever
    precedes it."""

    register: object
    in_ports: tuple


class _Slot(NamedTuple):
    """A SIB or a ScanMux: a ScanMux whose own select ``register`` comes right after it on the
    path. ``arms`` holds ``(value, content)`` for each arm that holds anything, content being
    ``(element, out_ports)`` pairs, TDI side first; ``in_ports`` are the ports between the
    slot's start and its arms."""

    register: object
    mux: object
    is_sib: bool
    arms: tuple
    in_ports: tuple


def _flat(dotted: str) -> str:
    """An ICL hierarchical name as one identifier: ``m1.sc1`` -> ``m1__sc1``."""
    return dotted.replace(".", "__")


class _ScanPath:
    """The scan path of an elaborated network, from the top module's scan-out back to its
    scan-in, decomposed into the elements warptap's model holds. Ports are only wires here, so
    a module boundary, a wrapper instance included, changes nothing about the path."""

    def __init__(self, top, classes: _Classes):
        self.top = top
        self.c = classes
        scan_ins = top.get_icl_item_type(classes.scan_in_port)
        scan_outs = top.get_icl_item_type(classes.scan_out_port)
        if len(scan_ins) != 1 or len(scan_outs) != 1:
            raise IclImportError(
                f"{NO_SCAN_PATH}: the top module {top.get_name()!r} has {len(scan_ins)} scan-in "
                f"and {len(scan_outs)} scan-out port(s), not one of each"
            )
        self.scan_in, self.scan_out = scan_ins[0], scan_outs[0]
        self._elements: dict = {}
        self._busy: set = set()

    # -- following the path backward -------------------------------------------------------

    def _single(self, concat, where):
        signals = concat.concat_sigs
        if len(signals) != 1 or isinstance(signals[0], self.c.number):
            raise IclImportError(
                f"{UNSUPPORTED_SCAN_ELEMENT}: the scan path at {where.get_name_with_hier()!r} is "
                "a constant or a concatenation"
            )
        return signals[0]

    @staticmethod
    def _binding(instance, port_name: str):
        for connection in instance.connections:
            ((signal, concat),) = connection.items()
            if signal.get_name() == port_name:
                return concat
        return None

    def _check_scan_out_bit(self, register, bits) -> None:
        """A path leaves a register from its last declared bit: ``ScanInSource`` feeds the
        first, whichever way the register is declared (warptap writes ``[0:w-1]``, others
        ``[w-1:0]``)."""
        if bits and list(bits) != [register.get_all_indexes()[-1]]:
            raise IclImportError(
                f"{PARTIAL_REGISTER}: the path takes bit(s) {list(bits)} of "
                f"{register.get_name_with_hier()!r}, whose scan-out bit is "
                f"{register.get_all_indexes()[-1]}"
            )

    def _source(self, scope, signal, ports: list):
        """What drives ``signal`` (resolved in ``scope``): a scan register, a ScanMux or the top
        module's scan-in. The ports passed on the way are appended to ``ports``."""
        item = scope.get_icl_item_name(signal.get_relative_name())
        bits = signal.get_indexes()
        while True:
            if isinstance(item, self.c.scan_register):
                self._check_scan_out_bit(item, bits)
                return item
            if isinstance(item, self.c.scan_mux):
                return item
            if isinstance(item, self.c.scan_in_port):
                owner = item.get_instance()
                if owner is self.top:
                    if item is self.scan_in:
                        return _TOP_SCAN_IN
                    raise IclImportError(
                        f"{NO_SCAN_PATH}: the path reaches the top module's port "
                        f"{item.get_name()!r}, not its scan-in"
                    )
                ports.append(item)
                binding = self._binding(owner, item.get_name())
                if binding is None:
                    raise IclImportError(
                        f"{NO_SCAN_PATH}: nothing drives scan-in port "
                        f"{item.get_name_with_hier()!r}"
                    )
                signal = self._single(binding, item)
                item = binding.instance.get_icl_item_name(signal.get_relative_name())
                bits = signal.get_indexes()
                continue
            if isinstance(item, self.c.scan_out_port):
                ports.append(item)
                (concat,) = item.sources.values()  # a scan port is one bit
                signal = self._single(concat, item)
                item = concat.instance.get_icl_item_name(signal.get_relative_name())
                bits = signal.get_indexes()
                continue
            raise IclImportError(
                f"{UNSUPPORTED_SCAN_ELEMENT}: {type(item).__name__} {item.get_name_with_hier()!r}"
            )

    def _select_register(self, mux):
        """The scan register ``mux`` is selected by, followed through ports; a description of
        what it is instead if it is something else."""
        signals = mux.mux_control.concat_sigs
        if len(signals) != 1 or isinstance(signals[0], self.c.number):
            return "a concatenation or constant"
        scope, signal = mux.mux_control.instance, signals[0]
        item = scope.get_icl_item_name(signal.get_relative_name())
        bits = signal.get_indexes()
        for _ in range(64):
            if isinstance(item, self.c.scan_register):
                if bits and sorted(bits) != sorted(item.get_all_indexes()):
                    return _PartialSelect(item)
                return item
            if isinstance(item, self.c.data_in_port) and item.get_instance() is not self.top:
                binding = self._binding(item.get_instance(), item.get_name())
                if binding is None or len(binding.concat_sigs) != 1:
                    return f"{type(item).__name__} {item.get_name_with_hier()!r}"
                signal = binding.concat_sigs[0]
                if isinstance(signal, self.c.number):
                    return "a constant"
                item = binding.instance.get_icl_item_name(signal.get_relative_name())
                bits = signal.get_indexes()
                continue
            sources = getattr(item, "sources", None)
            if isinstance(sources, dict) and len(sources) >= 1:
                concats = list(sources.values())
                if len(concats) != 1 or len(concats[0].concat_sigs) != 1:
                    return f"{type(item).__name__} {item.get_name_with_hier()!r}"
                signal = concats[0].concat_sigs[0]
                if isinstance(signal, self.c.number):
                    return "a constant"
                item = concats[0].instance.get_icl_item_name(signal.get_relative_name())
                bits = signal.get_indexes() or bits
                continue
            return f"{type(item).__name__} {item.get_name_with_hier()!r}"
        return "a select that never reaches a register"

    # -- the walk, on a stack of its own ------------------------------------------------------
    #
    # What comes before an element is found by walking back from it, and a SIB's bypass arm
    # leads straight to the element before the SIB, so written as calls the walk would nest once
    # per element along a chain: 4 Python frames per top-level slot. Python 3.10 crashes, out of
    # C stack, at about 20000 frames (a few thousand top-level SIBs on Linux, fewer on Windows,
    # whose stack is smaller). So the walk is written as generators: where a step needs the
    # element that ends at a node, it yields the node, and _run computes that element on a stack
    # of its own and sends it back, in the order the calls would have run.

    def _run(self, steps):
        """The result of ``steps``. Each node a step yields is answered with the element ending
        at it (:meth:`_element_steps`), computed depth-first, as a call would be; an exception
        goes back through the steps waiting on it, as it would through calls."""
        stack = [steps]
        value, error = None, None
        while True:
            try:
                if error is None:
                    request = stack[-1].send(value)
                else:
                    request = stack[-1].throw(error)
            except StopIteration as done:
                stack.pop()
                if not stack:
                    return done.value
                value, error = done.value, None
                continue
            except Exception as exc:  # noqa: BLE001 -- handed to the step waiting on this one
                stack.pop()
                if not stack:
                    raise
                value, error = None, exc
                continue
            stack.append(self._element_steps(request))
            value, error = None, None

    def _element_steps(self, node):
        """The element of the path that ends at ``node``, the node before it, and the ports
        between the two."""
        key = id(node)
        if key in self._elements:
            return self._elements[key]
        if key in self._busy:
            raise IclImportError(
                f"{NOT_A_TREE}: the path runs in a cycle through {node.get_name_with_hier()!r}"
            )
        self._busy.add(key)
        try:
            if isinstance(node, self.c.scan_register):
                result = yield from self._register_element_steps(node)
            else:
                result = self._bare_mux(node)
        finally:
            self._busy.discard(key)
        self._elements[key] = result
        return result

    def _register_element_steps(self, register):
        ports: list = []
        source = self._source(register.get_instance(), register.scan_in, ports)
        if isinstance(source, self.c.scan_mux) and self._select_register(source) is register:
            return (yield from self._slot_steps(register, source))
        return _Register(register, tuple(ports)), source, tuple(ports)

    def _bare_mux(self, mux):
        """A ScanMux whose output doesn't go straight into its own select register."""
        select = self._select_register(mux)
        if isinstance(select, str):
            raise IclImportError(
                f"{NON_REGISTER_SELECT}: {mux.get_name_with_hier()!r} is selected by {select}"
            )
        if isinstance(select, _PartialSelect):
            raise IclImportError(
                f"{FOREIGN_SELECT}: {mux.get_name_with_hier()!r} is selected by part of scan "
                f"register {select.register.get_name_with_hier()!r}"
            )
        for _, concat in mux.mux_selects:
            signal = self._single(concat, mux)
            if self._source(concat.instance, signal, []) is select:
                raise IclImportError(
                    f"{SIB_REGISTER_BEFORE_MUX}: {select.get_name_with_hier()!r} feeds "
                    f"{mux.get_name_with_hier()!r}, which it selects"
                )
        raise IclImportError(
            f"{FOREIGN_SELECT}: {mux.get_name_with_hier()!r} is selected by "
            f"{select.get_name_with_hier()!r}"
        )

    def _fork_steps(self, nodes, mux):
        """Where the arms of ``mux`` start: the first node every arm's path reaches."""
        paths = [[node] for node in nodes]
        seen = [{id(node)} for node in nodes]
        while True:
            common = set.intersection(*seen)
            if common:
                return next(node for node in paths[0] if id(node) in common)
            moved = False
            for k, path in enumerate(paths):
                if path[-1] is _TOP_SCAN_IN:
                    continue
                _, previous, _ = yield path[-1]
                path.append(previous)
                seen[k].add(id(previous))
                moved = True
                if all(id(previous) in s for s in seen):
                    return previous
            if not moved:
                raise IclImportError(
                    f"{NOT_A_TREE}: the arms of {mux.get_name_with_hier()!r} never meet"
                )

    def _slot_steps(self, register, mux):
        raw = []
        for values, concat in mux.mux_selects:
            ports: list = []
            node = self._source(concat.instance, self._single(concat, mux), ports)
            raw.append(([v.get_number() for v in values], node, tuple(ports)))
        fork = yield from self._fork_steps([node for _, node, _ in raw], mux)
        arms, in_ports, first_ports = [], [], None
        for values, node, ports in raw:
            content, start_ports = yield from self._chain_steps(node, ports, fork)
            in_ports.extend(start_ports)
            if first_ports is None:
                first_ports = start_ports
            arms.append((values, content))
        held = [(values, content) for values, content in arms if content]
        all_values = sorted(v for values, _ in arms for v in values)
        name = f"{register.get_name_with_hier()!r} and {mux.get_name_with_hier()!r}"
        if register.get_vector_size() == 1 and len(arms) == 2 and all_values == [0, 1] and len(held) == 1:
            ((values, content),) = held
            if values != [1]:
                raise IclImportError(f"{SIB_OPENS_ON_ZERO}: {name}")
            slot = _Slot(register, mux, True, ((1, tuple(content)),), tuple(in_ports))
        else:
            for values, _ in held:
                if len(values) != 1:
                    raise IclImportError(f"{MULTI_VALUE_ARM}: {name}, values {values}")
            if len(held) < 2:
                raise IclImportError(f"{TOO_FEW_ARMS}: {name}")
            slot = _Slot(
                register, mux, False,
                tuple((values[0], tuple(content)) for values, content in held),
                tuple(in_ports),
            )
        return slot, fork, first_ports

    def _chain_steps(self, node, ports, stop):
        """The elements between ``stop`` and ``node`` (``node`` reached through ``ports``), TDI
        side first, each paired with the ports from it to what follows it; and the ports
        between ``stop`` and the first element."""
        elements = []
        out_ports = tuple(ports)
        while node is not stop:
            if node is _TOP_SCAN_IN:
                raise IclImportError(
                    f"{NOT_A_TREE}: a segment runs back to the top module's scan-in without "
                    "meeting the start of its SIB or ScanMux"
                )
            element, previous, previous_ports = yield node
            elements.append((element, out_ports))
            node, out_ports = previous, previous_ports
        elements.reverse()
        return elements, out_ports

    def network(self):
        """The top-level elements, TDI side first."""
        (concat,) = self.scan_out.sources.values()
        ports: list = []
        node = self._source(concat.instance, self._single(concat, self.scan_out), ports)
        elements, _ = self._run(self._chain_steps(node, ports, _TOP_SCAN_IN))
        return elements


class _Builder:
    """Turns a decomposed scan path into warptap's model, naming each slot and instrument."""

    def __init__(self, path: _ScanPath):
        self.path = path
        self.c = path.c
        self.claimed: set = set()
        self.instruments: List[InstrumentNode] = []

    def _claim(self, item) -> None:
        if id(item) in self.claimed:
            raise IclImportError(
                f"{NOT_A_TREE}: {item.get_name_with_hier()!r} is on more than one scan path"
            )
        self.claimed.add(id(item))

    def _wrapper(self, out_ports, in_ports):
        """The outermost instance the path enters through its scan-in and leaves through its
        scan-out, around this element and nothing else."""
        outs = {
            id(p.get_instance()): p.get_instance()
            for p in out_ports
            if isinstance(p, self.c.scan_out_port)
        }
        ins = {id(p.get_instance()) for p in in_ports if isinstance(p, self.c.scan_in_port)}
        candidates = [i for k, i in outs.items() if k in ins and i is not self.path.top]
        if not candidates:
            return None
        return min(candidates, key=lambda i: i.get_hier().count("."))

    @staticmethod
    def _name(wrapper, item, prefix: str) -> str:
        if wrapper is None:
            return _flat(item.get_name_with_hier())
        hier = wrapper.get_hier()
        if "." not in hier and hier.startswith(prefix):
            return hier[len(prefix):]
        return _flat(hier)

    def _has_data_register(self, instance) -> bool:
        if instance.get_icl_item_type(self.c.data_register):
            return True
        return any(self._has_data_register(i) for i in instance.get_icl_item_type(self.c.instance))

    def chain(self, elements, where: str) -> tuple:
        slots = []
        for element, out_ports in elements:
            if isinstance(element, _Register):
                raise IclImportError(
                    f"{REGISTER_OUTSIDE_SLOT}: {element.register.get_name_with_hier()!r} sits "
                    f"{where}"
                )
            slots.append(self.slot(element, out_ports))
        return tuple(slots)

    def _gated(self, content, owner: str):
        registers = [e for e, _ in content if isinstance(e, _Register)]
        if len(content) == 1 and registers:
            return self.instrument(content[0]), ()
        if registers:
            names = ", ".join(repr(e.register.get_name_with_hier()) for e in registers)
            reason = REGISTERS_IN_SERIES if len(registers) == len(content) else REGISTER_OUTSIDE_SLOT
            raise IclImportError(f"{reason}: {names} in {owner}")
        return None, self.chain(content, f"in {owner}")

    def slot(self, slot: _Slot, out_ports):
        self._claim(slot.register)
        self._claim(slot.mux)
        wrapper = self._wrapper(out_ports, slot.in_ports)
        if slot.is_sib:
            name = self._name(wrapper, slot.register, SIB_INSTANCE_PREFIX)
            ((_, content),) = slot.arms
            instrument, nested = self._gated(content, f"SIB {name!r}")
            return SibNode(sib_name=name, instrument=instrument, nested=nested)
        name = self._name(wrapper, slot.mux, SCAN_MUX_MODULE_PREFIX)
        arms = []
        for value, content in slot.arms:
            instrument, nested = self._gated(content, f"ScanMux {name!r} arm {value}")
            arms.append(ScanArm(values=(value,), instrument=instrument, nested=nested))
        return ScanMuxNode(
            mux_name=name, select_width=slot.register.get_vector_size(), arms=tuple(arms)
        )

    def instrument(self, pair) -> InstrumentNode:
        element, out_ports = pair
        register = element.register
        self._claim(register)
        wrapper = self._wrapper(out_ports, element.in_ports)
        name = self._name(wrapper, register, INSTRUMENT_MODULE_PREFIX)
        owner = wrapper if wrapper is not None else register.get_instance()
        writes = (
            self._has_data_register(wrapper)
            if wrapper is not None
            else bool(owner.get_icl_item_type(self.c.data_register))
        )
        node = InstrumentNode(
            name=name,
            width=register.get_vector_size(),
            capture_value=0,
            direction=InstrumentDirection.WRITE if writes else InstrumentDirection.READ,
        )
        self.instruments.append(node)
        return node


def _collect_instrument_children(chain) -> List[ModuleInstance]:
    """Recursively walk a chain (and any nested sub-chains, including inside a
    :class:`~warptap.icl_model.ScanMuxNode`'s own arms) collecting each leaf instrument as a
    flat :class:`~warptap.icl_model.ModuleInstance` sibling -- mirrors
    :mod:`warptap.sib_plan`'s own flat ``ModuleInstance`` tree regardless of SIB-nesting depth,
    the direction ``icl_emit.py``'s own ``_collect_instruments`` walks."""
    children: List[ModuleInstance] = []
    for node in chain:
        if isinstance(node, ScanMuxNode):
            for arm in node.arms:
                if arm.nested:
                    children.extend(_collect_instrument_children(arm.nested))
                else:
                    children.append(ModuleInstance(name=arm.instrument.name))
        elif node.nested:
            children.extend(_collect_instrument_children(node.nested))
        else:
            children.append(ModuleInstance(name=node.instrument.name))
    return children


def _construct_ijtag(icl_parser_module, top_module: str, icl_paths: List[Path]):
    """``icl_parser_module(top_module, icl_paths, build_register_model=False)``, with the
    parser's exceptions translated into an :class:`IclImportError` carrying its message: an
    ``AssertionError`` from one of its own consistency checks (named by file and line), a
    ``ValueError`` (a syntax error, an unresolved name, or a construct it doesn't elaborate), a
    ``KeyError`` (``Ijtag`` looks modules up in a plain dict, so a ``top_module`` the files
    don't define, or a module or parameter one of them uses but none defines, surfaces as
    one, and so does a parse-tree node its processor has no handler for), and the bare
    ``Exception`` its few remaining checks raise. Shared by both import entry points
    (:func:`import_icl` and :func:`import_one_hot_data_group`), which both need
    ``build_register_model=False``: a OneHotDataGroup is scan-free, which the register model
    can't build, and for a scan network it is only a cost (see module docstring).

    The parser prints its progress and every item it checks to stdout; that output is
    discarded, so importing a network prints nothing."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return icl_parser_module(
                top_module, [str(p) for p in icl_paths], build_register_model=False
            )
    except AssertionError as exc:
        frame = exc.__traceback__
        while frame.tb_next is not None:
            frame = frame.tb_next
        where = f"{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}"
        detail = f": {exc}" if str(exc) else ""
        raise IclImportError(
            f"{INVALID_ICL}: the vendored parser's own consistency check (an assert at {where}) "
            f"failed for module {top_module!r}{detail}"
        ) from exc
    except ValueError as exc:
        message = str(exc)
        construct = _not_elaborated(message)
        if construct is not None:
            hint = (
                " (warptap's own ICL imports when emitted with include_access_link=False)"
                if construct == "AccessLink"
                else ""
            )
            shown = message if len(message) <= 200 else message[:200] + "..."
            raise IclImportError(
                f"{NOT_ELABORATED}: {construct}{hint}. The parser said: {shown}"
            ) from exc
        raise IclImportError(f"{INVALID_ICL}: {message}") from exc
    except KeyError as exc:
        missing = exc.args[0] if exc.args else None
        if hasattr(missing, "getRuleIndex"):
            # A parse-tree node the processor stores no result for: a construct it parses but
            # has no handler for (a ToClockPort's Source, for one).
            rule = type(missing).__name__
            rule = rule[: -len("Context")] if rule.endswith("Context") else rule
            raise IclImportError(
                f"{NOT_ELABORATED}: {rule} (the parser has no handler for it), in "
                f"{missing.getText()[:120]!r}"
            ) from exc
        scope, _, name = top_module.rpartition("::")
        if missing in (top_module, name, scope or "root"):
            raise IclImportError(
                f"no module {top_module!r} found in {[str(p) for p in icl_paths]!r}"
            ) from exc
        raise IclImportError(
            f"{INVALID_ICL}: the vendored parser found no definition of {missing!r} while "
            f"elaborating {top_module!r}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        if type(exc) is not Exception:
            raise
        # The parser raises a bare Exception for a few checks of its own: a name defined twice
        # in a module, a port split into overlapping or gapped parts.
        raise IclImportError(f"{INVALID_ICL}: {exc}") from exc


_NOT_SUPPORTED = (
    re.compile(r"^Not supported -> exit(\w+?)_def -> "),  # AccessLink, ClockMux
    re.compile(r"^(Broadcast|Instance address|Instance|Post source) not supported in: "),
)


def _not_elaborated(message: str):
    """The construct a parser ``ValueError`` says it doesn't elaborate, or ``None`` if the
    message is about something else."""
    for pattern in _NOT_SUPPORTED:
        match = pattern.match(message)
        if match:
            return match.group(1)
    return None


def import_icl(
    icl_paths: List[Path], top_module: str, *, icl_parser_module=None
) -> Tuple[PhysicalGraph, ModuleInstance]:
    """Parse ``icl_paths`` with ``icl_parser_module``, elaborate ``top_module``, and convert its
    scan network into warptap's :class:`~warptap.icl_model.PhysicalGraph`, plus a flat
    :class:`~warptap.icl_model.ModuleInstance` for the top module with one child per
    instrument.

    ``top_module`` is named the way the parser scopes it: ``"chip"``, or ``"q12710::M0"`` for a
    module declared in a namespace. ``icl_paths`` must hold every file the network's modules
    are defined in.

    ``icl_parser_module`` is an ``icl_parser`` ``Ijtag`` class. It defaults to the copy warptap
    ships (:mod:`warptap._vendor`), which needs the ``import`` extra
    (``pip install "warptap[import]"``); pass another, such as a development checkout's, to
    parse with that instead, as ``tests/conftest.py``'s ``icl_parser_module`` fixture does.

    The module docstring says what is recognized. Anything else raises
    :class:`IclImportError`, its message starting with the matching entry of
    :data:`REJECTION_REASONS`.
    """
    if icl_parser_module is None:
        icl_parser_module = _vendor.ijtag("import_icl")
    ijtag = _construct_ijtag(icl_parser_module, top_module, icl_paths)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 20000))  # one level of SIB/ScanMux nesting takes a few frames
    try:
        path = _ScanPath(ijtag.icl_instance, _Classes.of(icl_parser_module))
        builder = _Builder(path)
        chain = builder.chain(path.network(), "in the top module's chain")
    except ValueError as exc:  # a name the parser can't resolve
        raise IclImportError(f"{INVALID_ICL}: {exc}") from exc
    finally:
        sys.setrecursionlimit(limit)
    graph = PhysicalGraph(chain=chain)
    try:
        validate_physical_graph(graph)
    except ICLModelError as exc:
        raise IclImportError(f"the imported network breaks warptap's model: {exc}") from exc
    root = ModuleInstance(
        name=top_module.rpartition("::")[2],
        children=tuple(_collect_instrument_children(chain)),
    )
    return graph, root


def _one_hot_item_classes(icl_parser_module):
    """``IclOneHotDataGroup``/``IclDataRegister``, taken from the module ``Ijtag`` is defined
    in, as :meth:`_Classes.of` does (``Ijtag``'s ``from .icl_process import *`` chain
    re-exports them)."""
    mod = sys.modules[icl_parser_module.__module__]
    return mod.IclOneHotDataGroup, mod.IclDataRegister


def import_one_hot_data_group(
    icl_paths: List[Path], module_name: str, *, icl_parser_module=None
) -> OneHotDataGroup:
    """Parse ``icl_paths`` and recover the single :class:`~warptap.icl_model.OneHotDataGroup`
    declared in ``module_name`` -- the mirror-image recognition path to
    :func:`~warptap.icl_emit.render_one_hot_data_group_module`. ``icl_parser_module`` defaults
    to the vendored parser, as for :func:`import_icl`.

    Returns a single :class:`~warptap.icl_model.OneHotDataGroup`, not a tuple of them, even
    though the plan that originally sketched this function's signature drafted a plural
    ``Tuple[OneHotDataGroup, ...]`` return -- that draft predates Phase 0's own empirical
    confirmation (sub-step 8) that exactly one ``OneHotDataGroup`` per ``Module`` is a hard v1
    constraint, not just a convention (a second group's own same-typed bus ports would
    silently concatenate into the first's via the real checker's type-keyed port lookup, see
    :class:`~warptap.icl_model.OneHotDataGroup`'s own docstring). A singular return type keeps
    this function's own shape consistent with that confirmed constraint, and mirrors
    :func:`import_icl`'s own "one named module in, one warptap object out" shape exactly.

    Uses ``build_register_model=False`` unconditionally (not just for performance the way
    :func:`import_icl` uses it) -- a ``OneHotDataGroup`` module is scan-free by construction
    (no ``ScanInPort``/``ScanOutPort`` at all), and the real vendored tool's own retargeting-
    graph build crashes unconditionally for any scan-free module (Phase 0 sub-step 0's own
    confirmed finding, an ``AssertionError`` from ``icl_retargeting.py``'s own
    ``_add_one_hot_and_ir_chain``).

    Recovers ``group.name`` from ``module_name`` itself via
    :data:`~warptap.icl_emit.ONE_HOT_GROUP_MODULE_PREFIX`, not from the ``OneHotDataGroup``
    construct's own internal name (the two happen to hold the same value in anything this
    project's own emitter produces, but recovering it from the module name mirrors
    :func:`_mux_name_of`/:func:`_sib_name_of`'s own established convention exactly). Recovers
    ``data_width`` from the ``OneHotDataGroup`` item's own ``get_vector_size()`` (its own
    declared ``[hi:lo]``, always present regardless of which bus ports the group actually
    declares) and ``address_width`` from the ``ADDR`` port's ``get_vector_size()`` directly
    (warptap's own canonical port name, matching :func:`import_icl`'s own "hardcode warptap's
    own naming convention, recognize nothing else" discipline for ``SI``/``SO``/``tdi``/
    ``tdo``). ``writable``/``readable`` are read off the *first* recovered register's own
    ``is_writable()``/``is_readable()`` -- a confirmed group-level property in real ICL (see
    :class:`~warptap.icl_model.OneHotDataGroup`'s own docstring), so every register in one
    module agrees by construction; no redundant per-register consistency check is needed here.
    Registers come back sorted by ``address`` for a deterministic round trip, since parse order
    isn't a documented guarantee of the vendored tool's own ``get_icl_item_type``.

    ``reset_value``/``write_signal_bits``/``read_signal_bits`` always come back ``None``/``()``
    -- the same real, permanent round-trip limitation :func:`import_icl` already documents for
    an instrument's ``signal_bits``: ``ResetValue`` is parsed but never stored by the vendored
    tool at all (Phase 0 finding #4), and no per-register ``Source`` clause exists in real ICL
    for a host-net binding to be recovered from in the first place.

    Raises :class:`IclImportError` for: a real ``icl_parser`` gap (same two as
    :func:`import_icl`); ``module_name`` not prefixed with
    :data:`~warptap.icl_emit.ONE_HOT_GROUP_MODULE_PREFIX`; zero or more-than-one
    ``OneHotDataGroup`` items found (the latter a defensive check against hand-authored input --
    warptap's own emitter never produces it, see the docstring paragraph above); zero
    ``DataRegister`` items; a group that's neither writable nor readable -- none of these three
    can happen for anything this project's own emitter produces (:func:`~warptap.icl_emit.
    render_one_hot_data_group_module`'s own preconditions already forbid them), but this
    importer accepts arbitrary ``.icl`` input, matching :func:`import_icl`'s own "recognize
    warptap's own canonical shape, reject anything else" discipline.
    """
    if icl_parser_module is None:
        icl_parser_module = _vendor.ijtag("import_one_hot_data_group")
    ijtag = _construct_ijtag(icl_parser_module, module_name, icl_paths)
    top = ijtag.icl_instance
    IclOneHotDataGroup, IclDataRegister = _one_hot_item_classes(icl_parser_module)

    if not module_name.startswith(ONE_HOT_GROUP_MODULE_PREFIX):
        raise IclImportError(
            f"module {module_name!r} doesn't start with {ONE_HOT_GROUP_MODULE_PREFIX!r} -- "
            "this importer only recovers a OneHotDataGroup's original name from warptap's own "
            "naming convention"
        )
    group_name = module_name[len(ONE_HOT_GROUP_MODULE_PREFIX) :]

    groups = top.get_icl_item_type(IclOneHotDataGroup)
    if len(groups) != 1:
        raise IclImportError(
            f"module {module_name!r} has {len(groups)} OneHotDataGroup item(s), expected "
            "exactly 1 -- not a shape this importer recognizes (real ICL resolves "
            "AddressPort/WriteEnPort/etc. by type across the whole module, so a second "
            "group's own ports would silently concatenate into the first's; see "
            "render_one_hot_data_group_module's own docstring)"
        )
    data_width = groups[0].get_vector_size()
    address_width = top.get_icl_item_name("ADDR").get_vector_size()

    icl_registers = top.get_icl_item_type(IclDataRegister)
    if not icl_registers:
        raise IclImportError(
            f"OneHotDataGroup {group_name!r} in module {module_name!r} has no DataRegister "
            "items -- not a shape this importer recognizes"
        )

    writable = icl_registers[0].is_writable() is True
    readable = icl_registers[0].is_readable() is True
    if not (writable or readable):
        raise IclImportError(
            f"OneHotDataGroup {group_name!r} in module {module_name!r} is neither writable "
            "nor readable -- not a shape this importer recognizes"
        )

    registers = tuple(
        sorted(
            (
                OneHotDataRegister(
                    name=reg.get_name(),
                    address=reg.get_reg_address(),
                    width=reg.get_vector_size(),
                )
                for reg in icl_registers
            ),
            key=lambda reg: reg.address,
        )
    )
    return OneHotDataGroup(
        name=group_name,
        address_width=address_width,
        data_width=data_width,
        registers=registers,
        writable=writable,
        readable=readable,
    )
