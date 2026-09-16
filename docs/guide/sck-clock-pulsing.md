# System-clock pulsing (-sck)

`iRunLoop(count)` advances time between commands. By default it cycles TCK in Run-Test/Idle —
real PDL's `-tck` selector. Some designs need something different: a functional clock that's a
genuinely separate physical clock from TCK, needing to be pulsed a precise number of times
independent of however many TCK cycles the surrounding JTAG shifting happens to take. Passing
`sck_port` switches to real PDL's `-sck` (system clock) selector instead — pulsing that named
port directly, in its own timing domain, while TCK/TMS/TDI stay static.

```python
pdl.iTarget("axis_write")
pdl.iWrite(tdata | (1 << 8))   # tdata byte + a valid strobe bit
pdl.iApply()

pdl.iRunLoop(1, sck_port="clk")   # one pulse of the design's own "clk" port, not TCK
```

This is the mechanism that made it possible to validate warptap against
[`alexforencich/verilog-uart`](https://github.com/alexforencich/verilog-uart)'s `uart_tx`: a
full byte transmission takes a precise 81 cycles of the UART's own `clk` at `prescale=1`, with
no useful relationship to how many TCK cycles the JTAG side happens to spend shifting values in
and out — `iRunLoop(81, sck_port="clk")` (or several smaller calls adding up to that) pulses
exactly that many real clock edges, independent of everything else.

ICL has no confirmed mechanism for declaring a port as "the system clock" the way TCK is a real,
named port kind — `sck_port` is a Python-side-only parameter, never bound to an ICL
declaration; the caller supplies the real clock's own name directly, the same pattern
`retarget_faultflow_patterns`'s own `clock_port` parameter uses. `to_pdl()` does render it,
though: an `iRunLoop` with `sck_port` set emits real PDL's `-sck <port>` operand.
