"""ICL (Instrument Connectivity Language) text emitter (implementation_plan.md §7 Stage 10).
Extends the "stateless pretty-printer over one model" family SVF/STAPL already established
(:mod:`warptap.tap_ir_svf`, :mod:`warptap.tap_ir_stapl`) up one abstraction level: those two
render a *scan-pattern* (:mod:`warptap.tap_ir` ops); this one renders *network topology*
(:class:`~warptap.icl_model.PhysicalGraph`/:class:`~warptap.icl_model.ModuleInstance`) --
a genuinely different input shape, hence its own error-class family (``IclEmitError``, not
``TapIr*Error``).

**Grammar, confirmed this session against three independently-authored real implementations**
(an ANTLR4 grammar, a Ruby/Treetop grammar, and a C++ tool's real fixture files, all agreeing):
``Module <name> { ... }`` is brace-delimited with **no ``endmodule`` keyword** -- unlike every
Verilog-family text this project has emitted so far, an easy wrong-instinct default to avoid.
Port declarations (``ScanInPort``/``ScanOutPort``/``SelectPort``/``CaptureEnPort``/etc.) are
grouped into named ``ScanInterface { Port ...; }`` blocks. ``ScanMux <name> SelectedBy <expr>
{ <value> : <signal>; ... }`` selects among named scan sources.

**A SIB has no dedicated ICL keyword at all -- it's a design pattern**, confirmed real (IEEE
1687-2014 Appendix E example code, found reproduced near-verbatim in four independent
open-source repos this session): a ``ScanRegister`` whose ``ScanInSource`` is a ``ScanMux``
gated by the register's own committed value:

    ScanRegister SR { ScanInSource SIBmux; CaptureSource SR; ResetValue 1'b0; }
    ScanMux SIBmux SelectedBy SR { 1'b0 : SI; 1'b1 : fromSO; }

This maps directly onto ``rtl/sib_cell.v``'s own ``shift_ff``/``po``/``select`` shape: ``SR``
is ``po``, the ``ScanMux`` is exactly ``shift_ff <= po ? nested_so : si``. **One real,
deliberate divergence from the textbook pattern**: the real ``SIB_mux_pre`` example also
relays ``CaptureEn``/``ShiftEn``/``UpdateEn``/``Reset``/``TCK`` through per-instance
``To<X>Port`` ports, supporting arbitrary nesting depth. warptap's actual RTL doesn't do this
at all -- ``sib_cell.v`` has no such relay ports, and ``sib_insert.py`` wires
``capture_dr``/``shift_dr``/``update_dr``/``tck``/``trst_n`` identically to *every* cell in
the network directly from ``tap_core``, with no per-SIB relay. This emitter renders that
honestly: one shared top-level ``ScanInterface`` binds those five control signals to every SIB
and instrument module instance directly, rather than fabricating relay ports v1's flat network
(implementation_plan.md §7 Stage 4's own scoping decision) has no use for and warptap's own
RTL doesn't build.

**Another real, deliberate scope boundary, stated loudly rather than papered over**: ICL
describes scan-chain *topology* -- which module's ``SI`` binds to which module's ``SO``, and
which value of a ``ScanMux``'s select source chooses which arm -- not RTL-level clock-edge
timing hazards. ``rtl/sib_cell.v``'s real ``nested_select`` output is ``po & shift_ff &
select`` (implementation_plan.md §7 Stage 9's own hard-won fix: gates a WRITE instrument's
update-latch commit so it only ever fires on an edge that leaves its SIB open both before
*and* after, never one that opens or closes it -- see :mod:`warptap.sib_insert`'s module
docstring for the full story). ICL has no vocabulary for "before and after this specific
clock edge" -- only for a register's settled value. This emitter therefore renders the
textbook, ICL-idiomatic ``SR & SEL`` form (matching the real ``SIB_mux_pre`` example's own
``LogicSignal toSel_SR_SEL { SR[0] & SEL; }`` exactly) for describing *which* segment a SIB's
host-side select signal reaches -- correct at the topology level ICL operates on -- and does
NOT attempt to re-encode the RTL-level timing refinement in ICL text. That refinement is
proven correct by this project's own RTL cross-simulation (:mod:`warptap.sib_model`), a
separate, lower-level concern than what a network-topology description language is for.

**Real ``Instrument`` module shape, confirmed real** (same Appendix E source): ``DataInPort``/
``DataOutPort { Source ...; }``, and a ``ScanRegister`` with ``WriteDataSource``/
``WriteEnSource`` for a real write-target register. Maps directly onto
``rtl/instrument_write.v``'s real (non-stub) update latch.

**Chain wiring has no dedicated topology syntax** -- confirmed real: plain module
``Instance <name> Of <ModuleType> { InputPort SI = <prevInstance>.SO; ... }`` statements, each
SIB's ``SI`` explicitly bound to the previous slot's ``SO`` (or the top module's own ``TDI``
for the first slot) -- mirrors :mod:`warptap.sib_insert`'s own ``prev_so`` threading exactly.

**``AccessLink ... Of STD_1149_1_2001`` binds the SIB chain to the real TAP**, confirmed real.
v1 names ``EXTEST`` specifically: ``rtl/tap_core.v`` presents ``external_dr_tdo`` (where the
SIB chain plugs in) at ``tdo`` for any instruction that decodes to neither ``IDCODE`` nor
``BYPASS`` -- i.e. ``EXTEST`` or ``SAMPLE_PRELOAD`` are electrically identical paths, but only
``EXTEST`` is named here, matching every existing cross-sim test's own
``_select_extest_ops()`` convention; ``SAMPLE_PRELOAD``'s identical access is a known,
undocumented-in-ICL simplification, not an oversight.

**No confirmed real ICL mechanism exists for a READ instrument's fixed ``capture_value``
stub** (a warptap-internal testing artifact with no real hardware counterpart) -- this
emitter does not fabricate a plausible-looking ``CaptureSource`` for one; it emits an
explanatory comment instead, matching this project's "document limitations loudly rather than
invent something that looks plausible" discipline (e.g. Stage 4's closed-instrument
free-running limitation, Stage 7's STAPL ``COMPARE`` scope note before it was resolved).

**Live validation status against the vendored ``Honza255/icl_parser`` (this session's
findings, not assumed)**: this emitter's output was iteratively corrected against several
real, previously-unknown requirements of that tool's real grammar/checker -- every
``ScanInterface`` must contain a classifiable port (a ``TMSPort``/``ToTMSPort``,
``ShiftEnPort``/``SelectPort``, or ``ToShiftEnPort``/``ToSelectPort``, confirmed by a real,
named error the tool raises otherwise); a bare TAP-level reset must be a ``TRSTPort``, not a
generic ``ResetPort``; ``WriteDataSource``/``WriteEnSource`` belong to ``DataRegister``, never
``ScanRegister`` (this project's own first attempt got this wrong, corrected against the
tool's real grammar file, not by re-reading research notes); and a leaf instrument module's
scan ports need no ``ScanInterface`` wrapper at all. With those fixed, **every module this
emitter produces parses and passes the tool's own structural/semantic checks cleanly** --
proving grammar and structural correctness independently of this project's own code, the same
role OpenOCD/the Jam STAPL Player already play for SVF/STAPL (implementation_plan.md §7 Stage
7). The tool's own deeper retargeting-vector computation (``IclRegisterModel``, used for
``iWrite``/``iRead``/``iApply``-vector generation) used to hit a real internal
``AssertionError`` against any width>1 instrument's ``DataRegister``+``ScanRegister`` pairing --
three separate bit-serial-scan-port assumptions in the vendored tool itself, root-caused and
fixed directly in ``third_party/icl_parser`` (see :mod:`warptap.icl_import`'s module docstring
for the full diagnosis). Stage 10 ships with both grammar/structural AND retargeting-graph live
validation now passing for every network shape this emitter produces, width>1 included.
"""

from __future__ import annotations

from typing import List

from warptap.errors import WarptapError
from warptap.icl_model import InstrumentDirection, InstrumentNode, ModuleInstance, PhysicalGraph
from warptap.tap_ports import TCK, TDI, TDO, TMS, TRST_N

SIB_MODULE_TYPE = "warptap_sib"
"""The one fixed ICL module-type name every SIB instance is ``Instance ... Of`` -- exported
(not module-private) because :mod:`warptap.icl_import` (Stage 12) needs the exact same string
to recognize a SIB instance when walking a parsed network back in, the other direction."""

INSTRUMENT_MODULE_PREFIX = "warptap_instr_"
"""Prefix every instrument module type/instance name carries (``warptap_instr_<name>``) --
exported for the same reason as :data:`SIB_MODULE_TYPE`: :mod:`warptap.icl_import` strips it
back off an instrument instance's own name to recover the original instrument name."""

SIB_INSTANCE_PREFIX = "warptap_"
"""Prefix every SIB *instance* name carries (``warptap_<sib_name>``, via
:func:`_sib_instance_name`) -- distinct from :data:`SIB_MODULE_TYPE` (the shared *module type*
every SIB instance is ``Of``, not a per-instance name). Exported so
:mod:`warptap.icl_import` strips the exact same prefix back off, rather than re-deriving a
second literal that could silently drift out of sync with this one."""


class IclEmitError(WarptapError):
    """Raised when a :class:`~warptap.icl_model.PhysicalGraph` asks for something this ICL
    emitter can't represent -- a slot with neither an instrument nor a nested network (or
    both at once; this re-validates the same invariant
    :class:`~warptap.icl_model.validate_physical_graph`/
    :class:`~warptap.sib_insert.SibInsertError` enforce elsewhere, since a
    ``PhysicalGraph`` can be built directly, e.g. in tests, without ever calling
    ``insert_sib_network``), a zero-width instrument, or a WRITE instrument with no
    ``signal_bits`` -- rather than silently emitting something that looks plausible but
    describes nothing real."""


def _sib_instance_name(sib_name: str) -> str:
    return f"{SIB_INSTANCE_PREFIX}{sib_name}"


def render_sib_module_type() -> str:
    """The one canonical ``Module`` block for warptap's SIB primitive -- a single hand-authored
    definition, instantiated once per SIB via ``Instance ... Of`` (below), mirroring
    :mod:`warptap.sib_insert`'s own "hand-author once, clone per instance" RTL precedent
    reapplied here in ICL text. See this module's own docstring for the exact real-source
    grounding and the one deliberate divergence (no per-instance CaptureEn/ShiftEn/UpdateEn/
    Reset/TCK relay ports -- v1's flat network has no use for them).

    ``SelectPort SEL``/``ToSelectPort toSEL`` ARE kept (unlike the omitted CE/SE/UE/RST/TCK
    relay ports) -- not merely for RTL fidelity but because they're structurally required:
    validated directly against the vendored ``Honza255/icl_parser`` (Stage 10's own live
    oracle), which raises a real, named error ("cannot determine type... interface must
    contain a TMSPort/ToTMSPort, ShiftEnPort/SelectPort, or ToShiftEnPort/ToSelectPort") for
    any ``ScanInterface`` lacking one -- confirmed empirically this session, not assumed from
    the grammar alone."""
    return (
        f"Module {SIB_MODULE_TYPE} {{\n"
        "    ScanInPort SI;\n"
        "    SelectPort SEL;\n"
        "    ScanOutPort SO { Source SR; }\n"
        "    ScanInterface client { Port SI; Port SEL; Port SO; }\n"
        "\n"
        "    ScanInPort fromSO;\n"
        "    ScanOutPort toSI { Source SI; }\n"
        "    // Structural (topology-level) select signal only -- see module docstring's\n"
        "    // \"real, deliberate scope boundary\" note: does not re-encode\n"
        "    // rtl/sib_cell.v's real po & shift_ff & select update-latch-commit timing.\n"
        "    LogicSignal toSelSignal { SR & SEL; }\n"
        "    ToSelectPort toSEL { Source toSelSignal; }\n"
        "    ScanInterface host { Port fromSO; Port toSI; Port toSEL; }\n"
        "\n"
        "    ScanRegister SR { ScanInSource SIBmux; CaptureSource SR; ResetValue 1'b0; }\n"
        "    ScanMux SIBmux SelectedBy SR { 1'b0 : SI; 1'b1 : fromSO; }\n"
        "}"
    )


def _render_aliases(aliases, register_name: str) -> list:
    """One ``Alias <name>[hi:0] = <register_name>[abs_hi:abs_lo];`` per declared alias --
    real ICL rebases the alias's OWN declared range to start at 0 regardless of the source's
    absolute bit positions (confirmed real: a real fixture's own ``Alias mode[3:0] =
    DI[6:5],DI[3:2];`` declares a 0-based ``[3:0]`` LHS over non-0-based RHS source bits), and
    uses a bare name/index with no range at all for a 1-bit alias (that same real fixture's
    own ``Alias okay = DO[0];``)."""
    lines = []
    for alias in aliases:
        width = alias.high_bit - alias.low_bit + 1
        if width == 1:
            lines.append(f"    Alias {alias.name} = {register_name}[{alias.low_bit}];")
        else:
            lines.append(
                f"    Alias {alias.name}[{width - 1}:0] = "
                f"{register_name}[{alias.high_bit}:{alias.low_bit}];"
            )
    return lines


def render_instrument_module(instrument: InstrumentNode) -> str:
    """One ``Module`` block per distinct instrument, instantiated once (v1: exactly one
    instance per instrument, matching :mod:`warptap.sib_plan`'s own one-SIB-per-instrument
    scope). READ (``rtl/bc1_shift_only.v`` shape, no update latch): a plain
    ``ScanRegister{ScanInSource SI;}`` -- ``CaptureSource`` bound to the real host net when
    ``signal_bits`` is given, or replaced by an explanatory comment for a fixed-value stub
    (see module docstring). WRITE (``rtl/instrument_write.v`` shape, a real update latch): the
    real ``Instrument``+``DataOutPort`` pattern -- two separate registers, matching the real
    IEEE 1687-2014 Appendix E example's own split (confirmed directly against the vendored
    ``Honza255/icl_parser``'s real grammar this session: ``WriteDataSource``/``WriteEnSource``
    belong to ``DataRegister``, NOT ``ScanRegister``, which has no such attributes at all --
    an assumption this emitter got wrong on the first pass and corrected against the real
    tool, not by re-reading the grammar file alone): a ``ScanRegister`` (``SR``) is the actual
    shift-chain element (``ScanInSource``/self-``CaptureSource``, mirroring
    ``instrument_write.v``'s own ``shift_ff``); a ``DataRegister`` (``DR``) holds the
    committed functional value, its ``WriteDataSource`` bound directly to ``SR`` (the real
    Appendix E example instead routes this through an explicit ``DataInPort``, but nothing in
    the confirmed grammar requires that indirection for a same-module signal, and the direct
    form validated cleanly against the real tool) and drives ``DataOutPort DO``.

    Raises :class:`IclEmitError` for a WRITE instrument with no ``signal_bits`` -- nothing
    real for it to drive, the same precondition :mod:`warptap.sib_insert` itself enforces.

    Any declared ``instrument.aliases`` (Stage 15) render as real ``Alias`` declarations
    (:func:`_render_aliases`), referencing ``SR`` for a WRITE instrument -- the scan-composed
    register ``PDLInterpreter.iWrite``'s pending value actually lands in, per
    ``pdl_interpreter.py``'s own field-addressing design -- or ``DR`` for a READ instrument,
    matching each direction's existing register-naming exactly."""
    name = instrument.name
    module_name = f"{INSTRUMENT_MODULE_PREFIX}{name}"
    width = instrument.width

    if instrument.direction is InstrumentDirection.WRITE:
        if not instrument.signal_bits:
            raise IclEmitError(
                f"instrument {name!r} is a WRITE instrument with no signal_bits -- nothing "
                "real for it to drive, so there is nothing honest to emit as its DataOutPort"
            )
        bits = f"[{width - 1}:0]"
        reset_value = f"{width}'b0"
        drive_comment = (
            "    // DataOutPort DO drives real host port bit(s): "
            + ", ".join(f"{b.port_name}[{b.bit}]" for b in instrument.signal_bits)
            + "\n"
        )
        alias_lines = _render_aliases(instrument.aliases, "SR")
        alias_block = ("\n" + "\n".join(alias_lines) + "\n") if alias_lines else ""
        return (
            f"Module {module_name} {{\n"
            f"{drive_comment}"
            f"    DataOutPort DO{bits} {{ Source DR{bits}; }}\n"
            f"    ScanInPort SI{bits};\n"
            f"    ScanOutPort SO{bits} {{ Source SR{bits}; }}\n"
            "\n"
            f"    ScanRegister SR{bits} {{\n"
            f"        ScanInSource SI{bits};\n"
            f"        CaptureSource SR{bits};  // self-capture: mirrors instrument_write.v's\n"
            "                                   // own shift_ff <= po read-back-what-was-\n"
            "                                   // last-committed behavior\n"
            f"        ResetValue {reset_value};\n"
            "    }\n"
            f"    DataRegister DR{bits} {{\n"
            f"        WriteDataSource SR{bits};\n"
            "        WriteEnSource 1'b1;  // rtl/instrument_write.v commits unconditionally\n"
            "                              // once selected -- see rtl/instrument_write.v's\n"
            "                              // own module docstring for the real select gate.\n"
            f"        ResetValue {reset_value};\n"
            "    }\n"
            f"{alias_block}"
            "}"
        )

    # READ (rtl/bc1_shift_only.v shape): capture + shift only, no update latch.
    if instrument.signal_bits:
        capture_source = f"DR[{width - 1}:0]"
        binding_comment = (
            f"    // CaptureSource fans out from real host port bit(s): "
            + ", ".join(f"{b.port_name}[{b.bit}]" for b in instrument.signal_bits)
            + "\n"
        )
    else:
        capture_source = None
        binding_comment = (
            f"    // warptap: fixed stub capture_value={instrument.capture_value:#x} -- no\n"
            "    // real CaptureSource; ICL has no confirmed mechanism for a constant capture\n"
            "    // source, so none is fabricated here (see module docstring).\n"
        )

    lines = [
        f"Module {module_name} {{",
        f"    ScanInPort SI[{width - 1}:0];",
        f"    ScanOutPort SO[{width - 1}:0] {{ Source DR[{width - 1}:0]; }}",
        "",
        binding_comment.rstrip("\n"),
        f"    ScanRegister DR[{width - 1}:0] {{",
        f"        ScanInSource SI[{width - 1}:0];",
    ]
    if capture_source is not None:
        lines.append(f"        CaptureSource {capture_source};")
    lines.append(f"        ResetValue {width}'b0;")
    lines.append("    }")
    lines.extend(_render_aliases(instrument.aliases, "DR"))
    lines.append("}")
    return "\n".join(lines)


def _render_chain_instances(chain, prev_so: str) -> tuple:
    """One level of a chain: each slot's own ``Instance <sib> Of warptap_sib`` binds ``SI``
    to ``prev_so`` (the previous slot's ``SO``, or the caller's own entry point) and
    ``fromSO`` to whatever it gates -- a leaf instrument's own ``SO``, or (recursively) a
    nested sub-chain's own final ``SO``, mirroring exactly how :func:`~warptap.sib_insert.
    _insert_chain` threads ``prev_so``/wires a hierarchy slot's ``nested_so``. Returns
    ``(lines, final_so)`` -- ``final_so`` is this chain's own last slot's ``SO`` reference,
    for the caller to thread as its own ``prev_so`` (top level) or bind into an enclosing
    SIB's own ``fromSO`` (nested)."""
    lines: List[str] = []
    for node in chain:
        if node.instrument is None and not node.nested:
            raise IclEmitError(
                f"SIB {node.sib_name!r} has neither an instrument nor a nested network"
            )
        if node.instrument is not None and node.nested:
            raise IclEmitError(
                f"SIB {node.sib_name!r} has both an instrument and a nested network -- "
                "exactly one of the two is allowed, never both"
            )

        sib_instance = _sib_instance_name(node.sib_name)
        if node.nested:
            nested_lines, nested_final_so = _render_chain_instances(
                node.nested, prev_so=f"{sib_instance}.toSI"
            )
            lines.append(
                f"Instance {sib_instance} Of {SIB_MODULE_TYPE} {{ InputPort SI = {prev_so}; "
                f"InputPort fromSO = {nested_final_so}; }}"
            )
            lines.extend(nested_lines)
        else:
            if node.instrument.width < 1:
                raise IclEmitError(
                    f"instrument {node.instrument.name!r} (gated by {node.sib_name!r}) has "
                    f"width {node.instrument.width} -- an instrument needs at least 1 bit"
                )
            instr_instance = f"{INSTRUMENT_MODULE_PREFIX}{node.instrument.name}"
            lines.append(
                f"Instance {sib_instance} Of {SIB_MODULE_TYPE} {{ InputPort SI = {prev_so}; "
                f"InputPort fromSO = {instr_instance}.SO; }}"
            )
            lines.append(
                f"Instance {instr_instance} Of {instr_instance} "
                f"{{ InputPort SI = {sib_instance}.toSI; }}"
            )
        prev_so = f"{sib_instance}.SO"
    return lines, prev_so


def render_sib_instances(graph: PhysicalGraph) -> List[str]:
    """One ``Instance <sib> Of warptap_sib { InputPort SI = <prev>.SO; }`` per slot in
    ``graph.chain`` (and, recursively, in any nested sub-chain -- see
    :func:`_render_chain_instances`), TDI-side-first -- ``SI`` bound to the previous slot's
    ``SO``, or to the top module's own ``TDI`` for the first slot, mirroring
    :mod:`warptap.sib_insert`'s own ``prev_so`` threading exactly. Each leaf instrument gets
    its own ``Instance <instr> Of warptap_instr_<name> { InputPort SI = <sib>.toSI; }``,
    nested behind its gating SIB's own host-side scan-out."""
    lines, _final_so = _render_chain_instances(graph.chain, prev_so=TDI)
    return lines


def _collect_instruments(chain, seen: dict) -> None:
    """Recursively walk a chain (and any nested sub-chains) collecting each distinct leaf
    instrument by name, first-seen order -- mirrors :func:`_render_chain_instances`'s own
    traversal shape."""
    for node in chain:
        if node.nested:
            _collect_instruments(node.nested, seen)
        elif node.instrument is not None and node.instrument.name not in seen:
            seen[node.instrument.name] = node.instrument


def to_icl(
    graph: PhysicalGraph, root: ModuleInstance, *, include_access_link: bool = True
) -> str:
    """Render the complete ICL description of ``graph`` (from ``sib_plan.build_sib_plan``,
    ideally called with ``top_name`` set to the real target module's own name -- ``root.name``
    is used directly as this file's top ``Module`` name, no separate ``module_name`` parameter,
    since accepting the real name twice would only reintroduce a keep-two-values-in-sync risk
    ``sib_plan.build_sib_plan``'s own ``top_name`` fix was meant to remove).

    Emits: ``render_sib_module_type()`` once, one ``render_instrument_module()`` per distinct
    instrument, then the top module -- a ``ScanInterface`` naming the five real JTAG pins
    (:mod:`warptap.tap_ports`), the SIB/instrument ``Instance`` statements
    (``render_sib_instances``), and (when ``include_access_link``) a trailing
    ``AccessLink ... Of STD_1149_1_2001`` naming ``EXTEST`` (see module docstring for why).
    The ``wdr_select`` clause's shape (``ScanInterface { <sib>; }``, no ``ActiveSignals``)
    matches the confirmed real example directly (a real chip's ``MultiCoreAccessLink.icl``) --
    ``ActiveSignals`` there names the ``wir_select`` clause's own IR-decode signal, a genuinely
    different thing this v1 emitter doesn't need since it only ever describes one flat DR
    chain, not IR-based multi-core selection.

    ``include_access_link`` defaults to ``True`` (real, grammatically valid ICL, useful to any
    consumer that implements it) but the vendored ``Honza255/icl_parser`` -- Stage 10's own
    live-validation oracle -- has a confirmed real gap: its own processor raises "Not
    supported" for `AccessLink` regardless of what's inside it (verified directly against the
    real library, not assumed). Callers validating against that specific tool should pass
    ``include_access_link=False``; this is a real limitation of that one external tool, not of
    the ICL this function emits, and is documented here rather than silently worked around.
    Raises :class:`IclEmitError` for anything v1 can't represent."""
    module_type_blocks = [render_sib_module_type()]
    seen_instruments: dict = {}
    _collect_instruments(graph.chain, seen_instruments)
    for instrument in seen_instruments.values():
        module_type_blocks.append(render_instrument_module(instrument))

    instance_lines = render_sib_instances(graph)
    first_sib = _sib_instance_name(graph.chain[0].sib_name) if graph.chain else None
    last_so = f"{_sib_instance_name(graph.chain[-1].sib_name)}.SO" if graph.chain else TDI

    top_lines = [
        f"Module {root.name} {{",
        f"    ScanInPort {TDI};",
        f"    ScanOutPort {TDO} {{ Source {last_so if graph.chain else TDI}; }}",
        f"    TCKPort {TCK};",
        f"    TMSPort {TMS};",
        f"    TRSTPort {TRST_N};",
        "    ScanInterface tap { "
        + " ".join(f"Port {p};" for p in (TDI, TDO, TCK, TMS, TRST_N))
        + " }",
        "",
    ]
    top_lines.extend(f"    {line}" for line in instance_lines)
    if include_access_link:
        top_lines.append("")
        top_lines.append("    AccessLink warptap_tap Of STD_1149_1_2001 {")
        top_lines.append(f"        BSDLEntity {root.name};")
        if first_sib is not None:
            top_lines.append(f"        wdr_select {{ ScanInterface {{ {first_sib}; }} }}")
        top_lines.append("    }")
    top_lines.append("}")

    return "\n\n".join(module_type_blocks) + "\n\n" + "\n".join(top_lines) + "\n"
