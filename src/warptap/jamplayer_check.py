"""Subprocess wrapper around a real, independently-built copy of Altera's "Jam STAPL Player"
reference C interpreter (implementation_plan.md §7 Stage 7, §8's own testing strategy:
"validated by feeding output through an existing consumer... not a self-written parser").
Complements :mod:`warptap.svf_openocd_check`'s live SVF validation with the STAPL equivalent --
running warptap's own emitted STAPL output through code warptap didn't write, independent of
warptap's own test suite. This is what actually caught a real bug this session: ``to_stapl()``
computed its ``CRC`` statement one newline byte short of what JESD71 Annex B (and this
reference player's own ``jamcrc.c``) require, self-consistent in warptap's own tests but wrong
against ground truth -- see :mod:`warptap.tap_ir_stapl`'s ``to_stapl()`` for the fix.

**No off-the-shelf package exists for this** (unlike OpenOCD's plain ``apt install``) -- unlike
:mod:`warptap.svf_openocd_check`, this module cannot skip-and-explain via a package name; a
real player has to be built from source once per environment. To build one:

    git clone https://github.com/margro/jam-stapl.git
    cd jam-stapl/source
    sed -i 's/#define PORT WINDOWS_INPOUT32/#define PORT UNIX/' jamport.h
    gcc -O2 -o jam jamstub.c jamcomp.c jamexec.c jamcrc.c jamheap.c jamsym.c \\
        jamstack.c jamarray.c jamexp.c jamnote.c jamutil.c jamjtag.c -lm

``PORT UNIX`` is load-bearing, not incidental: it's what makes ``jam_jtag_io()`` (this
player's real-hardware TCK/TMS/TDI/TDO bit-banging function) fall through to its own built-in
``tdo = 0`` software-only stub -- confirmed directly by reading ``jamstub.c`` -- with zero code
changes needed, mirroring :mod:`warptap.svf_openocd_check`'s ``dummy`` adapter driver: no real
hardware, no parallel port, nothing to fail against. Then point ``WARPTAP_JAM_CMD`` at the
built binary (this module deliberately does not fall back to a bare ``jam`` on ``PATH`` the way
``iverilog``/``vvp``/``yosys``/``openocd`` do elsewhere in this project -- "jam" collides with
the name of an unrelated, real, historically-common Perforce/Jam build tool, and silently
running the wrong one would produce confusing, not just missing, results).

Because there's no real chip behind ``PORT UNIX``'s always-``0`` TDO, a ``COMPARE`` clause
expecting anything other than all-zero content will correctly fail its own comparison and
trigger this emitter's ``IF <result> == 0 THEN EXIT (1);`` enforcement line (confirmed directly
this session: the player reports ``Exit code = 1... Checking chain failure``, not a parse
error) -- exactly the intended, designed-for behavior, not a bug. Callers therefore either use
an all-zero expected value/mask to observe a clean pass, or treat ``Exit code = 1`` as proof the
enforcement path itself works, not as a grammar failure.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

DEFAULT_JAM_COMMAND = os.environ.get("WARPTAP_JAM_CMD")


def run_stapl_via_jam(
    stapl_text: str, *, action: str = "RUN", jam_command: str | None = None
) -> str:
    """Feed ``stapl_text`` through a real ``jam -a<action> <file>`` run. Returns the
    player's combined stdout+stderr (its version banner and CRC/NOTE/Export diagnostics go
    to stderr, the actual "Exit code = N... <status>" summary goes to stdout -- confirmed
    directly this session -- so this module combines both rather than making callers guess).

    Deliberately never raises on the subprocess's own exit code: this player's process
    always exits 0 regardless of the *STAPL program's* internal exit code (confirmed
    directly this session), so there is nothing meaningful to raise on -- callers grep the
    returned text for what they actually care about (e.g. "CRC matched", "Exit code = 0...
    Success", or "Exit code = 1" for a deliberately-triggered COMPARE-mismatch enforcement
    path -- see module docstring)."""
    command = jam_command or DEFAULT_JAM_COMMAND
    if command is None:
        raise RuntimeError(
            "no jam player command given and WARPTAP_JAM_CMD is not set -- see "
            "jamplayer_check.py's module docstring for how to build one"
        )
    with tempfile.TemporaryDirectory(prefix="warptap-jam-stapl-") as tmpdir:
        tmp = Path(tmpdir)
        stapl_name = "warptap.stapl"
        (tmp / stapl_name).write_text(stapl_text, encoding="utf-8")
        proc = subprocess.run(
            # -v (verbose) is required for the "CRC matched"/"CRC mismatch" and NOTE-field
            # diagnostic lines to appear at all -- without it, only the terse "Exit code =
            # N... <status>" summary is printed (confirmed directly this session).
            [command, "-v", f"-a{action}", stapl_name],
            capture_output=True,
            text=True,
            cwd=tmp,
        )
        return proc.stdout + proc.stderr
