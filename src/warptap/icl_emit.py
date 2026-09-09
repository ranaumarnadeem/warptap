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
from warptap.icl_model import (
    ChainSlot,
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    ScanMuxNode,
)
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

SCAN_MUX_MODULE_PREFIX = "warptap_scan_mux_"
"""Prefix every :class:`~warptap.icl_model.ScanMuxNode`'s own module type/instance name
carries (``warptap_scan_mux_<mux_name>``) -- unlike :data:`SIB_MODULE_TYPE` (one module type
shared by every SIB instance), each mux gets its OWN distinct module (arm count/select width/
values all vary per instance, mirroring :func:`render_instrument_module`'s own one-per-
distinct-instrument shape, and :mod:`warptap.sib_insert`'s own per-instance-imported-RTL-
module design for the identical reason -- see that module's own docstring). Exported for the
same reason as :data:`SIB_MODULE_TYPE`/:data:`INSTRUMENT_MODULE_PREFIX`: :mod:`warptap.
icl_import` recognizes this exact prefix walking a parsed network back in."""


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


def _slot_instance_name(slot: ChainSlot) -> str:
    """The ``Instance`` name a chain slot is referred to by, regardless of kind -- a SIB uses
    :func:`_sib_instance_name`; a :class:`~warptap.icl_model.ScanMuxNode` reuses its own module
    type name (see :func:`_render_scan_mux_instance`'s docstring for why that's correct: each
    mux module type is only ever instantiated once)."""
    if isinstance(slot, ScanMuxNode):
        return _scan_mux_module_type(slot.mux_name)
    return _sib_instance_name(slot.sib_name)


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


def _scan_mux_module_type(mux_name: str) -> str:
    return f"{SCAN_MUX_MODULE_PREFIX}{mux_name}"


def render_scan_mux_module(node: ScanMuxNode) -> str:
    """One ``Module`` block per distinct :class:`~warptap.icl_model.ScanMuxNode` -- unlike
    :func:`render_sib_module_type`'s single shared definition, a mux's own arm count/select
    width/values genuinely vary per instance, so this is synthesized fresh per node, mirroring
    :func:`render_instrument_module`'s own one-per-distinct-instrument shape (and
    :mod:`warptap.sib_insert`'s own per-instance-imported-RTL-module design, forced by the
    exact same underlying fact -- see that module's own docstring for the Yosys-side version
    of this same story).

    Directly generalizes :func:`render_sib_module_type`'s own real, confirmed shape (``SR``
    becomes the ``SELREG[W-1:0]`` register; the binary ``ScanMux SIBmux SelectedBy SR
    {1'b0:SI; 1'b1:fromSO;}`` becomes N value:fromArmK pairs) rather than inventing a new
    pattern. **A real, deliberate limitation shared with the SIB case**: no explicit "bypass"
    entry is listed for a select value matching no declared arm -- real ICL's own grammar has
    no wildcard/else clause, and the real vendored ``mux_3`` fixture examined while planning
    this feature leaves the exact same kind of value gaps unlisted (its own 4-bit select field
    only explicitly covers a subset of the 16 possible values) -- so this isn't a gap unique to
    warptap's own emitter, it's an inherent property of the real language. v1 also requires
    exactly one value per arm (:class:`~warptap.icl_model.ScanArm`'s own forward-compatible
    ``values`` field notwithstanding) -- re-validated here (belt-and-suspenders, not a new
    rule invented here; :class:`~warptap.icl_model.validate_physical_graph` already enforces
    it at construction time).

    Same topology-level scope boundary as the SIB case (see module docstring): ``toSELk``'s
    own ``LogicSignal`` (``(SELREG == value) & SEL``) does not attempt to re-encode
    ``rtl/scan_mux_cell.v``'s own ``arm_active``-vs-``arm_select`` before/after-this-edge
    distinction -- ICL has no vocabulary for that, matching exactly how the SIB module's own
    ``toSEL`` already elides ``nested_active`` vs ``nested_select``.

    **A real structural constraint, found only by live-validating against the vendored
    ``Honza255/icl_parser`` (not derivable from the grammar file alone)**: a ``host``
    ``ScanInterface`` may contain *at most one* ``ScanInPort`` and *exactly one*
    ``ScanOutPort`` (its own real "rule f1") -- it models exactly one scan-in/scan-out pair,
    never an N-way fan-in bundle. A single shared ``host`` interface listing all N
    ``fromArmK`` ports at once (this function's first-draft shape) fails that check as soon
    as N > 1. Fixed by declaring **N separate host interfaces** (``host0``..``hostN-1``), one
    per arm, each pairing exactly one ``fromArmK`` with the one shared ``toSI`` -- this isn't
    a workaround but the actually-correct idiom: the real spec's own rule (6.4.16-m, unchecked
    by this vendored tool but stated in its comments) already describes a module's interfaces
    as "uniquely selectable, with all others disabled," which is exactly a mux's own real
    semantics (only one arm is ever live at a time)."""
    module_name = _scan_mux_module_type(node.mux_name)
    width = node.select_width
    width_suffix = f"[{width - 1}:0]" if width > 1 else ""

    from_arm_lines: list[str] = []
    to_sel_port_lines: list[str] = []
    to_sel_logic_lines: list[str] = []
    mux_arm_clauses: list[str] = []
    host_interface_lines: list[str] = []
    for k, arm in enumerate(node.arms):
        if len(arm.values) != 1:
            raise IclEmitError(
                f"ScanMux {node.mux_name!r} arm {k} claims {len(arm.values)} values -- "
                "v1 requires exactly one value per arm"
            )
        value = arm.values[0]
        from_arm_lines.append(f"    ScanInPort fromArm{k};")
        to_sel_port_lines.append(f"    ToSelectPort toSEL{k} {{ Source toSelSignal{k}; }}")
        to_sel_logic_lines.append(
            f"    LogicSignal toSelSignal{k} {{ (SELREG == {width}'d{value}) & SEL; }}"
        )
        mux_arm_clauses.append(f"{width}'d{value} : fromArm{k}")
        host_interface_lines.append(
            f"    ScanInterface host{k} {{ Port fromArm{k}; Port toSI; Port toSEL{k}; }}"
        )

    return (
        f"Module {module_name} {{\n"
        "    ScanInPort SI;\n"
        "    SelectPort SEL;\n"
        f"    ScanOutPort SO{width_suffix} {{ Source SELREG{width_suffix}; }}\n"
        "    ScanInterface client { Port SI; Port SEL; Port SO; }\n"
        "\n"
        + "\n".join(from_arm_lines) + "\n"
        "    ScanOutPort toSI { Source SI; }\n"
        + "\n".join(to_sel_port_lines) + "\n"
        + "\n".join(host_interface_lines) + "\n"
        "\n"
        + "\n".join(to_sel_logic_lines) + "\n"
        "\n"
        f"    ScanRegister SELREG{width_suffix} {{\n"
        "        ScanInSource MUX;\n"
        f"        CaptureSource SELREG{width_suffix};\n"
        f"        ResetValue {width}'b0;\n"
        "    }\n"
        f"    ScanMux MUX SelectedBy SELREG{width_suffix} {{ " + "; ".join(mux_arm_clauses) + "; }\n"
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


def _render_scan_mux_instance(node: ScanMuxNode, prev_so: str) -> tuple:
    """One :class:`~warptap.icl_model.ScanMuxNode`'s own ``Instance ... Of
    warptap_scan_mux_<name>`` binds ``SI`` to ``prev_so`` and each ``fromArmK`` to that arm's
    own gated content -- a leaf instrument's own ``SO``, or (recursively) a nested sub-chain's
    own final ``SO`` -- directly generalizing :func:`_render_chain_instances`'s own SibNode
    ``fromSO`` binding to N arms instead of one. Like :func:`render_instrument_module`'s own
    module-type/instance-name reuse (each distinct instrument module is only ever instantiated
    once), the instance name here is the same string as the module type -- each mux gets its
    own distinct, uniquely-named module (:data:`SCAN_MUX_MODULE_PREFIX`), so there's exactly
    one instance of it. Returns ``(lines, final_so)``, the same shape
    :func:`_render_chain_instances` returns, so callers don't need to special-case a mux
    slot's own return value."""
    mux_instance = _scan_mux_module_type(node.mux_name)
    arm_bindings: List[str] = []
    arm_lines: List[str] = []
    for k, arm in enumerate(node.arms):
        if arm.instrument is None and not arm.nested:
            raise IclEmitError(
                f"ScanMux {node.mux_name!r} arm {k} has neither an instrument nor a "
                "nested network"
            )
        if arm.instrument is not None and arm.nested:
            raise IclEmitError(
                f"ScanMux {node.mux_name!r} arm {k} has both an instrument and a nested "
                "network -- exactly one of the two is allowed, never both"
            )
        if arm.nested:
            nested_lines, nested_final_so = _render_chain_instances(
                arm.nested, prev_so=f"{mux_instance}.toSI"
            )
            arm_bindings.append(f"InputPort fromArm{k} = {nested_final_so};")
            arm_lines.extend(nested_lines)
        else:
            if arm.instrument.width < 1:
                raise IclEmitError(
                    f"instrument {arm.instrument.name!r} (arm {k} of ScanMux "
                    f"{node.mux_name!r}) has width {arm.instrument.width} -- an "
                    "instrument needs at least 1 bit"
                )
            instr_instance = f"{INSTRUMENT_MODULE_PREFIX}{arm.instrument.name}"
            arm_bindings.append(f"InputPort fromArm{k} = {instr_instance}.SO;")
            arm_lines.append(
                f"Instance {instr_instance} Of {instr_instance} "
                f"{{ InputPort SI = {mux_instance}.toSI; }}"
            )
    lines = [
        f"Instance {mux_instance} Of {mux_instance} {{ InputPort SI = {prev_so}; "
        + " ".join(arm_bindings)
        + " }"
    ]
    lines.extend(arm_lines)
    return lines, f"{mux_instance}.SO"


def _render_chain_instances(chain, prev_so: str) -> tuple:
    """One level of a chain: each slot's own ``Instance <sib> Of warptap_sib`` binds ``SI``
    to ``prev_so`` (the previous slot's ``SO``, or the caller's own entry point) and
    ``fromSO`` to whatever it gates -- a leaf instrument's own ``SO``, or (recursively) a
    nested sub-chain's own final ``SO``, mirroring exactly how :func:`~warptap.sib_insert.
    _insert_chain` threads ``prev_so``/wires a hierarchy slot's ``nested_so``. A
    :class:`~warptap.icl_model.ScanMuxNode` slot delegates to :func:`_render_scan_mux_instance`
    instead -- an N-arm generalization of the same ``fromSO`` idea, not a different shape.
    Returns ``(lines, final_so)`` -- ``final_so`` is this chain's own last slot's ``SO``
    reference, for the caller to thread as its own ``prev_so`` (top level) or bind into an
    enclosing SIB's own ``fromSO`` (nested)."""
    lines: List[str] = []
    for node in chain:
        if isinstance(node, ScanMuxNode):
            mux_lines, prev_so = _render_scan_mux_instance(node, prev_so)
            lines.extend(mux_lines)
            continue

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
    """Recursively walk a chain (and any nested sub-chains, including inside a
    :class:`~warptap.icl_model.ScanMuxNode`'s own arms) collecting each distinct leaf
    instrument by name, first-seen order -- mirrors :func:`_render_chain_instances`'s own
    traversal shape."""
    for node in chain:
        if isinstance(node, ScanMuxNode):
            for arm in node.arms:
                if arm.nested:
                    _collect_instruments(arm.nested, seen)
                elif arm.instrument is not None and arm.instrument.name not in seen:
                    seen[arm.instrument.name] = arm.instrument
        elif node.nested:
            _collect_instruments(node.nested, seen)
        elif node.instrument is not None and node.instrument.name not in seen:
            seen[node.instrument.name] = node.instrument


def _collect_scan_muxes(chain, seen: dict) -> None:
    """Recursively walk a chain (and any nested sub-chains, including inside a
    :class:`~warptap.icl_model.ScanMuxNode`'s own arms) collecting each distinct
    :class:`~warptap.icl_model.ScanMuxNode` by name, first-seen order -- mirrors
    :func:`_collect_instruments`'s own traversal shape."""
    for node in chain:
        if isinstance(node, ScanMuxNode):
            if node.mux_name not in seen:
                seen[node.mux_name] = node
            for arm in node.arms:
                if arm.nested:
                    _collect_scan_muxes(arm.nested, seen)
        elif node.nested:
            _collect_scan_muxes(node.nested, seen)


def to_icl(
    graph: PhysicalGraph, root: ModuleInstance, *, include_access_link: bool = True
) -> str:
    """Render the complete ICL description of ``graph`` (from ``sib_plan.build_sib_plan``,
    ideally called with ``top_name`` set to the real target module's own name -- ``root.name``
    is used directly as this file's top ``Module`` name, no separate ``module_name`` parameter,
    since accepting the real name twice would only reintroduce a keep-two-values-in-sync risk
    ``sib_plan.build_sib_plan``'s own ``top_name`` fix was meant to remove).

    Emits: ``render_sib_module_type()`` once, one ``render_scan_mux_module()`` per distinct
    :class:`~warptap.icl_model.ScanMuxNode` (any depth/branch), one ``render_instrument_
    module()`` per distinct instrument, then the top module -- a ``ScanInterface`` naming the
    five real JTAG pins (:mod:`warptap.tap_ports`), the SIB/mux/instrument ``Instance``
    statements (``render_sib_instances``), and (when ``include_access_link``) a trailing
    ``AccessLink ... Of STD_1149_1_2001`` naming ``EXTEST`` (see module docstring for why).
    The ``wdr_select`` clause's shape (``ScanInterface { <slot>; }``, no ``ActiveSignals``)
    matches the confirmed real example directly (a real chip's ``MultiCoreAccessLink.icl``) --
    ``ActiveSignals`` there names the ``wir_select`` clause's own IR-decode signal, a genuinely
    different thing this v1 emitter doesn't need since it only ever describes one flat DR
    chain, not IR-based multi-core selection. The chain's first slot works here whether it's a
    SIB or a mux -- both module kinds declare the identically-shaped ``ScanInterface client
    { Port SI; Port SEL; Port SO; }``, so ``wdr_select``'s reference is agnostic to which.

    ``include_access_link`` defaults to ``True`` (real, grammatically valid ICL, useful to any
    consumer that implements it) but the vendored ``Honza255/icl_parser`` -- Stage 10's own
    live-validation oracle -- has a confirmed real gap: its own processor raises "Not
    supported" for `AccessLink` regardless of what's inside it (verified directly against the
    real library, not assumed). Callers validating against that specific tool should pass
    ``include_access_link=False``; this is a real limitation of that one external tool, not of
    the ICL this function emits, and is documented here rather than silently worked around.
    Raises :class:`IclEmitError` for anything v1 can't represent."""
    module_type_blocks = [render_sib_module_type()]
    seen_muxes: dict = {}
    _collect_scan_muxes(graph.chain, seen_muxes)
    for mux in seen_muxes.values():
        module_type_blocks.append(render_scan_mux_module(mux))
    seen_instruments: dict = {}
    _collect_instruments(graph.chain, seen_instruments)
    for instrument in seen_instruments.values():
        module_type_blocks.append(render_instrument_module(instrument))

    instance_lines = render_sib_instances(graph)
    first_slot = _slot_instance_name(graph.chain[0]) if graph.chain else None
    last_so = f"{_slot_instance_name(graph.chain[-1])}.SO" if graph.chain else TDI
    # A SibNode's own SO is always 1 bit (render_sib_module_type's SR), but a ScanMuxNode's
    # own SO carries its select_width (render_scan_mux_module's SO/SELREG pairing) -- tdo's
    # own declared width must match whatever it's bound to, the same same-module port_size==
    # source_size rule that forced render_scan_mux_module's own SO fix (see that function's
    # docstring), confirmed here by live-validating a mux sitting directly at the chain's tail
    # with no SIB wrapper (every other existing test only ever had a 1-bit SibNode there).
    last_so_width = (
        graph.chain[-1].select_width
        if graph.chain and isinstance(graph.chain[-1], ScanMuxNode)
        else 1
    )
    tdo_bits = f"[{last_so_width - 1}:0]" if last_so_width > 1 else ""

    top_lines = [
        f"Module {root.name} {{",
        f"    ScanInPort {TDI};",
        f"    ScanOutPort {TDO}{tdo_bits} {{ Source {last_so if graph.chain else TDI}; }}",
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
        if first_slot is not None:
            top_lines.append(f"        wdr_select {{ ScanInterface {{ {first_slot}; }} }}")
        top_lines.append("    }")
    top_lines.append("}")

    return "\n\n".join(module_type_blocks) + "\n\n" + "\n".join(top_lines) + "\n"
