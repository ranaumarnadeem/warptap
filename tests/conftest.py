from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest


def _default_yosys_command() -> str:
    if os.environ.get("WARPTAP_YOSYS_CMD"):
        return os.environ["WARPTAP_YOSYS_CMD"]
    if shutil.which("yosys"):
        return "yosys"
    # No system Yosys on PATH — fall back to the yowasp-yosys console-script
    # installed alongside this interpreter (e.g. in a dev venv), per
    # implementation_plan.md's note on using the WASM build for local dev/test.
    candidate = Path(sys.executable).with_name(
        "yowasp-yosys.exe" if os.name == "nt" else "yowasp-yosys"
    )
    if candidate.exists():
        return str(candidate)
    return "yosys"  # let it fail loudly with a clear "not found" error


@pytest.fixture(scope="session")
def yosys_command() -> str:
    return _default_yosys_command()


def _default_tool_command(env_var: str, tool_name: str) -> str | None:
    if os.environ.get(env_var):
        return os.environ[env_var]
    if shutil.which(tool_name):
        return tool_name
    return None  # unlike Yosys, no WASM fallback exists for Icarus Verilog


@pytest.fixture(scope="session")
def iverilog_command() -> str:
    cmd = _default_tool_command("WARPTAP_IVERILOG_CMD", "iverilog")
    if cmd is None:
        pytest.skip("iverilog not found on PATH and WARPTAP_IVERILOG_CMD not set")
    return cmd


@pytest.fixture(scope="session")
def vvp_command() -> str:
    cmd = _default_tool_command("WARPTAP_VVP_CMD", "vvp")
    if cmd is None:
        pytest.skip("vvp not found on PATH and WARPTAP_VVP_CMD not set")
    return cmd


@pytest.fixture(scope="session")
def openocd_command() -> str:
    cmd = _default_tool_command("WARPTAP_OPENOCD_CMD", "openocd")
    if cmd is None:
        pytest.skip("openocd not found on PATH and WARPTAP_OPENOCD_CMD not set")
    return cmd


@pytest.fixture(scope="session")
def jam_command() -> str:
    """Deliberately env-var-only, no PATH fallback (unlike every other *_command fixture
    here) -- "jam" collides with the name of an unrelated, real, historically-common
    Perforce/Jam build tool, and a bare PATH lookup risks silently running the wrong
    program. See jamplayer_check.py's module docstring for how to build a real one."""
    cmd = os.environ.get("WARPTAP_JAM_CMD")
    if cmd is None:
        pytest.skip(
            "WARPTAP_JAM_CMD not set -- see jamplayer_check.py's module docstring for how "
            "to build a real Jam STAPL Player"
        )
    return cmd


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def _default_openmbist_dir() -> Path:
    if os.environ.get("WARPTAP_OPENMBIST_DIR"):
        return Path(os.environ["WARPTAP_OPENMBIST_DIR"])
    # tests/conftest.py -> tests -> tapestry -> tapestry's own parent: openMBIST is a sibling
    # checkout, not nested inside this repo (implementation_plan.md §7 Stage 9 §5.2).
    return Path(__file__).resolve().parents[2] / "openMBIST"


@pytest.fixture(scope="session")
def openmbist_dir() -> Path:
    """The real openMBIST checkout this project cross-simulates Stage 9's functional
    instruments against -- a sibling project, never vendored into tapestry (implementation_
    plan.md §7 Stage 9 §5.2's own licensing note: read live from the sibling checkout at test
    time). Skips (not fails) when not found, mirroring iverilog_command/vvp_command's own
    "optional external dependency" discipline."""
    candidate = _default_openmbist_dir()
    if not candidate.is_dir():
        pytest.skip(f"openMBIST checkout not found at {candidate} (set WARPTAP_OPENMBIST_DIR)")
    return candidate


@pytest.fixture(scope="session")
def autombist_generator(openmbist_dir: Path):
    """``autombist.generator.generate_from_config`` imported live from the sibling openMBIST
    checkout's own ``src/`` tree (confirmed pure Python + Jinja2, no subprocess/WSL/cocotb
    needed) -- same skip-if-not-importable guard as ``openmbist_dir``, not a hard failure,
    since a missing/incompatible sibling checkout is an environment gap, not a warptap bug."""
    src_dir = str(openmbist_dir / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from autombist.generator import generate_from_config
    except ImportError as exc:
        pytest.skip(f"autombist.generator not importable from {src_dir}: {exc}")
    return generate_from_config


def _default_icl_parser_dir() -> Path:
    if os.environ.get("WARPTAP_ICL_PARSER_DIR"):
        return Path(os.environ["WARPTAP_ICL_PARSER_DIR"])
    # tests/conftest.py -> tests -> tapestry -> third_party/icl_parser (a git submodule
    # checked out inside this repo, unlike openmbist_dir's sibling-checkout convention --
    # icl_parser is vendored, per implementation_plan.md §7 Stage 10's own reasoning: it has
    # no setup.py/pyproject.toml, so pip install isn't available, and there's no established
    # precedent here for silently copying external source with no update mechanism).
    return Path(__file__).resolve().parents[1] / "third_party" / "icl_parser"


@pytest.fixture(scope="session")
def icl_parser_dir() -> Path:
    candidate = _default_icl_parser_dir()
    if not candidate.is_dir() or not any(candidate.iterdir()):
        pytest.skip(
            f"third_party/icl_parser not checked out at {candidate} -- run "
            "`git submodule update --init third_party/icl_parser`"
        )
    return candidate


@pytest.fixture(scope="session")
def icl_parser_module(icl_parser_dir: Path):
    """``Ijtag`` (the vendored ``Honza255/icl_parser``'s own public API class) imported live
    from ``third_party/icl_parser`` -- MIT-licensed, git-submodule-vendored (see
    ``icl_parser_dir``), not pip-installable. Requires ``antlr4-python3-runtime`` (pinned to
    4.7.2, matching the exact version the submodule's checked-in generated lexer/parser were
    produced by -- ANTLR-generated code is not reliably forward/backward compatible across
    runtime versions), ``z3-solver``, ``sympy``, and ``networkx`` -- dev/test only, never a
    runtime dependency of warptap itself (``pyproject.toml``'s own ``dependencies = []`` stays
    empty). Skips (not fails) when the submodule isn't checked out or any of those aren't
    importable, matching every other external-tool fixture's "optional dependency" discipline
    in this file."""
    src_dir = str(icl_parser_dir)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        from src.ijtag import Ijtag
    except ImportError as exc:
        pytest.skip(
            f"icl_parser not importable from {src_dir}: {exc} -- run `git submodule update "
            "--init third_party/icl_parser` and `pip install antlr4-python3-runtime==4.7.2 "
            "z3-solver networkx` (sympy is a warptap dependency already)"
        )
    return Ijtag
