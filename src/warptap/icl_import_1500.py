"""The model back from the ICL :mod:`warptap.icl_emit_1500` writes: a
:class:`~warptap.wsp1500_model.Wrapper1500`, or -- for a SoC top that chains wrappers --
a :class:`~warptap.wsp1500_model.WspChainDesign`.

Like :mod:`warptap.icl_import` for the SIB network, it takes only warptap's own shape,
and says so rather than guess: the model is read from the ``warptap_*`` attributes, then
written as ICL again, and the text must be that ICL byte for byte. Anything else -- a SIB
network, a third tool's wrapper, this ICL with its WIR or WBY edited out or an Enum value
changed -- is refused, so a program built from an imported wrapper is built from a
wrapper whose ICL is the standard description the vendored ``icl_parser`` checks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Union

from warptap.icl_emit_1500 import FORMAT, ROLES, Icl1500Error, to_icl_1500, to_icl_1500_chain
from warptap.wsp1500_model import (
    Instruction,
    WbrCell,
    Wrapper1500,
    WspChainDesign,
    WspPorts,
)

_MODULE = re.compile(r"^Module (\w+) \{$")
_ATTRIBUTE = re.compile(r'^    Attribute (warptap_\w+) = (?:"([^"]*)"|(\d+));$')


class Icl1500ImportError(ValueError):
    """ICL that isn't what warptap writes for an IEEE 1500 wrapper."""


def _refuse(why: str) -> Icl1500ImportError:
    return Icl1500ImportError(f"not warptap's IEEE 1500 ICL: {why}")


def _modules(text: str) -> dict[str, dict[str, str]]:
    """Each module's ``warptap_*`` attributes, by module name, in the text."""
    found: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        start = _MODULE.match(line)
        if start:
            if start.group(1) in found:
                raise _refuse(f"module {start.group(1)} twice")
            current = found.setdefault(start.group(1), {})
        elif line == "}":
            current = None
        elif current is not None:
            attribute = _ATTRIBUTE.match(line)
            if attribute:
                current[attribute.group(1)] = (
                    attribute.group(2)
                    if attribute.group(2) is not None
                    else attribute.group(3)
                )
    return found


def _wrapper(name: str, attrs: Mapping[str, str]) -> Wrapper1500:
    try:
        if attrs.get("warptap_format") != FORMAT:
            raise _refuse(f"{name} is not a wrapper module")
        bits = int(attrs["warptap_ir_bits"])
        instructions = []
        while f"warptap_instr_{len(instructions)}" in attrs:
            ins, code, serial, intest, extest = attrs[
                f"warptap_instr_{len(instructions)}"
            ].split(",")
            instructions.append(
                Instruction(ins, int(code), bool(int(serial)), (int(intest), int(extest)))
            )
        chains = []
        while f"warptap_cell_{len(chains)}_0" in attrs:
            k, cells = len(chains), []
            while f"warptap_cell_{k}_{len(cells)}" in attrs:
                side, label = attrs[f"warptap_cell_{k}_{len(cells)}"].split(",", 1)
                cells.append(WbrCell(label, side))
            chains.append(tuple(cells))
        names = attrs["warptap_ports"].split(",")
        if len(names) != len(ROLES) + 1:
            raise _refuse("the port names")
        pairs = [pair.split(":") for pair in attrs["warptap_chain_ports"].split(",")]
        ins_names = tuple(pair[0] for pair in pairs)
        outs_names = tuple(pair[1] for pair in pairs)
        # The names every chain gets without being told: not stored.
        ports = WspPorts(
            **dict(zip(ROLES, names)),
            scan_enable=names[-1],
            chain_in=(
                ()
                if ins_names == tuple(f"si{k}" for k in range(len(pairs)))
                else ins_names
            ),
            chain_out=(
                ()
                if outs_names == tuple(f"so{k}" for k in range(len(pairs)))
                else outs_names
            ),
        )
        return Wrapper1500(
            name=name,
            chains=tuple(chains),
            ir_bits=bits,
            instructions=tuple(instructions),
            ports=ports,
        )
    except Icl1500ImportError:
        raise
    except (KeyError, ValueError, IndexError) as exc:
        raise _refuse(f"{name}'s attributes: {exc}") from exc


def import_icl_1500(text: str) -> Union[Wrapper1500, WspChainDesign]:
    """The wrapper ``text`` describes, or the chain of them: the model read from its
    attributes, which must write exactly ``text`` again."""
    modules = _modules(text)
    if not modules:
        raise Icl1500ImportError("no module in the ICL")
    tops = [name for name, attrs in modules.items() if "warptap_chain" in attrs]
    wrappers = {
        name: attrs
        for name, attrs in modules.items()
        if attrs.get("warptap_format") == FORMAT and "warptap_chain" not in attrs
    }
    if not wrappers:
        raise _refuse("it has no wrapper module")
    try:
        if not tops:
            if len(wrappers) != 1:
                raise _refuse("several wrappers and no chain")
            ((name, attrs),) = wrappers.items()
            wrapper = _wrapper(name, attrs)
            if to_icl_1500(wrapper) != text:
                raise _refuse(f"{name} differs from the ICL warptap writes for it")
            return wrapper
        if len(tops) != 1:
            raise _refuse("more than one chain top")
        top = tops[0]
        attrs = modules[top]
        members = [item.split(":") for item in attrs["warptap_chain"].split(",")]
        built: dict[str, Wrapper1500] = {}
        for _, wrapper_name in members:
            if wrapper_name not in wrappers:
                raise _refuse(f"the chain names {wrapper_name}, which isn't defined")
            built.setdefault(wrapper_name, _wrapper(wrapper_name, wrappers[wrapper_name]))
        pins: dict[str, tuple[str, bool]] = {}
        for role in ROLES:
            port, inverted = attrs[f"warptap_pin_{role}"].split(",")
            pins[role] = (port, bool(int(inverted)))
        design = WspChainDesign(
            top=top,
            wrappers=tuple(built[name] for _, name in members),
            pins=pins,
        )
        if to_icl_1500_chain(design.wrappers, top=top, pins=pins) != text:
            raise _refuse(f"{top} differs from the ICL warptap writes for it")
        return design
    except Icl1500Error as exc:
        raise _refuse(str(exc)) from exc
    except (KeyError, ValueError) as exc:
        if isinstance(exc, Icl1500ImportError):
            raise
        raise _refuse(str(exc)) from exc


def import_icl_1500_file(path: Path) -> Union[Wrapper1500, WspChainDesign]:
    return import_icl_1500(Path(path).read_text(encoding="utf-8"))
