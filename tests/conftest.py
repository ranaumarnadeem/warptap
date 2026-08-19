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


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
