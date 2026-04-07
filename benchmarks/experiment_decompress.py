#!/usr/bin/env python3
"""Experiment: find optimal decompression/compression ratio.

Tests different decompression methods (algebraic, perturb), intensities
(fraction of gates affected), and number of compression steps per cycle.
Runs 3 decompress-compress cycles on each configuration and reports
the best gate count achieved.

Usage:
    python benchmarks/experiment_decompress.py [circuit.aag]

Default circuit: benchmarks/circuits/mul4_unsigned.aag
"""

from __future__ import annotations

import random as _random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aig_opt.aiger import parse_aag
from aig_opt.optimizer import (
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
    functional_reduction_pass,
    simple_rewrite,
)
from aig_opt.rewriter import dag_rewrite
from aig_opt.resub import resubstitution
from aig_opt.decompress import algebraic_rewrite, perturb_subgraphs


def do_cleanup(w):
    for p in [constant_propagation, structural_hashing, dead_node_elimination]:
        w = p(w)
    return w


def compress(w, seed, k=5, pert=0.3):
    """One compress step: perturbed rewrite + resub."""
    rng = _random.Random(seed)
    w = dag_rewrite(w, iterations=10, max_cut_size=k, perturbation=pert, rng=rng)
    w = do_cleanup(w)
    w = resubstitution(w, max_resub=1, allow_new_gates=False, rng=_random.Random(seed + 1))
    w = do_cleanup(w)
    return w


def decompress(w, method, frac, seed):
    """One decompress step."""
    rng = _random.Random(seed)
    if method == "algebraic":
        w = algebraic_rewrite(w, fraction=frac, rng=rng)
    elif method == "perturb":
        w = perturb_subgraphs(w, fraction=frac, rng=rng)
    w = do_cleanup(w)
    return w


def main():
    circuit_path = sys.argv[1] if len(sys.argv) > 1 else "benchmarks/circuits/mul4_unsigned.aag"
    aig = parse_aag(circuit_path)
    circuit_name = Path(circuit_path).name

    # Prepare: run basic passes
    prep = aig.copy()
    for p in [
        constant_propagation, structural_hashing, dead_node_elimination,
        functional_reduction_pass,
        constant_propagation, structural_hashing, dead_node_elimination,
        simple_rewrite,
        constant_propagation, structural_hashing, dead_node_elimination,
    ]:
        prep = p(prep)

    ref_tt = aig.truth_table() if len(aig.inputs) <= 16 else None

    print(f"Circuit: {circuit_name}")
    print(f"Original: {aig.num_ands()} gates, Prepared: {prep.num_ands()} gates")
    print()
    print(f"{'Config':<50} {'Best':>5} {'Verified':>8} {'Trace'}")
    print("-" * 110)

    results = []
    n_cycles = 3

    for method in ["algebraic", "perturb"]:
        for frac in [0.1, 0.2, 0.3, 0.5]:
            for n_compress in [2, 3, 5]:
                t0 = time.time()
                best = prep.num_ands()
                best_aig = prep.copy()
                trace = [best]
                work = prep.copy()

                for cycle in range(n_cycles):
                    seed_base = cycle * 100 + int(frac * 10) + n_compress
                    work = decompress(work, method, frac, seed_base)
                    trace.append(work.num_ands())

                    for ci in range(n_compress):
                        k = [5, 4, 5, 3, 5][ci % 5]
                        work = compress(work, seed_base + ci + 10, k=k, pert=0.3)
                        if work.num_ands() < best:
                            best = work.num_ands()
                            best_aig = work.copy()
                        trace.append(work.num_ands())

                elapsed = time.time() - t0

                # Verify
                verified = "?"
                if ref_tt is not None:
                    verified = "PASS" if best_aig.truth_table() == ref_tt else "FAIL"

                config = f"{method}({frac}) x{n_cycles}, {n_compress}comp/cycle [{elapsed:.0f}s]"
                trace_str = "->".join(str(t) for t in trace[:15])
                if len(trace) > 15:
                    trace_str += "..."
                print(f"{config:<50} {best:>5} {verified:>8} {trace_str}")
                results.append((config, best, verified))

    print()
    print("--- Sorted by best gate count ---")
    for config, best, verified in sorted(results, key=lambda x: x[1]):
        print(f"  {best:>3} [{verified}]  {config}")


if __name__ == "__main__":
    main()
