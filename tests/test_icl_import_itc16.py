"""``import_icl`` over every network of the ITC'16 IJTAG benchmark set: each either imports,
matching counts taken independently, and round-trips through ``to_icl``, or is rejected for a
named construct that warptap's model can't express, or for invalid ICL.

**The corpus is not vendored**: it carries no license, so it is read from a checkout at test
time and these tests skip without one. Point ``WARPTAP_ITC16_DIR`` at a checkout of
https://gitlab.com/Honza255/benchmarks at :data:`CORPUS_COMMIT`, or put one at
``../itc16-benchmarks`` beside this repository; CI clones it (``.github/workflows/tests.yml``).

**Which version**: the ``Fixes`` branch of the vendored parser's author's fork of the set, at
:data:`CORPUS_COMMIT`, not the set's upstream master (``gitlab.com/IJTAG/benchmarks``,
``577a4dd``). Upstream has ICL errors in 18 of its files that the branch corrects, which would
make many networks fail as invalid ICL before their structure is looked at. The branch also
changes two networks the other way: it leaves syntax errors in ``TreeFlat_Ex.icl`` and rewires
``TreeBalanced``; the table below is for the branch as it is.

**How**: networks are found the way the set's ``script/build.sh`` finds them: every directory
under ``ICL/`` with no subdirectories that holds a ``top.module``, parsed with its own ``.icl``
files and those of every directory above it, up to ``ICL/``. Each import runs in a process of
its own (``itc16_import_worker.py``) with a timeout of :data:`TIMEOUT` seconds
(``WARPTAP_ITC16_TIMEOUT`` overrides it). The networks in :data:`SLOW` take minutes to parse and
are marked ``slow`` (``-m "not slow"`` leaves them out).

**What an import is checked against**: the counts in :data:`EXPECTED`, taken from a structural
walk of the ICL made separately from the importer, and the parser's own inventory of the
elaborated design (every scan register, its bits, every ScanMux), which must be accounted for
exactly once; the first and last top-level slot, read off the top module by hand; and a round
trip, emitted by ``to_icl`` and imported again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from warptap.icl_import import (
    FOREIGN_SELECT,
    INVALID_ICL,
    NOT_ELABORATED,
    REGISTER_OUTSIDE_SLOT,
    REGISTERS_IN_SERIES,
    REJECTION_REASONS,
    SIB_REGISTER_BEFORE_MUX,
)

CORPUS_COMMIT = "f4578a8e157ddd46e81a29ca684965490b0fdfa8"
"""The commit of https://gitlab.com/Honza255/benchmarks (branch ``Fixes``) the table is for."""

TIMEOUT = float(os.environ.get("WARPTAP_ITC16_TIMEOUT", "3600"))
"""Seconds one network's import may take."""

WORKER = Path(__file__).resolve().parent / "itc16_import_worker.py"


class Imports(NamedTuple):
    sibs: int
    instruments: int
    bits: int  # every register bit: a SIB's one, a ScanMux's select register, the instruments'
    depth: int  # how deep SIBs and ScanMux slots nest
    first: str  # the top-level slot nearest the scan-in
    last: str  # the top-level slot nearest the scan-out
    scan_muxes: int = 0


class Rejected(NamedTuple):
    reason: str  # one of icl_import.REJECTION_REASONS, which the message starts with
    names: str  # part of the rest of the message: the element it names


EXPECTED = {
    # always-on registers: a scan register on the path outside any SIB or ScanMux, or beside
    # other elements in a SIB's segment (the model has no slot for one)
    "Advanced/N132D4": Rejected(REGISTER_OUTSIDE_SLOT, "'R128.reg8.SR' sits in the top module's chain"),
    "Advanced/N17D3": Rejected(REGISTER_OUTSIDE_SLOT, "'R13t.reg8.SR' in SIB 'SIB13'"),
    "Advanced/N32D6": Rejected(REGISTER_OUTSIDE_SLOT, "'R31.reg8.SR' sits in the top module's chain"),
    "Advanced/N73D14": Rejected(
        REGISTER_OUTSIDE_SLOT, "'R72t.reg8.SR', 'R71.reg8.SR', 'R70.reg8.SR' in SIB 'SIB72'"
    ),
    "Advanced/NE1200P430": Rejected(REGISTER_OUTSIDE_SLOT, "'R1199.reg8.SR' sits in the top module's chain"),
    "Advanced/NE600P150": Rejected(REGISTER_OUTSIDE_SLOT, "'R598.reg8.SR' sits in the top module's chain"),
    # several registers in series behind one SIB
    "Basic/TreeBalanced": Rejected(REGISTERS_IN_SERIES, "'m6.wrpIn.SR', 'm6.wrpOut.SR' in SIB 'm6__sibWrp'"),
    "Basic/TreeUnbalanced": Rejected(
        REGISTERS_IN_SERIES, "'M0.wrpIn.reg.SR', 'M0.wrpOut.reg.SR', 'M0.wrpCntrl.reg.SR' in SIB 'sib0'"
    ),
    "Classic/p22810": Rejected(
        REGISTERS_IN_SERIES, "'m1.m2.wrpIn.reg.SR', 'm1.m2.wrpOut.reg.SR' in SIB 'm1__sibM2'"
    ),
    # a ScanMux whose select register is not right after it: an SCB elsewhere on the path, on
    # another chain (FlexScan's mask chain), or part of a wider register
    "Advanced/FlexScan": Rejected(FOREIGN_SELECT, "'SC0.SMux' is selected by 'SC0.scbMask.SR'"),
    "Advanced/TrapOrFlap": Rejected(FOREIGN_SELECT, "'sMux1' is selected by part of scan register 'SREG0.SR'"),
    "Basic/BasicSCB": Rejected(FOREIGN_SELECT, "'sMux5' is selected by 'SCB5.SR'"),
    "Basic/TreeFlat": Rejected(FOREIGN_SELECT, "'sMux0' is selected by 'SCB1.SR'"),
    # the other SIB form
    "Basic/Mingle": Rejected(SIB_REGISTER_BEFORE_MUX, "'SIBpost3.SR' feeds 'SIBpost3.SIBmux'"),
    # constructs the parser doesn't elaborate (and the model doesn't hold)
    "Standard/CAD": Rejected(NOT_ELABORATED, "Instance address"),
    "Standard/Multi_Core_AccessLink": Rejected(NOT_ELABORATED, "ToClockPort_source"),
    "Standard/SoC_Debug_Access_Port": Rejected(NOT_ELABORATED, "Instance address"),
    # invalid ICL
    "Basic/TreeFlat_Ex": Rejected(INVALID_ICL, "TreeFlat_Ex.icl: line 83:8 missing ';' at 'InputPort'"),
    "Classic/a586710": Rejected(INVALID_ICL, "for data in ConcatSig [sMuxInputs]"),  # a ScanMux in a LogicSignal
    "Classic/p34392": Rejected(INVALID_ICL, "for data in ConcatSig [sMuxInputs]"),
    "Classic/p93791": Rejected(INVALID_ICL, "sMuxInputs.DO is separated into hier path"),  # a ScanMux's "port"
    "Standard/Kernel": Rejected(INVALID_ICL, "Parameter bidirs is defined more than once in module Kernel"),
    "Standard/Multi_Test_Access_Port": Rejected(  # client-TAP and host ports in one interface
        INVALID_ICL, "ScanInterface 'TAP_host': port 'toSELECT' (IclToSelectPort) not allowed"
    ),
    # imports: SIB networks
    "Classic/q12710": Imports(sibs=27, instruments=23, bits=26189, depth=2, first="sibIn", last="sibM4"),
    "Classic/t512505": Imports(sibs=159, instruments=128, bits=77005, depth=2, first="sibIn", last="sibM31"),
}
"""Network directory under ``ICL/`` -> what ``import_icl`` does with it."""

SLOW = {"Advanced/FlexScan", "Advanced/NE1200P430", "Advanced/NE600P150", "Classic/t512505"}
"""The networks whose import takes more than a minute: 2 to 6 minutes each on a loaded 10-core
machine, against 40 seconds at most for any other."""


def _corpus_dir() -> Path:
    if os.environ.get("WARPTAP_ITC16_DIR"):
        return Path(os.environ["WARPTAP_ITC16_DIR"])
    return Path(__file__).resolve().parents[2] / "itc16-benchmarks"


@pytest.fixture(scope="module")
def corpus() -> Path:
    root = _corpus_dir()
    if not (root / "ICL").is_dir():
        pytest.skip(
            f"ITC'16 benchmark set not found at {root} (set WARPTAP_ITC16_DIR to a checkout of "
            f"https://gitlab.com/Honza255/benchmarks at {CORPUS_COMMIT})"
        )
    if (root / ".git").exists():
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        if head != CORPUS_COMMIT:
            pytest.skip(f"the checkout at {root} is at {head}, not {CORPUS_COMMIT}")
    return root


def networks(root: Path) -> dict:
    """Network directory under ``ICL/`` -> ``(top module, files)``, found and listed the way
    the set's ``script/build.sh`` does."""
    icl_root = root / "ICL"
    found = {}
    for directory, subdirectories, files in os.walk(icl_root):
        if subdirectories or "top.module" not in files:
            continue
        here = Path(directory)
        top = (here / "top.module").read_text().strip()
        paths, level = [], here
        while True:
            paths += sorted(p for p in level.glob("*.icl"))
            if level == icl_root:
                break
            level = level.parent
        found[here.relative_to(icl_root).as_posix()] = (top, paths)
    return found


def _run(top: str, files: list) -> dict:
    args = json.dumps({"top": top, "files": [str(f) for f in files]})
    try:
        process = subprocess.run(
            [sys.executable, str(WORKER), args], capture_output=True, text=True, timeout=TIMEOUT
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"importing {top!r} took longer than {TIMEOUT:.0f} s (WARPTAP_ITC16_TIMEOUT raises it)"
        )
    lines = [line for line in process.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"the worker for {top!r} exited {process.returncode}:\n{process.stderr[-3000:]}"
    return json.loads(lines[-1][len("RESULT "):])


def test_the_table_covers_every_network_and_names_a_reason_for_each_rejection(corpus):
    assert sorted(networks(corpus)) == sorted(EXPECTED)
    for network, expected in EXPECTED.items():
        if isinstance(expected, Rejected):
            assert expected.reason in REJECTION_REASONS, network
    assert SLOW <= set(EXPECTED)


@pytest.mark.parametrize(
    "network",
    [pytest.param(n, marks=pytest.mark.slow) if n in SLOW else n for n in sorted(EXPECTED)],
)
def test_network(corpus, network):
    top, files = networks(corpus)[network]
    result = _run(top, files)
    expected = EXPECTED[network]
    if isinstance(expected, Rejected):
        assert result["outcome"] == "rejected", result
        assert result["message"].startswith(f"{expected.reason}: "), result["message"]
        assert expected.names in result["message"], result["message"]
        return

    assert result["outcome"] == "imports", result
    shape, parser = result["shape"], result["parser_inventory"]
    assert (shape["sibs"], shape["scan_muxes"], shape["instruments"], shape["bits"], shape["depth"]) == (
        expected.sibs, expected.scan_muxes, expected.instruments, expected.bits, expected.depth
    )
    # every scan register and ScanMux the parser elaborated is in the graph, once
    assert shape["sibs"] + shape["scan_muxes"] + shape["instruments"] == parser["registers"]
    assert shape["bits"] == parser["bits"]
    assert shape["sibs"] + shape["scan_muxes"] == parser["scan_muxes"]
    assert (result["top_chain"][0], result["top_chain"][-1]) == (expected.first, expected.last)
    assert result["round_trips"]
