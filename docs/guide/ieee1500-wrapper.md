# IEEE 1500 wrappers

A block wrapped to IEEE 1500 is tested through its wrapper serial port (WSP): the wrapper
instruction register (WIR), the bypass register (WBY) and the boundary register (WBR) are
reached with WSI, WSO and a handful of control signals, as a TAP's registers are reached with
TDI, TDO and TMS. warptap handles that port the way it handles a TAP: one model, the ICL and
PDL derived from it, scan operations that play on it, and an integrity program that tests the
wrapper itself.

Like the TAP and the IJTAG network, the wrapper's access logic -- the WIR, WBY and their
decode -- is a **test instrument**. It is tested with an ICL description and the PDL-retargeted
program below, not with scan ATPG; the boundary cells themselves are ordinary logic and are
graded by scan as the rest of the core is.

## The model

`Wrapper1500` describes a wrapper: its boundary cells in `chains` (each wrapper scan chain
head first, each cell a `WbrCell(label, side)` with side `"input"` or `"output"`), the WIR's
width, its instructions and its port names. `Wsp1500Sim` steps one wrapper a cycle at a
time; `Wsp1500Chain` steps wrappers whose WSI and WSO are chained, as a SoC top chains them.
`WspChainDesign` describes such a top: the wrappers (`u0`, `u1`, ...) and the SoC's own WSP
pins.

- The WIR has a shift stage (WSI into bit 0, the last bit out to WSO) that shifts on the rising
  edge of WRCK while SelectWIR and ShiftWR are 1, and an update stage -- the instruction in force --
  that takes the shift stage on the *falling* edge while SelectWIR and UpdateWR are 1. WRSTN at 0
  clears the update stage at once: WS_BYPASS, the functional mode. A code no instruction has acts
  as WS_BYPASS.
- WBY is one bit that captures 0 and shifts.
- The WBR is the data register of a *serial* instruction (WS_EXTEST, WS_INTEST) from WSI to WSO,
  the wrapper chains one after another. A *parallel* instruction (WP_*) works the cells on the
  wrapper's own scan ports instead.
- Each instruction puts the cells in a mode, (INTEST, EXTEST). An input cell holds its flop in
  INTEST and drives the core 0 in EXTEST; an output cell holds in EXTEST and drives its port 0 in
  INTEST. In a serial instruction every cell also holds unless the WIR is deselected and CaptureWR
  or ShiftWR is 1.

Values are 0, 1 or `None` (unknown): every register starts unknown but what WRSTN clears.

## ICL

`to_icl_1500(wrapper)` writes the wrapper as one ICL module beside a stub of its core. The ICL has
the standard shape (`ScanInterface WSP` and `WPP`, `ScanRegister WIR_SR`, `DataRegister WIR` with
an `Enum` of the instruction names, `ScanRegister WBY`, one `ScanRegister WBR<k>` per wrapper
chain, `ScanMux`es for WSO and the serial path, `DataMux`es for the cells' modes), so any
IEEE 1687 tool reads it, and the vendored parser checks it builds the retargeting graph.
`to_icl_1500_chain(wrappers, top="soc", pins=...)` writes each wrapper's module and a SoC top that
instantiates them as `u0`, `u1`, ... and chains them WSI to WSO under shared controls; `pins` maps a
WSP role to the SoC port that drives it and whether an inverter sits between.

The scan path of an ICL `ScanRegister` enters its MSB and leaves its LSB, and a WIR shifts its code
in last bit first, so **the value ICL gives an instruction is its code with the bits reversed**:
`iWrite WIR WS_EXTEST` and `iWrite WIR 3'b100` shift in the same bits.

`import_icl_1500(text)` rebuilds the model from the `warptap_*` attributes the emitter writes,
regenerates the ICL and requires the result to equal the text byte for byte. Anything else -- a SIB
network, a third tool's wrapper, this ICL with its WIR or WBY edited out -- is refused.

## PDL

`Wsp1500Interpreter` takes `iReset`, `iTarget`, `iWrite`, `iRead`, `iRunLoop` and `iApply` on a
wrapper, or on the chain `u0`, `u1`, ..., and retargets them onto the WSP as the scan operations of
[Pattern Export](../reference/pattern-export.md) (`ShiftIR` for a WIR scan, `ShiftDR` for a data
scan, `Runtest` for idle cycles, and `WspReset` for WRSTN).

```python
from warptap import Wsp1500Interpreter, to_pdl_1500

pdl = Wsp1500Interpreter(design)           # a Wrapper1500, or a WspChainDesign
pdl.iReset()
pdl.iWrite("u0.WIR", "WP_INTEST")
pdl.iWrite("u1.WIR", "WS_EXTEST")
pdl.iApply()                               # one WIR scan, the far wrapper's bits first
pdl.iWrite("u1.WBR1", 1)
pdl.iRead("u1.WBR0", 0b01)
pdl.iApply()                               # one data scan through whatever each WIR selects
print(to_pdl_1500(pdl.history, design))
```

A wrapper not written keeps its committed instruction, which must be known (an `iReset`, or a
write). A register the instruction in force doesn't select is refused. The WIR is not read: its
shift stage has no capture. `import_pdl_1500` reads the printed subset back.

`to_wsp_rows(pdl.program)` lowers the operations to tester cycles, each with the WSO its strobe
should show. A `ShiftIR` is SelectWIR and ShiftWR for each bit then SelectWIR and UpdateWR; a
`ShiftDR` is a CaptureWR cycle, the shifts with the WIR deselected, then UpdateWR.

## The integrity program

`build_wsp_integrity_program(design)` returns the cycles a production flow plays on the WSP to test
the wrapper's own infrastructure, with the value the model expects at every observation point on
every cycle: WSO, each wrapped output port (for a single wrapper) and each wrapper chain's scan
output. In order:

| Test | What it shows |
| --- | --- |
| `reset` | WRSTN clears every WBY: a sentinel comes out as many shifts late as there are wrappers. |
| `wir_length` | A sentinel through the WIRs comes out as many shifts late as they are long; the pattern ends WS_BYPASS. |
| `code_<bits>` | Every code of the WIR, one wrapper at a time (`u<k>_code_<bits>` in a chain), the others in bypass; an unused code acts as WS_BYPASS. The data register it selects is scanned with a sentinel. A serial instruction's boundary register is scanned three times, each starting with a capture from the driven ports (~P, P, P, P alternating) and leaving a tail in the register (0, 1, 0): a cell that follows its port where its mode says hold, or holds where it should capture, shows in one of them. Then idle cycles. |
| `wby_hold` | WBY loaded with 1 and a WIR scan of zeros, then with 0 and a WIR scan carrying ones: the bypass bit holds its value through a WIR scan and update. |
| `held_update` | An update with the WIR deselected, after the complement of the code in force is shifted into the WIR, leaves the instruction in force. |
| `capture_update` | CaptureWR with the WIR selected doesn't capture the boundary cells. |

What the program cannot know -- the core's outputs, which an output cell captures -- is *no care*:
the model runs twice, with those at 0 and at 1, and an observation is checked only where both
runs agree.

`program.to_json()` is a `warptap-wsp-program` (version 1): one column per WSP signal, the data
input ports it drives, the observation points, the tests as `start`/`stop` ranges and, per
observation point, an `expect` value string and a `care` string. A tool without warptap can read
and play it, and `WspIntegrityProgram.from_json` reads it back. `check_wsp_integrity(program,
observed)` returns the (step, point) pairs where what was measured differs from a care bit;
`replay_wsp_program(program, design)` produces the values the model gives, which is how a changed
or faulty wrapper is compared against the program.
