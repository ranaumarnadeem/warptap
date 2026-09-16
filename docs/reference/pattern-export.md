# Pattern Export

The shared, format-independent TAP-transaction op vocabulary (`warptap.tap_ir`), and the
SVF/STAPL/STIL text emitters that render it.

::: warptap.tap_ir
    options:
      members: [GotoState, ShiftIR, ShiftDR, Runtest, PulsePin, bits_from_int, bits_to_int]

::: warptap.tap_ir_svf
    options:
      members: [TapIrSvfError, to_svf]

::: warptap.tap_ir_stapl
    options:
      members: [TapIrStaplError, to_stapl]

::: warptap.tap_ir_stil
    options:
      members: [TapIrStilError, to_stil]
