"""Simulation-based functional reduction (FRAIGing / SAT sweeping).

Detects functionally equivalent or complementary nodes by simulating all
nodes with random bit-vectors, then merging equivalent pairs. Also detects
constant nodes that structural propagation misses.

This is a simplified version of the FRAIG algorithm (Coudert 1997,
Mishchenko 2006). Full FRAIG uses SAT to confirm candidates; we use
exhaustive verification for circuits with ≤20 inputs and random simulation
with many vectors for larger circuits.
"""

from __future__ import annotations

import random

from .aig import (
    AIG,
    CONST_FALSE,
    CONST_TRUE,
    make_lit,
    negate,
    resolve,
)


def _simulate_word(aig: AIG, input_words: dict[int, int]) -> dict[int, int]:
    """Simulate all gates with 64-bit words in parallel.

    input_words: maps input variable -> 64-bit int (each bit = one simulation vector)
    Returns: maps variable -> 64-bit simulation signature
    """
    sig: dict[int, int] = {0: 0}  # var 0 (constant false) = all zeros

    for v in aig.inputs:
        sig[v] = input_words[v]

    for v in aig.topological_sort_gates():
        l0, l1 = aig.and_gates[v]
        v0, v1 = l0 >> 1, l1 >> 1
        s0 = sig.get(v0, 0)
        s1 = sig.get(v1, 0)
        if l0 & 1:
            s0 = ~s0 & 0xFFFFFFFFFFFFFFFF
        if l1 & 1:
            s1 = ~s1 & 0xFFFFFFFFFFFFFFFF
        sig[v] = s0 & s1

    return sig



def _verify_equivalence_batched(
    aig: AIG,
    gates: list[int],
    pairs: list[tuple[int, int, bool]],
) -> list[bool]:
    """Verify multiple equivalence candidates in batch using parallel simulation.

    For ≤20 inputs: exhaustive (packs 64 patterns per word).
    For >20 inputs: statistical with 4096 random vectors.

    Returns list of bools (one per pair) indicating if equivalence holds.
    """
    MASK64 = 0xFFFFFFFFFFFFFFFF
    n = len(aig.inputs)

    if not pairs:
        return []

    results = [True] * len(pairs)
    alive = set(range(len(pairs)))  # indices of pairs not yet disproven

    if n <= 20:
        total = 1 << n
        # Process 64 patterns at a time
        for block_start in range(0, total, 64):
            block_end = min(block_start + 64, total)
            block_size = block_end - block_start

            # Build input words: bit j of input_words[v] = bit (v_idx) of pattern (block_start + j)
            input_words: dict[int, int] = {}
            for idx, v in enumerate(aig.inputs):
                word = 0
                for j in range(block_size):
                    if ((block_start + j) >> idx) & 1:
                        word |= 1 << j
                input_words[v] = word

            # Simulate
            sig: dict[int, int] = {0: 0}
            for v in aig.inputs:
                sig[v] = input_words[v]
            for v in gates:
                l0, l1 = aig.and_gates[v]
                s0 = sig.get(l0 >> 1, 0)
                s1 = sig.get(l1 >> 1, 0)
                if l0 & 1:
                    s0 = ~s0 & MASK64
                if l1 & 1:
                    s1 = ~s1 & MASK64
                sig[v] = s0 & s1

            # Check pairs
            mask = (1 << block_size) - 1
            for pi in list(alive):
                va, vb, comp = pairs[pi]
                sa = sig.get(va, 0) & mask
                sb = sig.get(vb, 0) & mask
                if comp:
                    sb = ~sb & mask
                if sa != sb:
                    results[pi] = False
                    alive.discard(pi)

            if not alive:
                break
    else:
        # Statistical: 64 rounds × 64-bit words = 4096 patterns
        rng = random.Random(12345)
        for _ in range(64):
            input_words = {v: rng.getrandbits(64) for v in aig.inputs}
            sig: dict[int, int] = {0: 0}
            for v in aig.inputs:
                sig[v] = input_words[v]
            for v in gates:
                l0, l1 = aig.and_gates[v]
                s0 = sig.get(l0 >> 1, 0)
                s1 = sig.get(l1 >> 1, 0)
                if l0 & 1:
                    s0 = ~s0 & MASK64
                if l1 & 1:
                    s1 = ~s1 & MASK64
                sig[v] = s0 & s1

            for pi in list(alive):
                va, vb, comp = pairs[pi]
                sa = sig.get(va, 0)
                sb = sig.get(vb, 0)
                if comp:
                    sb = ~sb & MASK64
                if sa != sb:
                    results[pi] = False
                    alive.discard(pi)

            if not alive:
                break

    return results


def functional_reduction(aig: AIG) -> AIG:
    """Detect and merge functionally equivalent/complementary nodes.

    Steps:
    1. Simulate all nodes with random bit-vectors (2048 random patterns)
    2. Group nodes by simulation signature (detecting equiv and complement)
    3. Verify candidates (exhaustive for ≤20 inputs, statistical otherwise)
    4. Merge by substituting equivalent nodes
    5. Clean up with constant propagation and dead node elimination
    """
    MASK64 = 0xFFFFFFFFFFFFFFFF

    # Step 1: Multi-round simulation for hash signatures
    rng = random.Random(42)
    gates = aig.topological_sort_gates()
    all_vars = list(aig.inputs) + gates

    # Use raw 64-bit signatures from a single round for grouping,
    # but verify with many rounds
    input_words = {v: rng.getrandbits(64) for v in aig.inputs}
    sig = _simulate_word(aig, input_words)

    # Step 2: Group by canonical signature (min of sig, ~sig)
    groups: dict[int, list[tuple[int, bool]]] = {}
    # var 0 = constant false
    groups[0] = [(0, False)]

    for v in all_vars:
        s = sig.get(v, 0)
        s_neg = ~s & MASK64
        if s <= s_neg:
            key = s
            neg = False
        else:
            key = s_neg
            neg = True
        groups.setdefault(key, []).append((v, neg))

    # Step 3: Build candidate pairs and verify in batch
    input_set = set(aig.inputs)
    candidate_pairs: list[tuple[int, int, bool]] = []  # (rep_var, var, complemented)
    pair_info: list[tuple[int, int, bool, int, bool]] = []  # extra info for merge

    for key, members in groups.items():
        if len(members) < 2:
            continue

        # Pick representative: prefer inputs, then lowest var for stability
        rep_var, rep_neg = members[0]
        for var, neg in members[1:]:
            if var in input_set:
                rep_var, rep_neg = var, neg

        for var, neg in members:
            if var == rep_var:
                continue
            if var in input_set:
                continue
            complemented = (neg != rep_neg)
            candidate_pairs.append((rep_var, var, complemented))
            pair_info.append((rep_var, var, complemented, 0, False))

    if not candidate_pairs:
        return aig

    verified = _verify_equivalence_batched(aig, gates, candidate_pairs)

    # Step 4: Build substitution map from verified equivalences
    subs: dict[int, int] = {}
    total_merged = 0

    for i, is_equiv in enumerate(verified):
        if not is_equiv:
            continue
        rep_var, var, complemented = candidate_pairs[i]
        rep_lit = make_lit(rep_var)
        if complemented:
            rep_lit = negate(rep_lit)
        subs[make_lit(var)] = rep_lit
        subs[negate(make_lit(var))] = negate(rep_lit)
        total_merged += 1

    if total_merged == 0:
        return aig

    # Step 5: Apply substitutions
    aig.remap_literals(subs)

    return aig
