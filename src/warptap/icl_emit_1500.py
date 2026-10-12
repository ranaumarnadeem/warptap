"""ICL (IEEE 1687) for an IEEE 1500 wrapper and its wrapper serial port, from a
:class:`~warptap.wsp1500_model.Wrapper1500`; and for a chain of wrappers on a SoC.

A wrapper is one ICL module, ``<name>``, beside a stub of its core (``<name>_core``,
whose outputs the wrapper's output cells capture):

- ``ScanInterface WSP`` -- WSI, WSO, ShiftWR, CaptureWR, UpdateWR, SelectWIR (a
  ``SelectPort``), WRCK and WRSTN (``ResetPort``, active low) -- and ``ScanInterface WPP``,
  the wrapper's own scan access: one ``Chain`` per wrapper chain, with the wrapper scan
  enable.
- The WIR: ``ScanRegister WIR_SR`` (WSI in, the shift stage) and ``DataRegister WIR`` (the
  update stage: ``WriteDataSource WIR_SR``, written while SelectWIR is selected, reset to
  WS_BYPASS), whose values ``Enum <name>_ops`` names.
- WBY, a one-bit ``ScanRegister`` that captures 0; one ``ScanRegister WBR<k>`` per wrapper
  chain, the head cell the MSB and the tail the LSB, whose capture a ``DataMux`` on the
  WIR chooses -- a serial instruction's cells capture their port (an input cell) or the
  core's output (an output cell) unless the mode holds them -- and whose scan input
  ``serial`` selects: WSI (chain 0) or the chain before, in a serial instruction, else the
  chain's own scan port.
- WSO: ``WsoMux``, the WIR's last bit while SelectWIR is 1, else ``WdrMux``: the last
  chain's tail in a serial instruction, else WBY.
- The core's inputs and the wrapper's outputs through a ``DataMux`` on the WIR: 0 where
  the instruction's mode makes the cell safe, the cell's flop where it holds, else the
  functional signal.

The scan path of a ``ScanRegister`` enters its MSB and leaves its LSB (IEEE 1687, and
the vendored parser). A wrapper's WIR shifts its code in last bit first, the first bit
shifted in ending nearest WSO, so the value ICL gives an instruction is its code with its
bits reversed -- ``iWrite WIR WS_EXTEST`` shifts in the same bits the code's last bit
first does.

Attributes carry the model (``warptap_*``), so :mod:`warptap.icl_import_1500` rebuilds
it exactly; the rest is standard ICL, which the vendored ``icl_parser`` checks.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional, Sequence

from warptap.wsp1500_model import (
    INPUT,
    Instruction,
    WbrCell,
    Wrapper1500,
)

FORMAT = "warptap-1500-icl-v1"
ROLES = ("wsi", "wso", "shiftwr", "capturewr", "updatewr", "selectwir", "wrck", "wrstn")
# The ICL words a port or net must not be named, which ANTLR reserves for the grammar.
_KEYWORDS = frozenset(
    """Module Enum Instance Of InputPort Source Enable Port Chain Attribute Alias
    ScanInPort ScanOutPort ShiftEnPort CaptureEnPort UpdateEnPort SelectPort ResetPort
    TCKPort TMSPort TRSTPort ClockPort DataInPort DataOutPort ScanRegister DataRegister
    LogicSignal ScanMux DataMux ClockMux ScanInterface AccessLink SelectedBy RefEnum
    ResetValue CaptureSource ScanInSource WriteEnSource WriteDataSource ActivePolarity
    NameSpace UseNameSpace Parameter LocalParameter""".split()
)


class Icl1500Error(ValueError):
    """A wrapper that can't be described as ICL."""


def icl_literal(code: int, bits: int) -> str:
    """An instruction code as the value ICL gives the WIR: the code's bits reversed
    (see the module docstring)."""
    return f"{bits}'b" + format(code, f"0{bits}b")[::-1]


def icl_name(label: str, taken: set[str]) -> str:
    """``label`` as an ICL identifier, unique among ``taken`` (which it joins): a bus
    bit ``d[3]`` is ``d_3``; a word ICL reserves, or one that starts with a digit, gets
    ``p_`` in front."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", label).strip("_") or "p"
    if name in _KEYWORDS or not name[0].isalpha():
        name = "p_" + name
    if name in taken:
        raise Icl1500Error(f"two ports would be named {name!r} in ICL (one is {label!r})")
    taken.add(name)
    return name


class _Names:
    """The ICL names of a wrapper's cells, ports and registers."""

    def __init__(self, wrapper: Wrapper1500) -> None:
        ports = wrapper.ports
        self.taken = {
            ports.wsi, ports.wso, ports.shiftwr, ports.capturewr, ports.updatewr,
            ports.selectwir, ports.wrck, ports.wrstn, ports.scan_enable,
            "WSP", "WPP", "WIR", "WIR_SR", "WBY", "WsoMux", "WdrMux", "serial", "core",
        }
        for k in range(len(wrapper.chains)):
            self.taken |= {
                ports.si(k), ports.so(k), f"WBR{k}", f"WBR{k}_in", f"Cap{k}",
            }
        self.cell: dict[tuple[int, int], str] = {}
        for k, chain in enumerate(wrapper.chains):
            for i, cell in enumerate(chain):
                self.cell[(k, i)] = icl_name(cell.label, self.taken)


def _concat(parts: Sequence[str]) -> str:
    return ", ".join(parts)


def _codes(wrapper: Wrapper1500, pick) -> list[str]:
    """The ICL literals of every code ``pick(instruction)`` is true of, where an unused
    code is WS_BYPASS."""
    found = []
    for code in range(1 << wrapper.ir_bits):
        if pick(wrapper.instruction(wrapper.instruction_of(code))):
            found.append(icl_literal(code, wrapper.ir_bits))
    return found


def _arm(literals: Sequence[str], source: str) -> str:
    return f"        {' | '.join(literals)} : {source};"


def _hold_safe(side: str, ins: Instruction) -> tuple[int, int]:
    intest, extest = ins.mode
    return (intest, extest) if side == INPUT else (extest, intest)


def _wrapper_module(wrapper: Wrapper1500) -> list[str]:
    names = _Names(wrapper)
    ports = wrapper.ports
    bits = wrapper.ir_bits
    if not wrapper.chains or any(not chain for chain in wrapper.chains):
        raise Icl1500Error(f"{wrapper.name}: a wrapper needs cells in every chain")
    serial = [i for i in wrapper.instructions if i.serial]
    lines = [f"Module {wrapper.name}_core {{"]
    for k, chain in enumerate(wrapper.chains):
        for i, cell in enumerate(chain):
            name = names.cell[(k, i)]
            if cell.side == INPUT:
                lines.append(f"    DataInPort {name};")
            else:
                # The core is a stub: an output nothing in the wrapper can know.
                lines.append(f"    DataOutPort {name} {{ Source 1'b0; }}")
    lines += ["}", "", f"Module {wrapper.name} {{"]

    def ref(k: int, i: int) -> str:
        n = len(wrapper.chains[k])
        return f"WBR{k}" if n == 1 else f"WBR{k}[{n - 1 - i}]"

    def tail(k: int) -> str:
        return f"WBR{k}" if len(wrapper.chains[k]) == 1 else f"WBR{k}[0]"

    # The model, for the importer.
    lines.append(f"    Attribute warptap_format = \"{FORMAT}\";")
    lines.append(f"    Attribute warptap_ir_bits = {bits};")
    for n, ins in enumerate(wrapper.instructions):
        lines.append(
            f"    Attribute warptap_instr_{n} = "
            f"\"{ins.name},{ins.code},{int(ins.serial)},{ins.mode[0]},{ins.mode[1]}\";"
        )
    for k, chain in enumerate(wrapper.chains):
        for i, cell in enumerate(chain):
            lines.append(
                f"    Attribute warptap_cell_{k}_{i} = \"{cell.side},{cell.label}\";"
            )
    role_names = ",".join(getattr(ports, role) for role in ROLES)
    chain_names = ",".join(
        f"{ports.si(k)}:{ports.so(k)}" for k in range(len(wrapper.chains))
    )
    lines.append(f"    Attribute warptap_ports = \"{role_names},{ports.scan_enable}\";")
    lines.append(f"    Attribute warptap_chain_ports = \"{chain_names}\";")
    lines.append("")
    lines.append(f"    Enum {wrapper.name}_ops {{")
    for ins in wrapper.instructions:
        lines.append(f"        {ins.name} = {icl_literal(ins.code, bits)};")
    lines.append("    }")
    lines += [
        f"    ScanInPort {ports.wsi};",
        f"    ScanOutPort {ports.wso} {{ Source WsoMux; }}",
        f"    ShiftEnPort {ports.shiftwr};",
        f"    CaptureEnPort {ports.capturewr};",
        f"    UpdateEnPort {ports.updatewr};",
        f"    SelectPort {ports.selectwir};",
        f"    TCKPort {ports.wrck};",
        f"    ResetPort {ports.wrstn} {{ ActivePolarity 0; }}",
        "    ScanInterface WSP { "
        + " ".join(f"Port {getattr(ports, role)};" for role in ROLES)
        + " }",
        "",
    ]
    wpp = []
    for k in range(len(wrapper.chains)):
        lines.append(f"    ScanInPort {ports.si(k)};")
        lines.append(f"    ScanOutPort {ports.so(k)} {{ Source {tail(k)}; }}")
        wpp.append(f"Chain c{k} {{ Port {ports.si(k)}; Port {ports.so(k)}; }}")
    lines.append(f"    ShiftEnPort {ports.scan_enable};")
    lines.append(
        "    ScanInterface WPP { "
        + " ".join(wpp)
        + f" Port {ports.scan_enable}; Port {ports.wrck}; Port {ports.wrstn}; }}"
    )
    lines.append("")

    # The ports of the wrapped block, and its core.
    for k, chain in enumerate(wrapper.chains):
        for i, cell in enumerate(chain):
            name = names.cell[(k, i)]
            if cell.side == INPUT:
                lines.append(f"    DataInPort {name};")
            else:
                lines.append(f"    DataOutPort {name} {{ Source Out_{name}; }}")
    connections = []
    for k, chain in enumerate(wrapper.chains):
        for i, cell in enumerate(chain):
            name = names.cell[(k, i)]
            if cell.side == INPUT:
                connections.append(f"InputPort {name} = In_{name};")
    lines.append(f"    Instance core Of {wrapper.name}_core {{ {' '.join(connections)} }}")
    lines.append("")

    # The registers.
    lines.append(f"    ScanRegister WIR_SR[{bits - 1}:0] {{ ScanInSource {ports.wsi}; }}")
    lines += [
        f"    DataRegister WIR[{bits - 1}:0] {{",
        "        WriteDataSource WIR_SR;",
        f"        WriteEnSource {ports.selectwir};",
        f"        ResetValue {bits}'b{'0' * bits};",
        f"        RefEnum {wrapper.name}_ops;",
        "    }",
        f"    ScanRegister WBY {{ ScanInSource {ports.wsi}; CaptureSource 1'b0; "
        "ResetValue 1'b0; }",
    ]
    if serial:
        expr = " || ".join(f"WIR == {icl_literal(i.code, bits)}" for i in serial)
        lines.append(f"    LogicSignal serial {{ {expr}; }}")
    rest = _codes(wrapper, lambda i: not i.serial)
    for k, chain in enumerate(wrapper.chains):
        n = len(chain)
        width = f"[{n - 1}:0]" if n > 1 else ""
        if serial:
            before = ports.wsi if k == 0 else tail(k - 1)
            lines += [
                f"    ScanMux WBR{k}_in SelectedBy serial {{",
                f"        1'b1 : {before};",
                f"        1'b0 : {ports.si(k)};",
                "    }",
                f"    DataMux Cap{k}{width} SelectedBy WIR {{",
            ]
            for ins in serial:
                parts = []
                for i, cell in enumerate(chain):
                    hold, _ = _hold_safe(cell.side, ins)
                    name = names.cell[(k, i)]
                    parts.append(
                        ref(k, i)
                        if hold
                        else (name if cell.side == INPUT else f"core.{name}")
                    )
                lines.append(_arm([icl_literal(ins.code, bits)], _concat(parts)))
            lines.append(_arm(rest, f"WBR{k}"))
            lines.append("    }")
            lines.append(
                f"    ScanRegister WBR{k}{width} {{ ScanInSource WBR{k}_in; "
                f"CaptureSource Cap{k}; }}"
            )
        else:
            lines.append(
                f"    ScanRegister WBR{k}{width} {{ ScanInSource {ports.si(k)}; }}"
            )
    last = len(wrapper.chains) - 1
    if serial:
        lines += [
            "    ScanMux WdrMux SelectedBy serial {",
            f"        1'b1 : {tail(last)};",
            "        1'b0 : WBY;",
            "    }",
        ]
        wdr = "WdrMux"
    else:
        wdr = "WBY"
    lines += [
        f"    ScanMux WsoMux SelectedBy {ports.selectwir} {{",
        "        1'b1 : WIR_SR[0];",
        f"        1'b0 : {wdr};",
        "    }",
        "",
    ]

    # The cells' functional paths: 0 where the mode is safe, the flop where it holds.
    for k, chain in enumerate(wrapper.chains):
        for i, cell in enumerate(chain):
            name = names.cell[(k, i)]
            safe = _codes(wrapper, lambda ins: _hold_safe(cell.side, ins)[1] == 1)
            hold = _codes(
                wrapper,
                lambda ins: _hold_safe(cell.side, ins)[1] == 0
                and _hold_safe(cell.side, ins)[0] == 1,
            )
            free = _codes(
                wrapper, lambda ins: _hold_safe(cell.side, ins) == (0, 0)
            )
            functional = name if cell.side == INPUT else f"core.{name}"
            mux = f"In_{name}" if cell.side == INPUT else f"Out_{name}"
            lines.append(f"    DataMux {mux} SelectedBy WIR {{")
            if safe:
                lines.append(_arm(safe, "1'b0"))
            if hold:
                lines.append(_arm(hold, ref(k, i)))
            if free:
                lines.append(_arm(free, functional))
            lines.append("    }")
    lines.append("}")
    return lines


def to_icl_1500(wrapper: Wrapper1500) -> str:
    """The ICL of one wrapper (see the module docstring), top module
    ``wrapper.name``."""
    return "\n".join(_wrapper_module(wrapper)) + "\n"


def to_icl_1500_chain(
    wrappers: Sequence[Wrapper1500],
    *,
    top: str,
    pins: Optional[Mapping[str, tuple[str, bool]]] = None,
) -> str:
    """The ICL of wrappers daisy-chained on a SoC: each wrapper's module, and ``top``
    with the SoC's own WSP -- ``pins`` maps a role (``ROLES``) to the SoC port that
    drives it and whether an inverter sits between (default: the wrapper's own port
    name, direct) -- the wrappers' WSI and WSO chained, WSI at the first and WSO from the
    last, under the shared controls."""
    if not wrappers:
        raise Icl1500Error("a chain needs a wrapper")
    pins = dict(pins or {})
    unknown = set(pins) - set(ROLES)
    if unknown:
        raise Icl1500Error(f"unknown WSP roles {sorted(unknown)}")
    first = wrappers[0].ports
    seen: dict[str, Wrapper1500] = {}
    lines: list[str] = []
    for wrapper in wrappers:
        if wrapper.name in seen:
            if seen[wrapper.name] != wrapper:
                raise Icl1500Error(f"two different wrappers are named {wrapper.name}")
            continue
        seen[wrapper.name] = wrapper
        lines += _wrapper_module(wrapper) + [""]
    soc = {role: pins.get(role, (getattr(first, role), False)) for role in ROLES}
    lines.append(f"Module {top} {{")
    lines.append(f"    Attribute warptap_format = \"{FORMAT}\";")
    lines.append(
        "    Attribute warptap_chain = \""
        + ",".join(f"u{n}:{w.name}" for n, w in enumerate(wrappers))
        + "\";"
    )
    for role in ROLES:
        port, inverted = soc[role]
        lines.append(f"    Attribute warptap_pin_{role} = \"{port},{int(inverted)}\";")
    lines += [
        f"    ScanInPort {soc['wsi'][0]};",
        f"    ScanOutPort {soc['wso'][0]} {{ Source u{len(wrappers) - 1}.{wrappers[-1].ports.wso}; }}",
        f"    ShiftEnPort {soc['shiftwr'][0]};",
        f"    CaptureEnPort {soc['capturewr'][0]};",
        f"    UpdateEnPort {soc['updatewr'][0]};",
        f"    SelectPort {soc['selectwir'][0]};",
        f"    TCKPort {soc['wrck'][0]};",
        f"    ResetPort {soc['wrstn'][0]} {{ ActivePolarity 0; }}",
        "    ScanInterface WSP { "
        + " ".join(f"Port {soc[role][0]};" for role in ROLES)
        + " }",
    ]
    for n, wrapper in enumerate(wrappers):
        own = wrapper.ports
        binds = []
        # The wrapped block's data inputs come from the SoC: ports of its own here.
        names = _Names(wrapper)
        for k, cells in enumerate(wrapper.chains):
            for i, cell in enumerate(cells):
                if cell.side == INPUT:
                    inner = names.cell[(k, i)]
                    lines.append(f"    DataInPort u{n}_{inner};")
                    binds.append(f"InputPort {inner} = u{n}_{inner};")
        for role in ROLES:
            if role == "wso":
                continue
            if role == "wsi":
                source = soc["wsi"][0] if n == 0 else f"u{n - 1}.{wrappers[n - 1].ports.wso}"
            else:
                port, inverted = soc[role]
                source = ("~" if inverted else "") + port
            binds.append(f"InputPort {getattr(own, role)} = {source};")
        # The wrapper's own scan access (WPP) is no part of the WSP chain: the SoC's
        # scan pins, ports of their own here.
        lines.append(f"    ShiftEnPort u{n}_{own.scan_enable};")
        binds.append(f"InputPort {own.scan_enable} = u{n}_{own.scan_enable};")
        wpp = []
        for k in range(len(wrapper.chains)):
            lines.append(f"    ScanInPort u{n}_{own.si(k)};")
            lines.append(
                f"    ScanOutPort u{n}_{own.so(k)} {{ Source u{n}.{own.so(k)}; }}"
            )
            binds.append(f"InputPort {own.si(k)} = u{n}_{own.si(k)};")
            wpp.append(
                f"Chain c{k} {{ Port u{n}_{own.si(k)}; Port u{n}_{own.so(k)}; }}"
            )
        lines.append(
            f"    ScanInterface u{n}_WPP {{ {' '.join(wpp)} "
            f"Port u{n}_{own.scan_enable}; Port {soc['wrck'][0]}; "
            f"Port {soc['wrstn'][0]}; }}"
        )
        lines.append(f"    Instance u{n} Of {wrapper.name} {{ {' '.join(binds)} }}")
    lines.append("}")
    return "\n".join(lines) + "\n"
