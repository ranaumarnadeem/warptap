"""warptap — inserts IEEE 1149.1 (JTAG/TAP) and IEEE 1687 (IJTAG/ICL+PDL) test-access
infrastructure into a design.

This module re-exports the real entry points a caller actually needs -- everything else stays
reachable via its own module (``warptap.<module>``), this is a curated surface, not a
flattening of every internal helper. See README.md's own Usage section for an end-to-end
example using these names.
"""

from __future__ import annotations

from warptap.bsr_insert import BsrInsertError, insert_bsr
from warptap.errors import WarptapError
from warptap.faultflow_retarget import FaultflowRetargetError, retarget_faultflow_patterns
from warptap.icl_emit import IclEmitError, to_icl
from warptap.icl_import import IclImportError, import_icl
from warptap.icl_model import (
    ICLAddressError,
    InstrumentDirection,
    InstrumentNode,
    ModuleInstance,
    PhysicalGraph,
    SibNode,
    SignalBinding,
    resolve_dotted_address,
)
from warptap.netlist import Netlist
from warptap.pdl_emit import PdlEmitError, to_pdl
from warptap.pdl_interpreter import PDLError, PDLInterpreter
from warptap.pdl_verify import PDLVerifyError, ReadCheckResult, check_reads, correlate_observed
from warptap.pipeline import insert_test_access
from warptap.sib_insert import SibInsertError, insert_sib_network
from warptap.sib_plan import InstrumentSpec, build_sib_plan
from warptap.tap_ir import (
    GotoState,
    PulsePin,
    Runtest,
    ShiftDR,
    ShiftIR,
    bits_from_int,
    bits_to_int,
)
from warptap.tap_ir_stapl import TapIrStaplError, to_stapl
from warptap.tap_ir_stil import TapIrStilError, to_stil
from warptap.tap_ir_svf import TapIrSvfError, to_svf
from warptap.yosys_io import YosysError, ingest, write_verilog_from_json

__version__ = "0.0.1"

__all__ = [
    "__version__",
    # errors
    "WarptapError",
    "BsrInsertError",
    "FaultflowRetargetError",
    "IclEmitError",
    "IclImportError",
    "ICLAddressError",
    "PdlEmitError",
    "PDLError",
    "PDLVerifyError",
    "SibInsertError",
    "TapIrStaplError",
    "TapIrStilError",
    "TapIrSvfError",
    "YosysError",
    # netlist / ingest / re-emit
    "Netlist",
    "ingest",
    "write_verilog_from_json",
    # network model
    "InstrumentDirection",
    "InstrumentNode",
    "ModuleInstance",
    "PhysicalGraph",
    "SibNode",
    "SignalBinding",
    "resolve_dotted_address",
    # insertion
    "InstrumentSpec",
    "build_sib_plan",
    "insert_bsr",
    "insert_sib_network",
    "insert_test_access",
    # PDL
    "PDLInterpreter",
    "to_pdl",
    "check_reads",
    "correlate_observed",
    "ReadCheckResult",
    # ICL
    "to_icl",
    "import_icl",
    # tap_ir / pattern export
    "GotoState",
    "PulsePin",
    "Runtest",
    "ShiftDR",
    "ShiftIR",
    "bits_from_int",
    "bits_to_int",
    "to_svf",
    "to_stapl",
    "to_stil",
    # faultflow
    "retarget_faultflow_patterns",
]
