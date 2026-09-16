# warptap

warptap inserts IEEE 1149.1 (JTAG/TAP) and IEEE 1687 (IJTAG/ICL+PDL) test-access
infrastructure into a design, pre-synthesis, via Yosys JSON netlist surgery. Give it a set of
real control/status signals to wire up; it hands back synthesizable Verilog with a JTAG TAP and
an IJTAG scan network spliced in, plus everything needed to actually drive that network and
emit a real ATE pattern file.

It's a library, not a service: `pip install warptap`, then call its functions from your own
build/generation pipeline. No daemon, no CLI subcommands yet — each call does one piece of work
and returns.

## Where to go next

- **[Quickstart](quickstart.md)** — install it and walk through one real, complete example:
  ingest RTL, insert a test-access network, drive it, emit an SVF pattern file.
- **[Guide](guide/nested-sib-networks.md)** — nested SIB networks, multi-arm ScanMux, named
  sub-field addressing, ICL/PDL import, system-clock pulsing, and faultflow pattern
  retargeting — one page per topic, each with real, working code.
- **[API Reference](reference/index.md)** — every publicly exported name, grouped by concern.
- **[Changelog](changelog.md)** — what shipped in each release.

## Source and issues

[github.com/ranaumarnadeem/warptap](https://github.com/ranaumarnadeem/warptap)
