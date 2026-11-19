"""Gate-level evidence: map a warptap-inserted design to IHP SG13G2 standard cells with Yosys and
simulate the mapped netlist with the cells' own Verilog models.

``map_to_sg13g2`` takes the same netlist JSON the RTL cross-simulations write their Verilog from,
so a test can run one inserted design both ways and compare the traces. The flow is a plain Yosys
standard-cell mapping that keeps the hierarchy, as a user's synthesis of an inserted design does:
warptap marks its own modules (``tap_core``, the SIB and instrument cells) ``keep_hierarchy``, so
each module is mapped on its own. ``tribuf`` (an existing ``assign io = en ? v : 1'bz`` becomes a
``$tribuf``, as warptap's own TDO and boundary-register drivers already are), ``synth``, every
``$_TBUF_`` mapped to ``sg13g2_ebufn_2`` with an inverted enable (``TE_B`` is active low),
``dfflibmap`` and ``abc`` against the typical-corner liberty, and ``hilomap`` for constants. Every
module of the mapped netlist must hold only library cells, instances of the design's own
modules and macros, with one ``sg13g2_ebufn_2`` per tri-state driver of the design.

What this shows, and what it doesn't: the inserted logic still behaves the same once it is made
of real library cells, through Yosys's mapping, with the IHP cell models' logic. It does not
show timing: the models' path delays are zero and Icarus does not run their timing checks, which
is what the STA in ``test_gate_level_sg13g2.py`` is for. Nothing here is silicon or a
commercial tool.

The cell models run in their power-aware mode (``USE_POWER_PINS``, every instance tied to VDD
and VSS): in the other mode each output goes through a ``buf`` gate, which turns ``z`` into
``x``, so a released tri-state pin would read ``x`` (:func:`with_power_pins`).

The testbenches print some of the inserted top module's own wires (the TAP state, instruction
and DR controls, and ``tdo_oe``), so those wires get Yosys's ``keep`` attribute: it keeps the
names on their nets and changes no logic. Modules named in ``macros`` (the openMBIST design's
SRAM macros, behavioral memories a real flow never maps to standard cells) are left as black
boxes, and their behavioral models are written out of the same JSON for the simulation.

The cell library: ``WARPTAP_IHP_STDCELL_DIR``, or the copy ``ciel`` installs. Each of the five
files is checked against its git blob SHA at the IHP-Open-PDK commit below, which is what CI
downloads.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from warptap.sim_io import run_verilog_testbench
from warptap.yosys_io import run_yosys_script

SG13G2_COMMIT = "e73adb4045731ddec21634146d536380057f6a26"
SG13G2_REPO_DIR = "ihp-sg13g2/libs.ref/sg13g2_stdcell"
SG13G2_FILES = {
    "lib/sg13g2_stdcell_typ_1p20V_25C.lib": "6768e90d97ea01f229a2079fbc198ddf92796b5e",
    "lib/sg13g2_stdcell_slow_1p08V_125C.lib": "a8077d89a51e11cc41c5ef3fa437c5a59ab30eb7",
    "lib/sg13g2_stdcell_fast_1p32V_m40C.lib": "55b7f7792f70ef0f57f967c674aa0f6fd1db27ba",
    "verilog/sg13g2_stdcell.v": "21df13c7adfcdeebc8f40ef7cfc8d6ffb976042a",
    "verilog/sg13g2_udp.v": "ca53cb9a45c16d0ed276d396300760d372048b33",
}

#: The inserted top module's wires the testbenches print.
KEEP_WIRES = (
    "warptap_tap_state",
    "warptap_current_instruction",
    "warptap_capture_dr",
    "warptap_shift_dr",
    "warptap_update_dr",
    "warptap_tdo_oe",
)

TRISTATE_CELL = "sg13g2_ebufn_2"

#: ``$_TBUF_`` (Yosys's tri-state gate, output ``Y = E ? A : z``) as the library's tri-state
#: buffer, whose enable ``TE_B`` is active low.
TBUF_TECHMAP = """\
module \\$_TBUF_ (input A, input E, output Y);
  sg13g2_ebufn_2 _TECHMAP_REPLACE_ (.A(A), .TE_B(~E), .Z(Y));
endmodule
"""

#: The control mapping: the same cell with the enable not inverted. A design mapped with it
#: drives each tri-stated pin exactly when it should be released.
TBUF_TECHMAP_WRONG_POLARITY = """\
module \\$_TBUF_ (input A, input E, output Y);
  sg13g2_ebufn_2 _TECHMAP_REPLACE_ (.A(A), .TE_B(E), .Z(Y));
endmodule
"""


def git_blob_sha(data: bytes) -> str:
    """What ``git hash-object`` prints for a file holding ``data``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@dataclass(frozen=True)
class Sg13g2:
    """The five SG13G2 standard-cell files, under ``root`` as they are in the PDK."""

    root: Path

    def path(self, rel: str) -> Path:
        return self.root / rel

    @property
    def typ_lib(self) -> Path:
        return self.path("lib/sg13g2_stdcell_typ_1p20V_25C.lib")

    @property
    def slow_lib(self) -> Path:
        return self.path("lib/sg13g2_stdcell_slow_1p08V_125C.lib")

    @property
    def fast_lib(self) -> Path:
        return self.path("lib/sg13g2_stdcell_fast_1p32V_m40C.lib")

    @property
    def models(self) -> List[Path]:
        return [self.path("verilog/sg13g2_stdcell.v"), self.path("verilog/sg13g2_udp.v")]


def _candidate_dirs() -> Iterable[Path]:
    if os.environ.get("WARPTAP_IHP_STDCELL_DIR"):
        yield Path(os.environ["WARPTAP_IHP_STDCELL_DIR"])
        return
    for base in (Path.home() / ".ciel" / "ciel" / "ihp-sg13" / "versions", Path.home() / ".ciel" / "ihp-sg13" / "versions"):
        if base.is_dir():
            for version in sorted(base.iterdir()):
                yield version / SG13G2_REPO_DIR


def find_sg13g2() -> tuple[Optional[Sg13g2], str]:
    """The cell library, or ``None`` and why not. A directory whose files are missing or differ
    from the pinned ones is refused, not used."""
    reasons = []
    for root in _candidate_dirs():
        problems = []
        for rel, sha in SG13G2_FILES.items():
            path = root / rel
            if not path.is_file():
                problems.append(f"{rel} missing")
            elif git_blob_sha(path.read_bytes()) != sha:
                problems.append(f"{rel} is not blob {sha[:12]}")
        if not problems:
            return Sg13g2(root), ""
        reasons.append(f"{root}: {'; '.join(problems)}")
    if not reasons:
        return None, (
            "no IHP SG13G2 standard-cell library found: set WARPTAP_IHP_STDCELL_DIR to a "
            f"directory holding {SG13G2_REPO_DIR}'s lib/ and verilog/ files"
        )
    return None, "; ".join(reasons)


@dataclass(frozen=True)
class Mapped:
    """A design mapped to SG13G2. ``verilog`` instantiates only library cells, its own modules
    and the ``macros``, whose behavioral models are ``macros_verilog``. ``cells`` maps each
    mapped module to its cell counts by type."""

    top: str
    verilog: str
    macros_verilog: str
    cells: Dict[str, Dict[str, int]]
    tristate_drivers: int

    def total(self, cell_type: str) -> int:
        return sum(counts.get(cell_type, 0) for counts in self.cells.values())

    def library_cells(self) -> int:
        return sum(n for counts in self.cells.values() for t, n in counts.items() if t.startswith("sg13g2_"))


def _count(path: Path) -> int:
    match = re.search(r"(\d+) objects", path.read_text(encoding="utf-8"))
    assert match, path.read_text(encoding="utf-8")
    return int(match.group(1))


def map_to_sg13g2(
    netlist_json: dict,
    top: str,
    lib: Sg13g2,
    *,
    macros: Sequence[str] = (),
    keep: Sequence[str] = KEEP_WIRES,
    tbuf_techmap: str = TBUF_TECHMAP,
    yosys_command: Optional[str] = None,
) -> Mapped:
    with tempfile.TemporaryDirectory(prefix="warptap-sg13g2-") as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "in.json").write_text(json.dumps(netlist_json), encoding="utf-8")
        shutil.copy(lib.typ_lib, tmp / "typ.lib")
        (tmp / "tbuf_map.v").write_text(tbuf_techmap, encoding="utf-8")
        steps = ["read_json in.json", f"hierarchy -top {top}"]
        if macros:
            names = " ".join(macros)
            # The macros' behavioral models, as the RTL run simulates them, then black boxes.
            steps += [
                f"select {names}",
                "write_verilog -noattr -selected macros.v",
                "select -clear",
                f"blackbox {names}",
            ]
        steps += [
            "read_liberty -lib -ignore_miss_dir typ.lib",
            *(f"setattr -set keep 1 {top}/w:{name}" for name in keep),
            "proc",
            "tribuf",
            "tee -q -o tristates.txt select -count t:$tribuf",
            f"synth -top {top}",
            "techmap -map tbuf_map.v",
            "techmap",
            "dfflibmap -liberty typ.lib",
            "abc -liberty typ.lib",
            "opt_clean",
            "hilomap -hicell sg13g2_tiehi L_HI -locell sg13g2_tielo L_LO",
            "opt_clean",
            "write_verilog -noattr -noexpr mapped.v",
            "write_json mapped.json",
        ]
        run_yosys_script("; ".join(steps), yosys_command=yosys_command, cwd=tmp)
        mapped = json.loads((tmp / "mapped.json").read_text(encoding="utf-8"))
        cells: Dict[str, Dict[str, int]] = {}
        for name, module in mapped["modules"].items():
            if _is_blackbox(module):
                continue  # a library cell's or a macro's interface, read as a black box
            counts: Dict[str, int] = {}
            for cell in module["cells"].values():
                counts[cell["type"]] = counts.get(cell["type"], 0) + 1
            cells[name] = counts
        return Mapped(
            top=top,
            verilog=(tmp / "mapped.v").read_text(encoding="utf-8"),
            macros_verilog=(tmp / "macros.v").read_text(encoding="utf-8") if macros else "",
            cells=cells,
            tristate_drivers=_count(tmp / "tristates.txt"),
        )


def _is_blackbox(module: dict) -> bool:
    value = module.get("attributes", {}).get("blackbox", 0)
    return bool(int(value, 2) if isinstance(value, str) else value)


def check_mapped(mapped: Mapped, macros: Sequence[str] = ()) -> None:
    """Every module holds only library cells, the design's own modules and macros, and there
    is one tri-state buffer per tri-state driver."""
    for module, counts in mapped.cells.items():
        # A parameterized module's name starts with "$paramod", a generic cell type's with "$".
        generic = {t: n for t, n in counts.items() if t.startswith("$") and t not in mapped.cells}
        assert not generic, f"generic cells left in {module} after mapping: {generic}"
        foreign = {
            t: n for t, n in counts.items()
            if not t.startswith("sg13g2_") and t not in mapped.cells and t not in macros
        }
        assert not foreign, f"cells in {module} that are not SG13G2 cells, modules or macros: {foreign}"
    assert mapped.total(TRISTATE_CELL) == mapped.tristate_drivers, (
        f"{mapped.tristate_drivers} tri-state drivers but {mapped.total(TRISTATE_CELL)} "
        f"{TRISTATE_CELL} cells"
    )


_INSTANCE = re.compile(r"^(\s*)(sg13g2_\w+)\s+(?:\\\S+|\S+)\s*\($", re.M)
_MODULE_HEADER = re.compile(r"^module\s+(?:\\\S+|\w+)\s*\(.*?\);\n", re.M | re.S)
_VDD, _VSS = "warptap_sg13g2_vdd", "warptap_sg13g2_vss"


def with_power_pins(verilog: str) -> str:
    """``verilog`` with every SG13G2 cell instance's VDD and VSS on a ``supply1`` and a
    ``supply0`` net of its module.

    The cell models drive each output through ``buf`` unless ``USE_POWER_PINS`` is defined,
    and a ``buf`` gate turns ``z`` into ``x``, so in that mode ``sg13g2_ebufn_2`` can never
    release its pin. With ``USE_POWER_PINS`` the output is ``power_good ? raw : 1'bx``, which
    passes ``z``; ``power_good`` needs VDD at 1 and VSS at 0."""
    supplies = _MODULE_HEADER.sub(lambda m: f"{m.group(0)}  supply1 {_VDD};\n  supply0 {_VSS};\n", verilog)
    return _INSTANCE.sub(
        lambda m: f"{m.group(0)}\n{m.group(1)}  .VDD({_VDD}),\n{m.group(1)}  .VSS({_VSS}),", supplies
    )


def simulation_verilog(mapped: Mapped) -> str:
    """One Verilog file for simulating ``mapped``: ``USE_POWER_PINS`` defined (it applies to the
    cell models compiled after this file), the netlist with every cell tied to power, and the
    macros' behavioral models."""
    powered = with_power_pins(mapped.verilog)
    tied = powered.count(f".VDD({_VDD})")
    assert tied == mapped.library_cells(), f"tied {tied} of {mapped.library_cells()} cells to power"
    return "`define USE_POWER_PINS\n" + powered + "\n" + mapped.macros_verilog


def run_gate_level(
    mapped: Mapped,
    lib: Sg13g2,
    testbench: Sequence[Path],
    *,
    extra_inputs: Optional[Dict[str, str]] = None,
    iverilog_command: Optional[str] = None,
    vvp_command: Optional[str] = None,
) -> str:
    """Simulate ``mapped`` (and its macros' models) with the SG13G2 cell models and
    ``testbench``; return what it prints. The models run in their power-aware mode
    (``USE_POWER_PINS``, every instance tied to VDD=1, VSS=0: see :func:`with_power_pins`)."""
    with tempfile.TemporaryDirectory(prefix="warptap-sg13g2-sim-") as tmpdir:
        design = Path(tmpdir) / f"{mapped.top}_sg13g2.v"
        design.write_text(simulation_verilog(mapped), encoding="utf-8")
        return run_verilog_testbench(
            [design, *lib.models, *testbench],
            extra_inputs=extra_inputs,
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
