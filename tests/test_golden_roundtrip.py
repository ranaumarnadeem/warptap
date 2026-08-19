"""Golden-file round-trip regression test (implementation_plan.md §4.3, last bullet):
``write_json`` of an unmodified design -> warptap no-op pass -> ``read_json`` ->
``write_verilog``, diffed against reference. Required to pass before any real
insertion logic is written — the schema has real edge cases (behavioral-process
rejection, ``$mem`` representation, per-module bit-ID scoping) that fail silently if
violated.

Requires a Yosys-compatible executable (see conftest.py's ``yosys_command`` fixture).
"""

from __future__ import annotations

import copy
import json

from warptap.netlist import Netlist
from warptap.yosys_io import ingest, write_verilog_from_json


def test_ingest_lowers_the_trivial_fixture(fixtures_dir, yosys_command):
    """Sanity check that the fixture actually exercises `proc` (an always block)
    and produces a nonempty cell list, before trusting anything downstream."""
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    assert "trivial" in raw["modules"]
    assert raw["modules"]["trivial"]["cells"]


def test_netlist_wrap_is_lossless_at_the_json_level(fixtures_dir, yosys_command):
    """Loading ingested JSON into the Netlist wrapper and dumping it back out, with
    no edits made, must be byte-for-byte (dict-equal) identical to the input."""
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)
    original = copy.deepcopy(raw)

    netlist = Netlist.from_json(raw)

    assert netlist.to_json() == original


def test_write_verilog_is_stable_through_a_full_json_roundtrip(fixtures_dir, yosys_command):
    """The actual 'no-op pass' round-trip the plan calls for, exercised through a
    real Yosys read_json/write_verilog pass (not just Python-side dict equality):
    emitting Verilog straight from freshly-ingested JSON must match emitting Verilog
    from that same JSON after a full load/no-op/dump cycle through Netlist."""
    raw = ingest([fixtures_dir / "trivial.v"], "trivial", yosys_command=yosys_command)

    direct = write_verilog_from_json(raw, yosys_command=yosys_command)

    netlist = Netlist.from_json(json.loads(json.dumps(raw)))  # simulate a fresh load
    roundtripped = write_verilog_from_json(netlist.to_json(), yosys_command=yosys_command)

    assert direct == roundtripped
