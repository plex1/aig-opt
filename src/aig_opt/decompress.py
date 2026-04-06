"""Decompression: intentionally restructure a circuit to escape local minima.

These passes temporarily increase gate count by creating structurally
different implementations. Subsequent compression passes (rewrite, resub)
then work from this new starting point and may find a lower minimum.
"""

from __future__ import annotations

import random

from .aig import AIG, CONST_FALSE, CONST_TRUE, make_lit, negate


def resynthesize_from_truth_tables(aig: AIG, rng: random.Random | None = None) -> AIG:
    """Blow up the circuit by resynthesizing all outputs from their truth tables.

    Computes the truth table for each output, then builds a completely new
    AIG from scratch using Shannon decomposition with a random variable
    ordering. The result is functionally identical but structurally
    different (and usually larger).

    Only works for circuits with ≤ 20 inputs (truth table must fit in memory).
    """
    if rng is None:
        rng = random.Random(42)

    n = len(aig.inputs)
    if n > 20 or n == 0:
        return aig  # too large for truth table approach

    from .rewriter import SynthesisContext, synthesize_tt
    from .npn import permute_tt

    # Compute truth tables for all outputs
    mask = (1 << (1 << n)) - 1
    output_tts: list[int] = []
    gates = aig.topological_sort_gates()

    for out_lit in aig.outputs:
        tt = 0
        for pattern in range(1 << n):
            val: dict[int, int] = {0: 0}
            for j, inp in enumerate(aig.inputs):
                val[inp] = (pattern >> j) & 1
            for v in gates:
                if v not in aig.and_gates:
                    continue
                r0, r1 = aig.and_gates[v]
                a = val.get(r0 >> 1, 0) ^ (r0 & 1)
                b = val.get(r1 >> 1, 0) ^ (r1 & 1)
                val[v] = a & b
            out_val = val.get(out_lit >> 1, 0) ^ (out_lit & 1)
            if out_val:
                tt |= (1 << pattern)
        output_tts.append(tt & mask)

    # Resynthesize with a random variable permutation in a shared context
    perm = list(range(n))
    rng.shuffle(perm)

    new_inputs = list(aig.inputs)
    leaf_lits = [make_lit(new_inputs[perm[i]]) for i in range(n)]
    ctx = SynthesisContext({}, max(aig.inputs) + 1)

    new_outputs = []
    for tt in output_tts:
        perm_tt = permute_tt(tt, tuple(perm), n)
        result_lit = synthesize_tt(perm_tt, n, leaf_lits, ctx)
        new_outputs.append(result_lit)

    return AIG(
        max_var=ctx.next_var - 1,
        inputs=new_inputs,
        outputs=new_outputs,
        and_gates=dict(ctx.new_gates),
        latches=list(aig.latches),
    )


def perturb_subgraphs(aig: AIG, fraction: float = 0.3, rng: random.Random | None = None) -> AIG:
    """Randomly resynthesize a fraction of nodes with non-optimal decompositions.

    For each selected node, recompute its truth table over a random cut,
    then resynthesize with a random (not best) variable permutation.
    Recomputes cuts after each perturbation to avoid stale references.
    """
    if rng is None:
        rng = random.Random(42)

    from .rewriter import (
        enumerate_cuts, compute_cut_truth_table, SynthesisContext,
        synthesize_tt, verify_synthesis, _validate_cut,
    )
    from .npn import permute_tt

    n_gates = aig.num_ands()
    n_to_perturb = max(1, int(n_gates * fraction))

    for _ in range(n_to_perturb):
        all_cuts = enumerate_cuts(aig, 5)

        gates = sorted(aig.and_gates.keys())
        rng.shuffle(gates)

        done = False
        for var in gates:
            if var not in aig.and_gates:
                continue
            node_cuts = all_cuts.get(var, [])
            valid_cuts = [c for c in node_cuts if 2 <= len(c) <= 5
                          and _validate_cut(aig, var, c)]
            if not valid_cuts:
                continue

            cut = rng.choice(valid_cuts)
            tt, leaves = compute_cut_truth_table(aig, var, cut)
            n = len(leaves)
            if n == 0:
                continue

            perm = list(range(n))
            rng.shuffle(perm)
            perm_tt = permute_tt(tt, tuple(perm), n)
            leaf_lits = [make_lit(leaves[perm[i]]) for i in range(n)]

            hash_table: dict[tuple[int, int], int] = {}
            for gv, (r0, r1) in aig.and_gates.items():
                hash_table[(min(r0, r1), max(r0, r1))] = gv

            ctx = SynthesisContext(hash_table, aig.max_var + 1)
            new_lit = synthesize_tt(perm_tt, n, leaf_lits, ctx)

            if verify_synthesis(aig, ctx.new_gates, new_lit, tt, leaves):
                aig.and_gates.update(ctx.new_gates)
                aig.max_var = max(aig.max_var, ctx.next_var - 1)

                old_lit = make_lit(var)
                if new_lit != old_lit:
                    subs = {old_lit: new_lit, negate(old_lit): negate(new_lit)}
                    aig.remap_literals(subs)
                done = True
                break

        if not done:
            break  # no more perturbable gates

    return aig
