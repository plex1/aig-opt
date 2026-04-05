"""CLI entry point for the AIG optimizer."""

from __future__ import annotations

import argparse
import sys

from .aiger import parse_aag, write_aag
from .optimizer import optimize


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="aig-opt",
        description="Optimize AIGER (.aag) files",
    )
    parser.add_argument("input", help="Input .aag file")
    parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Number of full pipeline rounds (default: 1, stops early if no improvement)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print before/after gate counts to stderr",
    )
    args = parser.parse_args(argv)

    aig = parse_aag(args.input)
    before = aig.num_ands()

    aig = optimize(aig, rounds=args.rounds)
    aig.compact()

    after = aig.num_ands()

    if args.stats:
        reduction = ((before - after) / before * 100) if before > 0 else 0
        print(f"Before: {before} AND gates", file=sys.stderr)
        print(f"After:  {after} AND gates", file=sys.stderr)
        print(f"Reduction: {before - after} gates ({reduction:.1f}%)", file=sys.stderr)

    text = write_aag(aig)
    if args.output:
        write_aag(aig, args.output)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
