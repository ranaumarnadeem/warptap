# Network Integrity

The TCK-level program a production flow plays to test the TAP and the IJTAG network themselves --
IDCODE, the instruction register, BYPASS and every unimplemented opcode, each SIB and ScanMux arm
opened alone, each WRITE instrument written and read back, a TMS reset -- with the TDO it expects
from warptap's own TAP and network models. See the module docstring below for the tests in order
and for how a READ instrument bound to a design signal makes its TDO bits don't-care.

```python
from warptap import build_integrity_program, check_integrity

program = build_integrity_program(graph, root)

# Your harness: drive tms/tdi/trst_n for one TCK period, return tdo sampled before TCK rises.
observed = [tck_period(c.tms, c.tdi, c.trst_n) for c in program.cycles]

result = check_integrity(program, observed)
assert result.passed, result.failures  # the first mismatch of each failing test

program_json = program.to_json()  # a warptap-tck-program file, for a tool without warptap
```

::: warptap.tap_integrity
    options:
      members: [build_integrity_program, check_integrity, select_instruction, TapConfig, IntegrityProgram, IntegrityTest, TckCycle, IntegrityResult, IntegrityFailure, TapIntegrityError, TRST_LEAD_IN, TMS_RESET_LEAD_IN]
