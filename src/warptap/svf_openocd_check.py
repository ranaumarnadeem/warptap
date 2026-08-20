"""Subprocess wrapper around OpenOCD's ``svf`` command (implementation_plan.md §7 Stage 7,
§8's own testing strategy: "validated by feeding output through an existing consumer... not a
self-written parser"). Runs an emitted SVF file through a REAL, independent SVF player --
OpenOCD's ``dummy`` adapter driver (a software-only JTAG interface, no real hardware needed)
plus one explicitly-declared TAP, so OpenOCD's own bus auto-probe never runs: that auto-probe
assumes a real chip is present and fails loudly (attempting to walk a 32-tap chain, reporting
"IR capture error", etc.) against a driver with no chip behind it at all -- confirmed by
direct experimentation this session, irrelevant noise for a pure grammar/syntax check, not a
real failure.

Only ever validates GRAMMAR, not CONTENT: the dummy driver has no real chip, so an SVF op
carrying a TDO/MASK expected-value clause reports a content ("tdo check error") mismatch
against the dummy driver's own fixed TDO behavior -- expected, not a bug. What running an
emitted file through OpenOCD DOES prove, independent of warptap's own emitter code: every
literal it wrote (hex TDI/TDO/MASK, state names, RUNTEST cycle counts) is decoded by OpenOCD's
own, independently-implemented SVF parser exactly as intended -- confirmed directly this
session: OpenOCD's reported ``WANT``/``MASK`` in a tdo-check-error message matched warptap's
own emitted hex literals exactly, byte for byte.

``tap_irlen`` must match ``rtl/tap_core.v``'s actual ``IR_WIDTH`` (its own default of 4,
matching every other module's ``DEFAULT_IR_WIDTH`` convention) -- OpenOCD needs *some* IR
length to define the TAP it auto-probe-skips past, even though ``-ignore-version``/a fake
``-expected-id`` mean it never actually checks a real IDCODE against it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

DEFAULT_OPENOCD_COMMAND = os.environ.get("WARPTAP_OPENOCD_CMD", "openocd")


def run_svf_via_openocd(
    svf_text: str, *, tap_irlen: int = 4, openocd_command: str | None = None
) -> str:
    """Feed ``svf_text`` through a real ``openocd -f interface/dummy.cfg ... svf <file>``
    run. Returns OpenOCD's combined stdout+stderr (it writes its own diagnostics and the
    ``svf`` command's own pass/fail summary to stderr, confirmed directly this session, not
    stdout) for the caller to inspect.

    Deliberately never raises on OpenOCD's exit code: OpenOCD exits nonzero even for a
    content-only "tdo check error" against the dummy driver's fixed (non-warptap) TDO
    behavior -- expected, not a real failure (see module docstring). Callers grep the
    returned text themselves for what they actually care about (e.g. "programmed
    successfully" for a write-only file, or that a reported ``WANT``/``MASK`` matches what
    they emitted for a file with reads).
    """
    command = openocd_command or DEFAULT_OPENOCD_COMMAND
    with tempfile.TemporaryDirectory(prefix="warptap-openocd-svf-") as tmpdir:
        tmp = Path(tmpdir)
        svf_name = "warptap.svf"
        (tmp / svf_name).write_text(svf_text, encoding="utf-8")
        proc = subprocess.run(
            [
                command,
                "-f", "interface/dummy.cfg",
                "-c", "transport select jtag",
                "-c",
                f"jtag newtap warptap tap -irlen {tap_irlen} "
                "-expected-id 0x00000000 -ignore-version",
                "-c", "init",
                "-c", f"svf {svf_name}",
                "-c", "shutdown",
            ],
            capture_output=True,
            text=True,
            cwd=tmp,
        )
        return proc.stdout + proc.stderr
