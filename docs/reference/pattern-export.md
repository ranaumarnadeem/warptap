# Pattern Export

The shared, format-independent TAP-transaction op vocabulary (`warptap.tap_ir`), and the
SVF/STAPL/STIL text emitters that render it.

## A tester-ready STIL file

`to_stil` always declares TCK, TMS, TDI and TDO. Declare the DUT's other pins as well:
`inputs` with the value each one holds, `outputs` (never compared), one signal per bit of a
bus. Start the pattern with a reset lead-in that clocks TCK while TRST is low:

```python
from warptap import GotoState, SetPins, select_instruction, to_stil
from warptap.tap_fsm import TapState
from warptap.tap_model import OPCODE_EXTEST

lead_in = [
    SetPins((("trst_n", 0), ("rst_n", 0))),  # assert TRST and the chip reset
    GotoState(TapState.TEST_LOGIC_RESET),    # five TCK cycles with TMS=1
    SetPins((("trst_n", 1), ("rst_n", 1))),  # release both
    GotoState(TapState.RUN_TEST_IDLE),       # one TMS=0 cycle
]
stil = to_stil(
    lead_in + select_instruction(OPCODE_EXTEST) + pdl.program,
    jtag_period="50ns",
    pulse_periods={"clk": "10ns"},
    inputs={"trst_n": 1, "rst_n": 1, "clk": 0, "func_addr[0]": 0, "func_addr[1]": 1},
    outputs=["func_dout[0]", "func_dout[1]"],
)
```

In each TCK cycle TMS and TDI change at 0, TDO is compared at a quarter period, and TCK rises
at half and falls at three quarters; `to_stil` below says why. A `Runtest` or `PulsePin` of
many cycles is written as one `Loop`. Releases up to 0.0.3 compared TDO after the rising edge,
so on warptap's own TAP every compare checked the next bit.

::: warptap.tap_ir
    options:
      members: [GotoState, ShiftIR, ShiftDR, Runtest, PulsePin, SetPins, bits_from_int, bits_to_int]

::: warptap.tap_ir_svf
    options:
      members: [TapIrSvfError, to_svf]

::: warptap.tap_ir_stapl
    options:
      members: [TapIrStaplError, to_stapl]

::: warptap.tap_ir_stil
    options:
      members: [TapIrStilError, to_stil]
