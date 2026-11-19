# Gate-level evidence (IHP SG13G2)

warptap's other checks simulate the inserted RTL. These map inserted designs to a real open
standard-cell library, IHP's SG13G2 (130 nm, Apache-2.0), simulate the mapped netlists with the
library's own cell models, and run static timing analysis on them with OpenSTA. They live in the
test suite (`tests/gate_level.py`, `tests/test_gate_level_sg13g2.py`, `tests/test_sta_sg13g2.py`),
not in the library: they are evidence about what warptap inserts, not a flow it provides.

## What is mapped

Each design is inserted the way its RTL cross-simulation inserts it, and its netlist is mapped
with Yosys, keeping the hierarchy: warptap marks its own modules (`tap_core`, the SIB and
instrument cells) `keep_hierarchy`, as a user's synthesis keeps them. Every tri-state driver,
warptap's `$tribuf` on TDO and the boundary register's output3 pins as well as a design's own
`assign io = en ? v : 1'bz`, becomes an `sg13g2_ebufn_2`; the rest is `dfflibmap` and `abc`
against the typical-corner liberty. The mapped netlist must hold only library cells (none marked
`dont_use`), the design's own modules and its macros, with one `sg13g2_ebufn_2` per tri-state
driver.

| Design | Instruments | Library cells |
|---|---|---|
| `real_signal` with a SIB network | 1 | 234 |
| `trivial` with a boundary-scan register | | 228 |
| alexforencich/verilog-uart's `uart_tx` | 3 | 468 |
| openMBIST's `mem_subsystem_mbist` (three SRAM macros kept as macros) | 8 | 1941 |

## Simulation

Each design's existing RTL cross-simulation runs again on the mapped netlist, unchanged: its own
stimulus, testbench and assertions, with the cell models compiled in. Its trace must match the
RTL run's row for row:

- the tri-stated TDO pin: a bit in Shift-IR and Shift-DR, `z` elsewhere, moving only at falling
  edges of TCK, sampled before the rising edge, after it and after the falling edge;
- the boundary register's EXTEST path, and a BC_7 cell over a design's own tri-state driver;
- `uart_tx` transmits the byte written through JTAG, and three reads of `busy` pass;
- `mem_subsystem_mbist`'s self-repair start/busy access through its eight-instrument network.

Two differences from the RTL are allowed, each in the one test that hits it, and both are the
RTL's semantics rather than a circuit's: an unreset register in openMBIST's read mux makes the
RTL's `case` read 0 where the mapped mux reads `x` in the first reset cycle, and a register that
samples a floating pin holds `z` in the RTL, which a Verilog `reg` can, where a flip-flop holds
`x`.

The cell models run in their power-aware mode (`USE_POWER_PINS`, every cell tied to VDD and
VSS). In the other mode each output goes through a `buf` gate, which turns `z` into `x`, so a
released tri-state pin could never read `z`.

Control: mapping the tri-state buffer without inverting its active-low enable fails the TDO
rules.

## Static timing

OpenSTA times the same mapped netlists against `tests/fixtures/sta/warptap.sdc` at two corners
of the library: slow (1.08 V, 125 C) and fast (1.32 V, -40 C). Every setup, hold, recovery,
removal, pulse-width and period check runs at both, and at the stated TCK, 50 MHz (20 ns), all
of them must pass. The SDC's comments say what it assumes:

- a tester that changes TDI and TMS, and releases TRST, on TCK's falling edge and samples TDO on
  its rising edge, with a tenth of a period of skew each way; warptap's TAP launches TDO on the
  falling edge, so TDO's path to the tester is half a cycle;
- each design's own clock (`clk`, 20 ns) asynchronous to TCK: an instrument hands the design a
  static level, and no access relies on a timed path from one domain to the other;
- inputs driven by the library's strongest buffer (a pad), outputs loaded with 10 fF.

Worst slack at 50 MHz, in ns (setup at the slow corner, hold at the fast one), and the fastest
TCK at which every check still passes:

| Design | Setup | Hold | Fastest passing TCK |
|---|---|---|---|
| `real_signal` with a SIB network | 6.573 | 0.178 | 6.86 ns (146 MHz) |
| `trivial` with a boundary-scan register | 6.572 | 0.178 | 6.86 ns (146 MHz) |
| `uart_tx` | 6.543 | 0.179 | 6.92 ns (145 MHz) |
| `mem_subsystem_mbist` | 6.561 | 0.178 | 6.88 ns (145 MHz) |

The worst paths are in `tap_core`, the same in every design. The setup-critical one runs from a
rising-edge TCK flop through the TDO output mux to the TDO flop, which launches on the falling
edge: half a cycle, about 3.4 ns at the slow corner, which is what limits TCK to about 145 MHz.
The hold-critical one is a bit of the IDCODE shift register to the next. TRST's recovery slack
is 7.7 ns and its removal slack 2.06 ns. With TCK at 0.2 ns, setup fails, as it must.

Design rules (maximum slew) are reported but not checked: with no buffering, a few high-fanout
nets inside `tap_core`, and one in openMBIST's own logic, exceed the library's 2.51 ns slew limit
(up to 3.0 ns), and their delays are computed past the liberty tables' range.

## What this does not show

- Silicon, or a commercial tool: none is involved.
- Timing in simulation: the cell models' path delays are zero, and Icarus Verilog does not run
  their timing checks; the STA is what checks timing.
- Placed timing: the netlist has no placement, wires, buffering or clock tree, so a net's delay
  is its pins' load.
- The SRAM macros' timing: they are black boxes to the STA, so paths into and out of them are not
  timed.

## Running it

The library's five files (the typical, slow and fast liberty files and the two Verilog model
files) are found through `WARPTAP_IHP_STDCELL_DIR`, or in the copy `ciel` installs, and each is
checked against its git blob SHA at a pinned IHP-Open-PDK commit;
`python tests/gate_level.py fetch <directory>` downloads them, as CI does. The gate-level
simulations then run with the rest of the suite and skip without the library. The STA needs
OpenSTA (`WARPTAP_STA_CMD`, or `sta` on `PATH`), built from source with CUDD; CI doesn't build
it, so the STA tests skip there.
