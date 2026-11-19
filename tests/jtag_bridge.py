"""Drive a design's TAP and pins one TCK cycle at a time, from Python.

:class:`JtagBridge` compiles the design with a generated Icarus Verilog testbench that reads one
command per line on stdin and answers on stdout, so the simulation stays live between calls: a
test, or a third-party JTAG tool through a small driver adapter, decides each TMS/TDI bit after
seeing the TDO bits before it, and sets and reads the design's other pins between cycles, the
way a board would.

Each :meth:`JtagBridge.cycle` is one TCK period: TMS and TDI change while TCK is low, TDO is
sampled just before the rising edge (warptap's TAP launches it on the falling edge), then TCK
rises and falls. Pins are set and read between cycles, with TCK low.

Pin values are integers whose bit ``i`` is the port's ``i``-th bit in Yosys order, the least
significant first (``d[0]`` of ``input [3:0] d``, ``c[1]`` of ``input [0:1] c``): Verilog
connects the testbench's ``[w-1:0]`` nets to the port by position, MSB to MSB.

:class:`Jtag` holds the IEEE 1149.1 state sequences for IR and DR scans, written here from the
standard's state diagram rather than taken from warptap's own pattern code, so tests built on it
don't check warptap against itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

_TAP_INPUTS = ("tck", "tms", "tdi", "trst_n")
_STDIN, _STDOUT = "32'h8000_0000", "32'h8000_0001"


def _testbench(top: str, ports: Sequence[Tuple[str, str, int]]) -> str:
    """The bridge testbench for ``top``, whose ports are ``(name, direction, width)``."""
    others = [p for p in ports if p[0] not in (*_TAP_INPUTS, "tdo")]
    decls, conns, drive_cases, pin_names = [], [], [], []
    for index, (name, direction, width) in enumerate(others):
        rng = f"[{width - 1}:0] " if width > 1 else ""
        if direction == "input":
            decls.append(f"  reg {rng}drv_{name} = 0;")
            conns.append(f"    .{name}(drv_{name})")
            drive_cases.append(f"        {index}: drv_{name} = a;")
        elif direction == "output":
            decls.append(f"  wire {rng}{name};")
            conns.append(f"    .{name}({name})")
            pin_names.append(name)
        else:  # inout: the board drives the bits whose enable is set, else lets the chip
            decls.append(f"  wire {rng}{name};")
            decls.append(f"  reg {rng}drv_{name} = 0;")
            decls.append(f"  reg {rng}en_{name} = 0;")
            for bit in range(width):
                sel = f"[{bit}]" if width > 1 else ""
                decls.append(
                    f"  assign {name}{sel} = en_{name}{sel} ? drv_{name}{sel} : 1'bz;"
                )
            conns.append(f"    .{name}({name})")
            drive_cases.append(f"        {index}: begin drv_{name} = a; en_{name} = b; end")
            pin_names.append(name)
    pin_format = " ".join(["%b"] * len(pin_names)) or "-"
    pin_args = (", " + ", ".join(pin_names)) if pin_names else ""
    return "\n".join(
        [
            "`timescale 1ns/1ps",
            "module warptap_jtag_bridge;",
            "  reg tck = 0, tms = 1, tdi = 0, trst_n = 0;",
            "  wire tdo;",
            *decls,
            f"  {top} dut (",
            ",\n".join(
                ["    .tck(tck)", "    .tms(tms)", "    .tdi(tdi)", "    .trst_n(trst_n)", "    .tdo(tdo)"]
                + conns
            ),
            "  );",
            "  reg [8*8:1] cmd;",
            "  integer n, idx, a, b;",
            "  initial begin",
            "    forever begin",
            f"      n = $fscanf({_STDIN}, \"%s\", cmd);",
            "      if (n != 1 || cmd == \"Q\") $finish;",
            "      else if (cmd == \"C\") begin",
            f"        n = $fscanf({_STDIN}, \"%d %d\", a, b);",
            "        tms = a; tdi = b; #5;",
            f"        $display(\"%b\", tdo); $fflush({_STDOUT});",
            "        tck = 1; #5; tck = 0; #5;",
            "      end else if (cmd == \"T\") begin",
            f"        n = $fscanf({_STDIN}, \"%d\", a);",
            f"        trst_n = a; #5; $display(\"ok\"); $fflush({_STDOUT});",
            "      end else if (cmd == \"D\") begin",
            f"        n = $fscanf({_STDIN}, \"%d %d %d\", idx, a, b);",
            "        case (idx)",
            *drive_cases,
            "        default: ;",
            "        endcase",
            f"        #1; $display(\"ok\"); $fflush({_STDOUT});",
            "      end else if (cmd == \"P\") begin",
            f"        $display(\"{pin_format}\"{pin_args}); $fflush({_STDOUT});",
            "      end",
            "    end",
            "  end",
            "endmodule",
            "",
        ]
    )


class JtagBridge:
    """A live simulation of ``top`` (in ``verilog_files``), whose ports are ``ports``:
    ``(name, direction, width)`` for every top-level port, the TAP pins included."""

    def __init__(
        self,
        verilog_files: Sequence[Path],
        top: str,
        ports: Sequence[Tuple[str, str, int]],
        *,
        workdir: Path,
        iverilog_command: str = "iverilog",
        vvp_command: str = "vvp",
    ):
        self._ports = list(ports)
        others = [p for p in self._ports if p[0] not in (*_TAP_INPUTS, "tdo")]
        self._index = {name: i for i, (name, _, _) in enumerate(others)}
        self._direction = {name: direction for name, direction, _ in others}
        self._width = {name: width for name, _, width in others}
        self._pins = [name for name, direction, _ in others if direction != "input"]
        tb = Path(workdir) / "warptap_jtag_bridge.v"
        tb.write_text(_testbench(top, self._ports), encoding="utf-8")
        sim = Path(workdir) / "warptap_jtag_bridge.vvp"
        subprocess.run(
            [iverilog_command, "-g2012", "-o", str(sim), str(tb), *map(str, verilog_files)],
            check=True, capture_output=True, text=True,
        )
        self._proc = subprocess.Popen(
            [vvp_command, "-n", str(sim)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )

    def _ask(self, line: str) -> str:
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()
        reply = self._proc.stdout.readline()
        if not reply:
            raise RuntimeError(f"the simulation ended while answering {line!r}")
        return reply.strip()

    def cycle(self, tms: int, tdi: int = 0) -> str:
        """One TCK period; returns TDO as sampled before the rising edge: "0", "1", "z" or "x"."""
        return self._ask(f"C {tms} {tdi}")

    def trst(self, value: int) -> None:
        self._ask(f"T {value}")

    def drive(self, port: str, value: int, enable: int | None = None) -> None:
        """Drive input ``port`` to ``value``; for an inout, drive the bits set in ``enable``
        (default: all) and release the others."""
        if enable is None:
            enable = (1 << self._width[port]) - 1
        self._ask(f"D {self._index[port]} {value} {enable if self._direction[port] == 'inout' else 0}")

    def release(self, port: str) -> None:
        self.drive(port, 0, enable=0)

    def pins(self) -> Dict[str, str]:
        """Every output and inout pin as the board sees it: one character per bit, the most
        significant (the port's last bit in Yosys order) first."""
        values = self._ask("P").split()
        return dict(zip(self._pins, values)) if self._pins else {}

    def pin_bit(self, port: str, position: int) -> str:
        """Bit ``position`` (Yosys order) of output or inout ``port``: "0", "1", "z" or "x"."""
        text = self.pins()[port]
        return text[len(text) - 1 - position]

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.stdin.write("Q\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=60)

    def __enter__(self) -> "JtagBridge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Jtag:
    """IR and DR scans through a :class:`JtagBridge`, each from and back to Run-Test/Idle."""

    def __init__(self, bridge: JtagBridge):
        self.bridge = bridge

    def reset(self) -> None:
        """Pulse TRST, then five TMS=1 cycles to Test-Logic-Reset, then Run-Test/Idle."""
        self.bridge.trst(0)
        self.bridge.cycle(1)
        self.bridge.trst(1)
        for _ in range(5):
            self.bridge.cycle(1)
        self.bridge.cycle(0)

    def _shift(self, bits: Sequence[int]) -> List[str]:
        """In Shift-IR or Shift-DR: shift ``bits`` in, the first nearest TDO when done, and
        leave through Exit1, Update and back to Run-Test/Idle. Returns TDO, first bit out
        first."""
        out = []
        for k, bit in enumerate(bits):
            out.append(self.bridge.cycle(1 if k == len(bits) - 1 else 0, bit))
        self.bridge.cycle(1)  # Exit1 -> Update
        self.bridge.cycle(0)  # Update -> Run-Test/Idle
        return out

    def ir(self, opcode: int, width: int) -> List[str]:
        for tms in (1, 1, 0, 0):  # Select-DR, Select-IR, Capture-IR, Shift-IR
            self.bridge.cycle(tms)
        return self._shift([(opcode >> k) & 1 for k in range(width)])

    def dr(self, bits: Sequence[int]) -> List[str]:
        """Shift ``bits`` through the selected data register; ``bits[0]`` ends up in the
        stage nearest TDO. Returns what was captured, nearest TDO first."""
        for tms in (1, 0, 0):  # Select-DR, Capture-DR, Shift-DR
            self.bridge.cycle(tms)
        return self._shift(list(bits))
