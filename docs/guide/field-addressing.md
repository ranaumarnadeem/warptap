# Named sub-field addressing

Real ICL lets an instrument declare named `Alias` sub-ranges within its own register — a
control/status word split into meaningfully-named fields rather than one opaque integer.
Declare them on the `InstrumentSpec`, then address them by name from `iWrite`/`iRead` instead
of writing/reading the whole register.

```python
from warptap import Alias, InstrumentSpec, PDLInterpreter, build_sib_plan

specs = [
    InstrumentSpec(
        "status_reg", width=4, capture_value=0,
        aliases=(Alias("lo", 0, 1), Alias("hi", 2, 3)),
    )
]
graph, root = build_sib_plan(specs)

pdl = PDLInterpreter(graph, root)
pdl.iTarget("status_reg")
pdl.iWrite(0b01, field="lo")   # bits[1:0]
pdl.iWrite(0b11, field="hi")   # bits[3:2] -- doesn't clobber lo's own bits
pdl.iApply()
```

`field=None` (the default on both `iWrite` and `iRead`) addresses the whole instrument, exactly
as without any `Alias` declared at all. An unknown field name raises `PDLError` naming the bad
field, rather than silently addressing the wrong bits.

## Fields and separate applies

Writing one field in a fresh `iApply` cycle — with no other field of the same instrument also
queued that same batch — preserves the other fields' own last-committed values instead of
zeroing them: `PDLInterpreter` tracks each `WRITE`-direction instrument's own last-known
committed value across calls, so a sub-field write only ever touches the bits it names.
`iRead(expected, field=...)` works the same way for the read side, masking in only the named
sub-range so the rest of the instrument's content is don't-care for that check.
