"""Decompression: intentionally restructure a circuit to escape local minima.

These passes temporarily increase gate count by creating structurally
different implementations. Subsequent compression passes (rewrite, resub)
then work from this new starting point and may find a lower minimum.
"""

from __future__ import annotations

import random

from .aig import AIG, CONST_FALSE, CONST_TRUE, lit_to_var, is_negated, make_lit, negate


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


def algebraic_rewrite(aig: AIG, fraction: float = 0.3, rng: random.Random | None = None) -> AIG:
    """Restructure gates using algebraic identities.

    Applies random algebraic transformations that preserve function but
    change structure. Each increases gate count by 1-2 but creates a
    fundamentally different circuit topology.

    Transformations:
    1. Distributive expansion: a AND (b OR c) -> (a AND b) OR (a AND c)
       In AIG: AND(a, NOT(AND(NOT b, NOT c))) -> NOT(AND(NOT(AND(a,b)), NOT(AND(a,c))))
       Cost: 3 gates -> 4 gates (+1), but different structure.

    2. De Morgan decomposition: AND(a, b) -> NOT(OR(NOT a, NOT b))
       -> NOT(NOT(AND(NOT a, NOT b)))
       This is a no-op in AIG (same structure), but combined with
       other rewrites it changes the optimization landscape.

    3. XOR introduction: if a gate computes a AND NOT(b), replace with
       a AND (a XOR (a AND b)). More gates but exposes XOR structure
       that may be optimized differently.

    4. Associative reshuffling: for AND chains a AND b AND c,
       change (a AND b) AND c to a AND (b AND c).
    """
    if rng is None:
        rng = random.Random(42)

    gates = aig.topological_sort_gates()
    n_to_transform = max(1, int(len(gates) * fraction))

    # Build gate hash for reuse
    gate_hash: dict[tuple[int, int], int] = {}
    for var, (r0, r1) in aig.and_gates.items():
        gate_hash[(min(r0, r1), max(r0, r1))] = var

    def make_and(a: int, b: int) -> int:
        if a == CONST_FALSE or b == CONST_FALSE:
            return CONST_FALSE
        if a == CONST_TRUE:
            return b
        if b == CONST_TRUE:
            return a
        if a == b:
            return a
        if a == negate(b):
            return CONST_FALSE
        k = (min(a, b), max(a, b))
        if k in gate_hash:
            return make_lit(gate_hash[k])
        var = aig.max_var + 1
        aig.max_var = var
        aig.and_gates[var] = k
        gate_hash[k] = var
        return make_lit(var)

    def make_or(a: int, b: int) -> int:
        return negate(make_and(negate(a), negate(b)))

    candidates = list(gates)
    rng.shuffle(candidates)
    transformed = 0

    for var in candidates:
        if transformed >= n_to_transform:
            break
        if var not in aig.and_gates:
            continue

        r0, r1 = aig.and_gates[var]
        v0, v1 = lit_to_var(r0), lit_to_var(r1)

        transform = rng.randint(0, 2)

        if transform == 0:
            # Distributive: AND(a, OR(b, c)) -> OR(AND(a,b), AND(a,c))
            # Detect: var = AND(r0, r1) where r1 = NOT(AND(x, y))
            # meaning r1 = OR(NOT x, NOT y), so var = AND(r0, OR(NOT x, NOT y))
            target_lit, other_lit = (r1, r0) if not is_negated(r1) else (r0, r1)
            # We need the negated input to be a gate (represents OR)
            if is_negated(target_lit):
                inner_var = lit_to_var(target_lit)
                if inner_var in aig.and_gates:
                    ix, iy = aig.and_gates[inner_var]
                    # var = AND(other, NOT(AND(ix, iy)))
                    # = AND(other, OR(NOT ix, NOT iy))
                    # -> OR(AND(other, NOT ix), AND(other, NOT iy))
                    t1 = make_and(other_lit, negate(ix))
                    t2 = make_and(other_lit, negate(iy))
                    new_lit = make_or(t1, t2)
                    if new_lit != make_lit(var):
                        subs = {make_lit(var): new_lit, negate(make_lit(var)): negate(new_lit)}
                        aig.remap_literals(subs)
                        transformed += 1
                        continue

        if transform == 1:
            # Associative reshuffle: if both inputs are AND gates,
            # pick one input from each and regroup
            if (not is_negated(r0) and v0 in aig.and_gates
                    and not is_negated(r1) and v1 in aig.and_gates):
                a0, a1 = aig.and_gates[v0]
                b0, b1 = aig.and_gates[v1]
                # Current: AND(AND(a0,a1), AND(b0,b1))
                # Reshuffle to: AND(AND(a0,b0), AND(a1,b1))
                t1 = make_and(a0, b0)
                t2 = make_and(a1, b1)
                new_lit = make_and(t1, t2)
                if new_lit != make_lit(var):
                    subs = {make_lit(var): new_lit, negate(make_lit(var)): negate(new_lit)}
                    aig.remap_literals(subs)
                    transformed += 1
                    continue

        if transform == 2:
            # Complement-based: AND(a, b) -> AND(NOT(NOT a), b)
            # = AND(NOT(NOT a OR NOT a), b) ... not useful directly
            # Instead: AND(a, b) -> AND(a, NOT(NOT b))
            # Expand NOT b: if b is a gate AND(x,y), NOT b = OR(NOT x, NOT y)
            # So AND(a, NOT(OR(NOT x, NOT y))) = AND(a, AND(x, y)) = AND(a, b)
            # That's a no-op. Let's try something else:
            # AND(a, b) -> AND(a AND a, b) = AND(AND(a, a), b) = AND(a, b)
            # Also no-op. Skip — the other transforms are more useful.
            pass

    return aig
