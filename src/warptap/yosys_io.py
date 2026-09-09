"""Subprocess wrapper around a Yosys executable (plan §4.1).

warptap never links against Yosys; it shells out to it, exactly like faultflow does
for its own Yosys usage. The executable is configurable (``yosys_command``, or the
``WARPTAP_YOSYS_CMD`` environment variable) so tests can point at a pip-installed WASM
build (``yowasp-yosys``) while a real install defaults to the ``yosys`` binary on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from warptap.errors import WarptapError

DEFAULT_YOSYS_COMMAND = os.environ.get("WARPTAP_YOSYS_CMD", "yosys")


class YosysError(WarptapError):
    """Raised when a Yosys subprocess invocation exits nonzero."""

    def __init__(self, script: str, returncode: int, stdout: str, stderr: str):
        self.script = script
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"yosys exited with code {returncode}\n"
            f"--- script ---\n{script}\n"
            f"--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )


def run_yosys_script(
    script: str, *, yosys_command: str | None = None, cwd: Path | str | None = None
) -> str:
    """Run a Yosys script (commands separated by ``;``) and return captured stdout.

    ``cwd``, when given, is both the subprocess's working directory *and* the
    directory every file path in ``script`` should be expressed relative to. This
    matters beyond tidiness: WASI-sandboxed Yosys builds (e.g. ``yowasp-yosys``, used
    for local dev/test per implementation_plan.md) only map the process's working
    directory into their virtual filesystem — an absolute Windows path like
    ``C:\\Users\\...`` is not resolvable from inside that sandbox, only a bare
    relative filename is. A real ``yosys`` install has no such restriction, but
    writing every caller to use ``cwd``-relative paths keeps one code path that works
    against both.

    Raises :class:`YosysError` on nonzero exit.
    """
    command = yosys_command or DEFAULT_YOSYS_COMMAND
    proc = subprocess.run(
        [command, "-q", "-p", script],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if proc.returncode != 0:
        raise YosysError(script, proc.returncode, proc.stdout, proc.stderr)
    return proc.stdout


def ingest(
    verilog_files: list[Path | str],
    top: str,
    *,
    yosys_command: str | None = None,
    use_sv: bool = False,
) -> dict[str, Any]:
    """Ingest pass (plan §4.1 step 1): lower Verilog source(s) for ``top`` into
    Yosys's JSON netlist representation.

    Runs ``read_verilog; hierarchy -top <top>; proc; memory_collect; write_json``.
    This step is mandatory, not optional, before any pure-Python JSON surgery:
    ``write_json`` rejects modules that still contain unlowered behavioral processes
    (``$proc``/``always`` blocks).

    ``use_sv``, when set, adds ``-sv`` to ``read_verilog`` so SystemVerilog constructs
    (``always_comb``/``always_ff``, ``logic``) parse. Off by default: every existing
    caller passes plain Verilog-2001 sources, and this keeps their output byte-for-byte
    unaffected (plan §7 Stage 9 §5.3).
    """
    with tempfile.TemporaryDirectory(prefix="warptap-ingest-") as tmpdir:
        tmp = Path(tmpdir)
        local_names: list[str] = []
        for f in verilog_files:
            src = Path(f)
            if src.name in local_names:
                raise ValueError(f"duplicate Verilog source filename: {src.name!r}")
            shutil.copy(src, tmp / src.name)
            local_names.append(src.name)

        out_name = "netlist.json"
        sv_flag = "-sv " if use_sv else ""
        read_cmds = " ".join(f"read_verilog {sv_flag}{name};" for name in local_names)
        script = (
            f"{read_cmds} "
            f"hierarchy -top {top}; "
            f"proc; "
            f"memory_collect; "
            f"write_json {out_name}"
        )
        run_yosys_script(script, yosys_command=yosys_command, cwd=tmp)
        return json.loads((tmp / out_name).read_text(encoding="utf-8"))


def ingest_with_params(
    verilog_files: list[Path | str],
    top: str,
    chparams: dict[str, int],
    *,
    yosys_command: str | None = None,
    use_sv: bool = False,
) -> dict[str, Any]:
    """Like :func:`ingest`, but applies ``chparam -set <name> <value> ...`` to ``top``
    *before* ``hierarchy`` runs, baking in specific parameter values for a genuinely
    parameterized module (e.g. ``rtl/scan_mux_cell.v``'s ``ARMS``/``SEL_WIDTH``/
    ``ARM_VALUES``, which vary per :class:`~warptap.icl_model.ScanMuxNode` instance).

    Needed because ``hierarchy`` (part of plain :func:`ingest`'s own script) resolves every
    parameter-dependent port/register width to a concrete value AND strips the module's own
    overridability entirely -- confirmed empirically (not assumed): a module imported via
    plain ``ingest()`` and later instantiated with an ``add_cell(..., parameters={...})``
    override fails to compile (``iverilog`` reports "parameter ... not found in ..."),
    regardless of whether that specific parameter affects any port width at all. The only
    way to get a correctly-shaped, correctly-valued instance of such a module is to import a
    freshly, fully parameter-specialized copy of it -- one Yosys module per distinct
    parameter combination actually needed, instantiated with zero further overrides.

    ``chparam``'s own value syntax is a plain decimal integer (unlike Yosys JSON's own
    cell-``parameters`` binary-string encoding -- a different layer, a different format)."""
    with tempfile.TemporaryDirectory(prefix="warptap-ingest-") as tmpdir:
        tmp = Path(tmpdir)
        local_names: list[str] = []
        for f in verilog_files:
            src = Path(f)
            if src.name in local_names:
                raise ValueError(f"duplicate Verilog source filename: {src.name!r}")
            shutil.copy(src, tmp / src.name)
            local_names.append(src.name)

        out_name = "netlist.json"
        sv_flag = "-sv " if use_sv else ""
        read_cmds = " ".join(f"read_verilog {sv_flag}{name};" for name in local_names)
        chparam_flags = " ".join(f"-set {name} {value}" for name, value in chparams.items())
        script = (
            f"{read_cmds} "
            f"chparam {chparam_flags} {top}; "
            f"hierarchy -top {top}; "
            f"proc; "
            f"memory_collect; "
            f"write_json {out_name}"
        )
        run_yosys_script(script, yosys_command=yosys_command, cwd=tmp)
        return json.loads((tmp / out_name).read_text(encoding="utf-8"))


def write_verilog_from_json(
    netlist_json: dict[str, Any], *, yosys_command: str | None = None
) -> str:
    """Reload a Yosys JSON netlist dict and emit it back out as Verilog text.

    Used by the golden-file round-trip test (plan §4.3) and, later, by the final
    synthesis step's output stage.
    """
    with tempfile.TemporaryDirectory(prefix="warptap-emit-") as tmpdir:
        tmp = Path(tmpdir)
        in_name, out_name = "netlist.json", "out.v"
        (tmp / in_name).write_text(json.dumps(netlist_json), encoding="utf-8")
        script = f"read_json {in_name}; write_verilog {out_name}"
        run_yosys_script(script, yosys_command=yosys_command, cwd=tmp)
        return (tmp / out_name).read_text(encoding="utf-8")


def synthesize(
    netlist_json: dict[str, Any], *, synth_script: str, yosys_command: str | None = None
) -> dict[str, Any]:
    """Final-synth step (plan §4.1 step 3): reload a JSON netlist and run a
    caller-supplied Yosys script fragment over it, returning the re-synthesized JSON.

    ``synth_script`` is whatever the target flow's own synthesis recipe is, plus
    warptap's locked/preserving script fragment (plan §4.3) — this function doesn't
    choose or embed a specific recipe, it just runs whatever it's given.
    """
    with tempfile.TemporaryDirectory(prefix="warptap-synth-") as tmpdir:
        tmp = Path(tmpdir)
        in_name, out_name = "in.json", "out.json"
        (tmp / in_name).write_text(json.dumps(netlist_json), encoding="utf-8")
        script = f"read_json {in_name}; {synth_script}; write_json {out_name}"
        run_yosys_script(script, yosys_command=yosys_command, cwd=tmp)
        return json.loads((tmp / out_name).read_text(encoding="utf-8"))
