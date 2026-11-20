"""Import one ITC'16 benchmark network in a process of its own and print the outcome as one
JSON line, prefixed with ``RESULT``. ``test_icl_import_itc16.py`` runs this once per network,
so a slow parse can be timed out and a big one's memory goes back when it ends.

For a network that imports, the line also carries the parser's own inventory of the elaborated
design (every scan register, its bits, every ScanMux), which the test checks the imported graph
against, and whether the graph round-trips through :func:`~warptap.icl_emit.to_icl`.

usage: python itc16_import_worker.py '{"top": "q12710::M0", "files": ["...", ...]}'
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from icl_reimport import reimport  # noqa: E402

from warptap import _vendor  # noqa: E402
from warptap.icl_import import IclImportError, import_icl  # noqa: E402
from warptap.icl_model import ScanMuxNode  # noqa: E402


def shape(chain, depth: int = 1) -> dict:
    """What a chain holds: its SIBs, ScanMuxes and instruments, their register bits (a SIB's
    one, a ScanMux's select register, an instrument's width), and how deep slots nest."""
    totals = {"sibs": 0, "scan_muxes": 0, "instruments": 0, "bits": 0, "depth": depth if chain else 0}

    def add(nested, instrument):
        if instrument is not None:
            totals["instruments"] += 1
            totals["bits"] += instrument.width
        if nested:
            inner = shape(nested, depth + 1)
            for key in ("sibs", "scan_muxes", "instruments", "bits"):
                totals[key] += inner[key]
            totals["depth"] = max(totals["depth"], inner["depth"])

    for slot in chain:
        if isinstance(slot, ScanMuxNode):
            totals["scan_muxes"] += 1
            totals["bits"] += slot.select_width
            for arm in slot.arms:
                add(arm.nested, arm.instrument)
        else:
            totals["sibs"] += 1
            totals["bits"] += 1
            add(slot.nested, slot.instrument)
    return totals


def inventory(instance, classes) -> dict:
    """The parser's count of the elaborated design under ``instance``: scan registers, their
    bits, and ScanMuxes, over every instance."""
    module = sys.modules[classes.__module__]
    totals = {"registers": 0, "bits": 0, "scan_muxes": 0}
    stack = [instance]
    while stack:
        current = stack.pop()
        for register in current.get_icl_item_type(module.IclScanRegister):
            totals["registers"] += 1
            totals["bits"] += register.get_vector_size()
        totals["scan_muxes"] += len(current.get_icl_item_type(module.IclScanMux))
        stack.extend(current.get_icl_item_type(module.IclInstance))
    return totals


def _names(chain) -> list:
    return [slot.mux_name if isinstance(slot, ScanMuxNode) else slot.sib_name for slot in chain]


def main() -> None:
    args = json.loads(sys.argv[1])
    ijtag_class = _vendor.ijtag("import_icl")
    parsed = {}

    def parse_once(top, files, **kwargs):
        """The vendored Ijtag, keeping what it built for the inventory."""
        parsed["ijtag"] = ijtag_class(top, files, **kwargs)
        return parsed["ijtag"]

    parse_once.__module__ = ijtag_class.__module__  # import_icl finds the item classes by it

    start = time.monotonic()
    try:
        graph, root = import_icl(
            [Path(f) for f in args["files"]], args["top"], icl_parser_module=parse_once
        )
    except IclImportError as exc:
        result = {"outcome": "rejected", "message": str(exc)}
    else:
        result = {
            "outcome": "imports",
            "shape": shape(graph.chain),
            "parser_inventory": inventory(parsed["ijtag"].icl_instance, ijtag_class),
            "top_chain": _names(graph.chain),
            "instruments": [child.name for child in root.children],
        }
        with tempfile.TemporaryDirectory(prefix="warptap-itc16-") as directory:
            again, again_root = reimport(graph, root, Path(directory), None)
        result["round_trips"] = again == graph and again_root == root
    result["seconds"] = round(time.monotonic() - start, 1)
    print("RESULT " + json.dumps(result))


if __name__ == "__main__":
    main()
