"""warptap command-line entry point.

Stage 1 only wires up plumbing (see implementation_plan.md §7) — no JTAG/IJTAG
subcommands exist yet.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="warptap")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)
    if args.version:
        from warptap import __version__

        print(__version__)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
