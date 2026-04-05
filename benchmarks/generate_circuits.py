#!/usr/bin/env python3
"""Generate random AIG circuits for benchmarking."""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aig_opt.aig import AIG, make_lit, negate
from aig_opt.aiger import write_aag


def generate_random_aig(
    num_inputs: int,
    num_gates: int,
    num_outputs: int,
    redundancy: float = 0.0,
    seed: int | None = None,
) -> AIG:
    """Generate a random AIG circuit.

    Args:
        num_inputs: Number of primary inputs
        num_gates: Target number of AND gates
        num_outputs: Number of primary outputs
        redundancy: Fraction of gates that are deliberately redundant (0.0-1.0).
                    Includes duplicate gates, const inputs, trivial patterns.
        seed: Random seed for reproducibility
    """
    rng = random.Random(seed)
    aig = AIG()

    inputs = list(range(1, num_inputs + 1))
    aig.inputs = inputs[:]
    next_var = num_inputs + 1

    # Available signals (literals) to use as gate inputs
    available = []
    for v in inputs:
        available.append(make_lit(v))
        available.append(negate(make_lit(v)))

    gate_vars = []
    num_redundant = int(num_gates * redundancy)
    num_normal = num_gates - num_redundant

    # Generate normal gates
    for _ in range(num_normal):
        if len(available) < 2:
            break
        r0 = rng.choice(available)
        r1 = rng.choice(available)
        var = next_var
        next_var += 1
        aig.and_gates[var] = (r0, r1)
        gate_vars.append(var)
        available.append(make_lit(var))
        available.append(negate(make_lit(var)))

    # Generate redundant gates
    for _ in range(num_redundant):
        if len(available) < 2:
            break
        var = next_var
        next_var += 1
        kind = rng.choice(["duplicate", "const_zero", "trivial_same", "trivial_neg", "dead"])

        if kind == "duplicate" and gate_vars:
            # Duplicate an existing gate
            orig = rng.choice(gate_vars)
            r0, r1 = aig.and_gates[orig]
            aig.and_gates[var] = (r0, r1)
        elif kind == "const_zero":
            # AND with constant 0
            r0 = rng.choice(available)
            aig.and_gates[var] = (r0, 0)
        elif kind == "trivial_same":
            # x AND x
            r0 = rng.choice(available)
            aig.and_gates[var] = (r0, r0)
        elif kind == "trivial_neg":
            # x AND NOT(x)
            r0 = rng.choice(available)
            aig.and_gates[var] = (r0, negate(r0))
        else:
            # Dead gate (will be added but not connected to output)
            r0 = rng.choice(available)
            r1 = rng.choice(available)
            aig.and_gates[var] = (r0, r1)
            # Don't add to available so it stays dead
            gate_vars.append(var)
            continue

        gate_vars.append(var)
        available.append(make_lit(var))
        available.append(negate(make_lit(var)))

    # Pick outputs from available signals (prefer gate outputs)
    gate_lits = [make_lit(v) for v in gate_vars] + [negate(make_lit(v)) for v in gate_vars]
    if not gate_lits:
        gate_lits = available
    for _ in range(num_outputs):
        aig.outputs.append(rng.choice(gate_lits if gate_lits else available))

    aig.max_var = next_var - 1
    return aig


CIRCUIT_CONFIGS = [
    # (name, num_inputs, num_gates, num_outputs, redundancy, seed)
    ("rand_small_clean", 4, 10, 2, 0.0, 42),
    ("rand_small_redund", 4, 10, 2, 0.4, 43),
    ("rand_med_clean", 8, 50, 4, 0.0, 44),
    ("rand_med_redund", 8, 50, 4, 0.3, 45),
    ("rand_large_clean", 16, 200, 8, 0.0, 46),
    ("rand_large_redund", 16, 200, 8, 0.25, 47),
    ("rand_xlarge_clean", 32, 1000, 16, 0.0, 48),
    ("rand_xlarge_redund", 32, 1000, 16, 0.2, 49),
]


def main() -> None:
    circuits_dir = Path(__file__).parent / "circuits"
    circuits_dir.mkdir(exist_ok=True)

    for name, n_in, n_gates, n_out, redund, seed in CIRCUIT_CONFIGS:
        aig = generate_random_aig(n_in, n_gates, n_out, redundancy=redund, seed=seed)
        path = circuits_dir / f"{name}.aag"
        aig.comments = [
            f"Random circuit: {n_in} inputs, {n_gates} target gates, {n_out} outputs",
            f"Redundancy: {redund:.0%}, seed: {seed}",
        ]
        write_aag(aig, path)
        print(f"Generated {path.name}: {aig.num_inputs()} inputs, {aig.num_ands()} gates, {aig.num_outputs()} outputs")


if __name__ == "__main__":
    main()
