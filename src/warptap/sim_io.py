"""Subprocess wrapper around Icarus Verilog (``iverilog``/``vvp``), used to cross-simulate
hand-authored RTL (e.g. ``rtl/tap_core.v``) against its Python behavioral-model counterpart
(implementation_plan.md §7 Stage 2).

Mirrors ``yosys_io.py``'s established pattern deliberately: a custom ``*Error`` carrying
command/returncode/stdout/stderr, ``tempfile.TemporaryDirectory`` + ``cwd=tmp`` + relative
filenames (same reason as ``yosys_io.py`` — one code path that works whether paths are native
or sandboxed), and ``WARPTAP_*_CMD`` environment-variable overrides.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from warptap.errors import WarptapError

DEFAULT_IVERILOG_COMMAND = os.environ.get("WARPTAP_IVERILOG_CMD", "iverilog")
DEFAULT_VVP_COMMAND = os.environ.get("WARPTAP_VVP_CMD", "vvp")


class SimError(WarptapError):
    """Raised when an ``iverilog`` compile or ``vvp`` run exits nonzero."""

    def __init__(self, command: list[str], returncode: int, stdout: str, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"{command[0]} exited with code {returncode}\n"
            f"--- command ---\n{' '.join(command)}\n"
            f"--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )


def _run(command: list[str], *, cwd: Path) -> str:
    proc = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise SimError(command, proc.returncode, proc.stdout, proc.stderr)
    return proc.stdout


def run_verilog_testbench(
    verilog_files: list[Path | str],
    *,
    extra_inputs: dict[str, str] | None = None,
    iverilog_command: str | None = None,
    vvp_command: str | None = None,
) -> str:
    """Compile ``verilog_files`` with ``iverilog`` and run the result with ``vvp``,
    returning captured stdout (whatever the testbench ``$display``s).

    ``extra_inputs`` (e.g. a generated stimulus file a testbench reads via ``$fscanf``)
    are written into the same cwd-relative temp directory the Verilog sources are copied
    into, under the given filenames — the testbench should read them by that bare name,
    same discipline as ``yosys_io.ingest()``.
    """
    with tempfile.TemporaryDirectory(prefix="warptap-sim-") as tmpdir:
        tmp = Path(tmpdir)
        local_names: list[str] = []
        for f in verilog_files:
            src = Path(f)
            if src.name in local_names:
                raise ValueError(f"duplicate Verilog source filename: {src.name!r}")
            shutil.copy(src, tmp / src.name)
            local_names.append(src.name)

        for name, content in (extra_inputs or {}).items():
            (tmp / name).write_text(content, encoding="utf-8")

        out_name = "sim.vvp"
        iverilog = iverilog_command or DEFAULT_IVERILOG_COMMAND
        vvp = vvp_command or DEFAULT_VVP_COMMAND
        _run([iverilog, "-g2012", "-o", out_name, *local_names], cwd=tmp)
        return _run([vvp, out_name], cwd=tmp)


def tool_available(command: str) -> bool:
    """Whether ``command`` resolves to a real executable, either on PATH or as an
    absolute/relative path that exists — used by conftest.py to skip cross-sim tests
    cleanly when Icarus Verilog isn't installed, rather than failing."""
    return shutil.which(command) is not None or Path(command).exists()
