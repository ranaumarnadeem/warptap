"""Replay a STIL file literally against RTL in Icarus: the oracle for ``warptap.tap_ir_stil``'s
timing, which a syntax and semantic check cannot see.

Semi-ATE-STIL's own dump compiler (code warptap didn't write) parses the file and resolves every
vector's waveform characters, its ``Loop`` blocks and its ``W`` switches; it runs in a
subprocess because it calls ``os._exit`` on some errors. This module only turns its output into
timed events, as a tester would apply them:

- each drive event (``D``/``U``/``Z``/``N``) at its offset in its vector's period, ``P`` holding
  the prior drive;
- each compare (``H``/``L``/``T``) checked at exactly its offset, ``X`` checking nothing.

Any other event, a drive on an ``Out`` signal or a compare on an ``In`` signal raises
``ValueError`` rather than being skipped. The generated testbench reads the events from a file and
counts every compare that sees anything but the expected value (``x`` and ``z`` included).

A STIL signal named ``bus[3]`` drives bit 3 of the DUT port ``bus``. DUT inputs the file does not
declare are tied by the caller (``tie``), and ``reset_pulse`` pulses some of them to their active
level before the pattern starts, for a pattern with no reset lead-in of its own. ``initial`` gives
a STIL-driven input a value from time zero, with no event, the way a tester's pin powers up.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from warptap.sim_io import run_verilog_testbench

_DRIVES = {"D": 0, "U": 1, "Z": 2, "N": 3}
_COMPARES = {"L": 0, "H": 1, "T": 2}
_NO_EVENT = {"P", "X"}
_PATTERN_START_FS = 100_000_000  # 100ns: room for a reset_pulse before the first vector
_RESET_ASSERT_FS = 10_000_000
_RESET_RELEASE_FS = 40_000_000

_COMPILE_SCRIPT = r"""
import json, sys
from Semi_ATE.STIL.parsers.STILDumpCompiler import STILDumpCompiler
from Semi_ATE.STIL.parsers.TimeUtils import TimeUtils
stil_path, out_dir = sys.argv[1], sys.argv[2]
compiler = STILDumpCompiler(stil_path, out_dir, expanding_procs=True,
                            is_scan_mem_available=False, enable_trace=False)
compiler.compile()
periods = {key.split("::", 1)[1]: TimeUtils.get_time_fsec(value)
           for key, value in compiler.wft2period.items()}
print("@@RESULT@@" + json.dumps({"done": compiler.is_parsing_done, "error": compiler.err_msg,
                                 "types": compiler.sig2type, "periods": periods}))
"""


@dataclass(frozen=True)
class ReplayResult:
    vectors: int
    compares: int
    mismatches: List[str]


@dataclass(frozen=True)
class _Row:
    wfcs: str
    commands: Tuple[str, ...]


def _unquote(name: str) -> str:
    name = name.strip()
    return name[1:-1] if len(name) >= 2 and name[0] == name[-1] == '"' else name


def _compile(
    stil_text: str,
) -> Tuple[Dict[str, str], Dict[str, int], Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory(prefix="warptap-stil-replay-")
    stil_path = Path(tmp.name) / "pattern.stil"
    stil_path.write_text(stil_text, encoding="utf-8")
    out_dir = Path(tmp.name) / "dump"
    proc = subprocess.run(
        [sys.executable, "-u", "-c", _COMPILE_SCRIPT, str(stil_path), str(out_dir)],
        capture_output=True, text=True,
    )
    marker = [line for line in proc.stdout.splitlines() if line.startswith("@@RESULT@@")]
    if proc.returncode != 0 or not marker:
        tmp.cleanup()
        raise ValueError(f"Semi-ATE-STIL dump compiler failed:\n{proc.stdout}\n{proc.stderr}")
    result = json.loads(marker[-1][len("@@RESULT@@"):])
    if not result["done"] or result["error"]:
        tmp.cleanup()
        raise ValueError(f"Semi-ATE-STIL rejected the file: {result['error']}")
    types = {_unquote(name): kind for name, kind in result["types"].items()}
    periods = {_unquote(name): int(round(fs)) for name, fs in result["periods"].items()}
    return types, periods, out_dir, tmp


def _read_timing(out_dir: Path) -> Dict[Tuple[str, str, str], List[Tuple[str, int]]]:
    """(wft, signal, wfc) -> [(event, offset_fs)]."""
    timing: Dict[Tuple[str, str, str], List[Tuple[str, int]]] = {}
    for line in (out_dir / "timing.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split("|")]
        _domain, wft, signal, wfc = fields[:4]
        events = []
        for event in filter(None, fields[4:]):
            kind, _, offset = event.partition(":")
            if not offset.endswith("fs"):
                raise ValueError(f"unexpected event time in timing.txt: {event!r}")
            events.append((kind, int(round(float(offset[:-2])))))
        timing[(_unquote(wft), _unquote(signal), wfc)] = events
    return timing


def _read_rows(out_dir: Path) -> Tuple[List[str], List[_Row]]:
    blocks = list(out_dir.glob("*.pattern_block"))
    if len(blocks) != 1:
        raise ValueError(f"expected one pattern block, found {[b.name for b in blocks]}")
    order: Optional[List[str]] = None
    rows: List[_Row] = []
    row_re = re.compile(r"^\s*(\d+)\|\s*[^|\s]+\|\s*[^|\s]+\|\s*([^|\s]+)\|(.*)$")
    for line in blocks[0].read_text().splitlines():
        if line.startswith("# SIGNALS_ORDER|"):
            order = [_unquote(s) for s in line.split("|", 1)[1].rstrip("|").split("+")]
            continue
        match = row_re.match(line)
        if match is None:
            continue
        if int(match.group(1)) != len(rows):
            raise ValueError(f"vector addresses out of order at {line!r}")
        commands = tuple(c.strip() for c in match.group(3).split("|") if c.strip())
        rows.append(_Row(match.group(2), commands))
    if order is None:
        raise ValueError("pattern block has no SIGNALS_ORDER line")
    for row in rows:
        if len(row.wfcs) != len(order):
            raise ValueError(f"{row.wfcs!r} does not have one WFC per signal of {order}")
    return order, rows


def _expand(rows: List[_Row]) -> List[Tuple[str, str]]:
    """Execute the dump's loop commands: [(wft, wfcs)], one entry per vector applied.

    LOAD_LOOP_COUNTER sits on the vector before its loop's START_LOOP (and may share a vector
    with the previous loop's body); STOP_LOOP=n ends a loop that started n vectors earlier."""
    flat: List[Tuple[str, str]] = []
    wft: Optional[str] = None
    pending_count: Optional[int] = None
    loop_start: Optional[int] = None
    loop_count = 0
    i = 0
    wfts: List[Optional[str]] = []
    for row in rows:  # resolve each row's table first: a loop body re-applies the same one
        for command in row.commands:
            if command.startswith("WFT="):
                wft = _unquote(command[4:])
        wfts.append(wft)
    while i < len(rows):
        row = rows[i]
        names = {c.split("=", 1)[0] for c in row.commands}
        unknown = names - {"WFT", "VECTOR", "LOAD_LOOP_COUNTER", "START_LOOP", "STOP_LOOP"}
        if unknown:
            raise ValueError(f"unsupported pattern command(s) {sorted(unknown)} at vector {i}")
        if "START_LOOP" in names and loop_start != i:
            if pending_count is None:
                raise ValueError(f"loop at vector {i} has no LOAD_LOOP_COUNTER before it")
            loop_start, loop_count = i, pending_count
        for command in row.commands:
            if command.startswith("LOAD_LOOP_COUNTER="):
                pending_count = int(command.split("=", 1)[1])
        if "VECTOR" not in names:
            raise ValueError(f"pattern row {i} is not a vector: {row.commands}")
        if wfts[i] is None:
            raise ValueError(f"vector {i} runs before any W statement")
        flat.append((wfts[i], row.wfcs))
        stop = [c for c in row.commands if c.startswith("STOP_LOOP=")]
        if stop:
            start = i - int(stop[0].split("=", 1)[1])
            if start != loop_start:
                raise ValueError(f"STOP_LOOP at vector {i} does not close the loop at {loop_start}")
            loop_count -= 1
            if loop_count > 0:
                i = start
                continue
            loop_start = None
        i += 1
    return flat


def _split(name: str) -> Tuple[str, Optional[int]]:
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_$]*)\[(\d+)\]", name)
    if match:
        return match.group(1), int(match.group(2))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
        raise ValueError(f"cannot map STIL signal {name!r} onto a Verilog port")
    return name, None


def _testbench(
    top: str,
    order: Sequence[str],
    types: Mapping[str, str],
    tie: Mapping[str, int],
    reset_pulse: Mapping[str, int],
    initial: Mapping[str, int],
) -> str:
    ports: Dict[str, Tuple[str, Optional[int]]] = {}  # base -> (direction, max bit)
    for name in order:
        base, bit = _split(name)
        direction = types[name]
        if base in ports:
            prior_direction, prior_bit = ports[base]
            if prior_direction != direction or (prior_bit is None) != (bit is None):
                raise ValueError(f"STIL signals for port {base!r} disagree on shape or direction")
            ports[base] = (direction, max(prior_bit, bit) if bit is not None else None)
        else:
            ports[base] = (direction, bit)
    for name in initial:
        if name not in order or types[name] != "In":
            raise ValueError(f"initial value for {name!r}, which is not a STIL input")
    for name in list(tie) + list(reset_pulse):
        if name in ports:
            raise ValueError(f"{name!r} is driven by the STIL file, it cannot also be tied")

    lines = ["`timescale 1fs/1fs", "module stil_replay_tb;"]
    for base, (direction, bit) in ports.items():
        width = "" if bit is None else f"[{bit}:0] "
        lines.append(f"    {'reg' if direction == 'In' else 'wire'} {width}{base};")
    for name, value in tie.items():
        lines.append(f"    reg {name} = 1'b{value};")
    for name, active in reset_pulse.items():
        lines.append(f"    reg {name} = 1'b{1 - active};")
    connections = [f".{name}({name})" for name in (*ports, *tie, *reset_pulse)]
    lines.append(f"    {top} dut ({', '.join(connections)});")
    lines += [
        "    function reg level(input integer v);",
        "        case (v)",
        "            0: level = 1'b0; 1: level = 1'b1; 2: level = 1'bz; default: level = 1'bx;",
        "        endcase",
        "    endfunction",
        "    reg [63:0] at;",
        "    integer fd, op, idx, val, compares, mismatches;",
        "    reg got;",
        "    initial begin",
    ]
    for name, value in initial.items():
        base, bit = _split(name)
        target = base if bit is None else f"{base}[{bit}]"
        lines.append(f"        {target} = 1'b{value};")
    if reset_pulse:
        lines.append(f"        #{_RESET_ASSERT_FS};")
        lines += [f"        {name} = 1'b{active};" for name, active in reset_pulse.items()]
        lines.append(f"        #{_RESET_RELEASE_FS - _RESET_ASSERT_FS};")
        lines += [f"        {name} = 1'b{1 - active};" for name, active in reset_pulse.items()]
    lines += [
        "        compares = 0; mismatches = 0;",
        '        fd = $fopen("events.txt", "r");',
        '        while ($fscanf(fd, "%d %d %d %d", at, op, idx, val) == 4) begin',
        "            if (at > $time) #(at - $time);",
        "            if (op == 0) begin",
        "                case (idx)",
    ]
    for idx, name in enumerate(order):
        if types[name] == "In":
            base, bit = _split(name)
            target = base if bit is None else f"{base}[{bit}]"
            lines.append(f"                    {idx}: {target} = level(val);")
    lines += [
        "                endcase",
        "            end else begin",
        "                case (idx)",
    ]
    for idx, name in enumerate(order):
        if types[name] == "Out":
            base, bit = _split(name)
            source = base if bit is None else f"{base}[{bit}]"
            lines.append(f"                    {idx}: got = {source};")
    lines += [
        "                endcase",
        "                compares = compares + 1;",
        "                if (got !== level(val)) begin",
        "                    mismatches = mismatches + 1;",
        '                    $display("MISMATCH,%0d,%0d,%0d,%b", $time, idx, val, got);',
        "                end",
        "            end",
        "        end",
        '        $display("RESULT,%0d,%0d", compares, mismatches);',
        "        $finish;",
        "    end",
        "endmodule",
    ]
    return "\n".join(lines) + "\n"


def replay_stil(
    stil_text: str,
    verilog_files: Sequence[Path],
    top: str,
    *,
    tie: Optional[Mapping[str, int]] = None,
    reset_pulse: Optional[Mapping[str, int]] = None,
    initial: Optional[Mapping[str, int]] = None,
    iverilog_command: Optional[str] = None,
    vvp_command: Optional[str] = None,
) -> ReplayResult:
    types, periods, out_dir, tmp = _compile(stil_text)
    try:
        timing = _read_timing(out_dir)
        order, rows = _read_rows(out_dir)
    finally:
        tmp.cleanup()
    vectors = _expand(rows)

    events: List[Tuple[int, int, int, int]] = []  # (time_fs, op, signal index, value)
    start = _PATTERN_START_FS
    for wft, wfcs in vectors:
        period = periods[wft]
        for idx, (name, wfc) in enumerate(zip(order, wfcs)):
            waveform = timing.get((wft, name, wfc))
            if waveform is None:
                raise ValueError(f"WaveformTable {wft} has no WFC {wfc!r} for {name}")
            for kind, offset in waveform:
                if not 0 <= offset < period:
                    raise ValueError(
                        f"{name} event {kind} at {offset}fs is outside the {period}fs period"
                    )
                if kind in _DRIVES:
                    if types[name] != "In":
                        raise ValueError(f"drive event {kind} on output {name}")
                    events.append((start + offset, 0, idx, _DRIVES[kind]))
                elif kind in _COMPARES:
                    if types[name] != "Out":
                        raise ValueError(f"compare event {kind} on input {name}")
                    events.append((start + offset, 1, idx, _COMPARES[kind]))
                elif kind not in _NO_EVENT:
                    raise ValueError(f"unsupported waveform event {kind!r} on {name}")
        start += period
    events.sort(key=lambda e: (e[0], e[1]))  # at one instant, drive before compare

    testbench = _testbench(top, order, types, tie or {}, reset_pulse or {}, initial or {})
    with tempfile.TemporaryDirectory(prefix="warptap-stil-replay-tb-") as tmpdir:
        tb_path = Path(tmpdir) / "stil_replay_tb.v"
        tb_path.write_text(testbench, encoding="utf-8")
        stdout = run_verilog_testbench(
            [*verilog_files, tb_path],
            extra_inputs={"events.txt": "".join(f"{t} {op} {i} {v}\n" for t, op, i, v in events)},
            iverilog_command=iverilog_command,
            vvp_command=vvp_command,
        )
    result = [line for line in stdout.splitlines() if line.startswith("RESULT,")]
    if len(result) != 1:
        raise ValueError(f"testbench printed no RESULT line:\n{stdout}")
    compares, mismatch_count = (int(v) for v in result[0].split(",")[1:])
    mismatches = []
    for line in stdout.splitlines():
        if line.startswith("MISMATCH,"):
            at, idx, expected, got = line.split(",")[1:]
            mismatches.append(f"{order[int(idx)]} at {int(at) / 1e6:g}ns: expected "
                              f"{'01z'[int(expected)]}, got {got}")
    assert len(mismatches) == mismatch_count
    expected_compares = sum(1 for e in events if e[1] == 1)
    assert compares == expected_compares, (compares, expected_compares)
    return ReplayResult(vectors=len(vectors), compares=compares, mismatches=mismatches)
