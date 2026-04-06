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
        "--balance",
        action="store_true",
        help="Enable balance-rewrite cycles (slower, reduces depth and breaks convergence)",
    )
    parser.add_argument(
        "--stochastic",
        type=int,
        default=0,
        metavar="N",
        help="Multi-restart stochastic optimization with N restarts (0 = off)",
    )
    parser.add_argument(
        "--multioutput",
        action="store_true",
        help="Enable multi-output resynthesis (slower, finds cross-output gate sharing)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print before/after gate counts to stderr",
    )
    args = parser.parse_args(argv)

    aig = parse_aag(args.input)
    before = aig.num_ands()

    aig = optimize(aig, balance=args.balance, multioutput=args.multioutput,
                   stochastic=args.stochastic)
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
