"""Simulation-guided resubstitution.

For each gate, checks whether its function can be expressed as a simple
combination (AND, OR) of other existing nodes in the circuit.
If so, the gate is replaced and its now-dead subgraph is removed.
"""

from __future__ import annotations

import random

from .aig import (
    AIG,
    CONST_FALSE,
    CONST_TRUE,
    lit_to_var,
    is_negated,
    make_lit,
    negate,
)

MASK64 = 0xFFFFFFFFFFFFFFFF


def _simulate_all(aig: AIG, num_rounds: int = 4, seed: int = 42) -> dict[int, list[int]]:
    """Simulate all nodes, returning var -> list of 64-bit signatures."""
    rng = random.Random(seed)
    gates = aig.topological_sort_gates()
    sigs: dict[int, list[int]] = {0: [0] * num_rounds}

    for v in aig.inputs:
        sigs[v] = [rng.getrandbits(64) for _ in range(num_rounds)]

    for rnd in range(num_rounds):
        for v in gates:
            r0, r1 = aig.and_gates[v]
            s0 = sigs.get(lit_to_var(r0), sigs[0])[rnd]
            s1 = sigs.get(lit_to_var(r1), sigs[0])[rnd]
            if r0 & 1:
                s0 = ~s0 & MASK64
            if r1 & 1:
                s1 = ~s1 & MASK64
            if v not in sigs:
                sigs[v] = [0] * num_rounds
            sigs[v][rnd] = s0 & s1

    return sigs


def _topo_index(aig: AIG) -> dict[int, int]:
    """Assign topological index to each node."""
    idx = {v: i for i, v in enumerate(aig.inputs)}
    for i, v in enumerate(aig.topological_sort_gates()):
        idx[v] = len(aig.inputs) + i
    return idx


def _verify_resub(aig: AIG, target: int, replacement_lit: int) -> bool:
    """Verify target == replacement_lit by exhaustive/statistical simulation."""
    n = len(aig.inputs)
    gates = aig.topological_sort_gates()
    rep_var = lit_to_var(replacement_lit)
    rep_neg = is_negated(replacement_lit)

    if n <= 20:
        total = 1 << n
        for block in range(0, total, 64):
            bs = min(64, total - block)
            sig: dict[int, int] = {0: 0}
            for idx, v in enumerate(aig.inputs):
                w = 0
                for j in range(bs):
                    if ((block + j) >> idx) & 1:
                        w |= 1 << j
                sig[v] = w
            for v in gates:
                if v not in aig.and_gates:
                    continue
                r0, r1 = aig.and_gates[v]
                s0 = sig.get(lit_to_var(r0), 0)
                s1 = sig.get(lit_to_var(r1), 0)
                if r0 & 1: s0 = ~s0 & MASK64
                if r1 & 1: s1 = ~s1 & MASK64
                sig[v] = s0 & s1

            mask = (1 << bs) - 1
            st = sig.get(target, 0) & mask
            sr = sig.get(rep_var, 0) & mask
            if rep_neg:
                sr = ~sr & mask
            if st != sr:
                return False
        return True
    else:
        rng = random.Random(54321)
        for _ in range(64):
            sig: dict[int, int] = {0: 0}
            for v in aig.inputs:
                sig[v] = rng.getrandbits(64)
            for v in gates:
                if v not in aig.and_gates:
                    continue
                r0, r1 = aig.and_gates[v]
                s0 = sig.get(lit_to_var(r0), 0)
                s1 = sig.get(lit_to_var(r1), 0)
                if r0 & 1: s0 = ~s0 & MASK64
                if r1 & 1: s1 = ~s1 & MASK64
                sig[v] = s0 & s1
            st = sig.get(target, 0)
            sr = sig.get(rep_var, 0)
            if rep_neg:
                sr = ~sr & MASK64
            if st != sr:
                return False
        return True


def resubstitution(aig: AIG, max_resub: int = 1) -> AIG:
    """Resubstitution pass.

    For each node, tries to express it as a simple function of other
    existing nodes using simulation-based candidate filtering:
      0-resub: target == existing_node (or complement)
      1-resub: target == AND(d_i, d_j) with possibly negated inputs
    """
    from .optimizer import dead_node_elimination, structural_hashing

    for _iteration in range(50):  # safety limit
        changed = False

        sigs = _simulate_all(aig, num_rounds=8)
        gates = aig.topological_sort_gates()
        topo = _topo_index(aig)
        nr = len(sigs.get(0, [0]))

        # Build hash table for existing gates
        gate_hash: dict[tuple[int, int], int] = {}
        for var, (r0, r1) in aig.and_gates.items():
            key = (min(r0, r1), max(r0, r1))
            gate_hash[key] = var

        # All available signals (inputs + gates), sorted by topo order
        all_vars = sorted(
            list(aig.inputs) + gates,
            key=lambda v: topo.get(v, 0),
        )

        # Precompute signature tuples for fast comparison
        sig_tuples: dict[int, tuple[int, ...]] = {}
        for v in all_vars:
            s = sigs.get(v)
            if s is not None:
                sig_tuples[v] = tuple(s)

        for target in reversed(gates):
            if target not in aig.and_gates:
                continue

            tsig = sig_tuples.get(target)
            if tsig is None:
                continue

            target_topo = topo.get(target, 0)

            # Collect divisors: all nodes with smaller topo index
            # (they don't depend on target, so no cycles)
            divisors = [v for v in all_vars
                        if topo.get(v, 0) < target_topo
                        and v in sig_tuples]

            # --- 0-resub: target == divisor (or complement) ---
            for d in divisors:
                dsig = sig_tuples[d]
                if dsig == tsig:
                    # Candidate: target == d
                    lit = make_lit(d)
                    if _verify_resub(aig, target, lit):
                        subs = {make_lit(target): lit, negate(make_lit(target)): negate(lit)}
                        aig.remap_literals(subs)
                        changed = True
                        break
                # Check complement
                csig = tuple(~s & MASK64 for s in dsig)
                if csig == tsig:
                    lit = negate(make_lit(d))
                    if _verify_resub(aig, target, lit):
                        subs = {make_lit(target): lit, negate(make_lit(target)): negate(lit)}
                        aig.remap_literals(subs)
                        changed = True
                        break

            if changed:
                aig = dead_node_elimination(aig)
                break

            if max_resub < 1:
                continue

            # --- 1-resub: target == AND(lit_i, lit_j) ---
            # For each pair, check all 4 negation combos via signatures
            n_div = len(divisors)
            found = False

            for i in range(n_div):
                if found:
                    break
                di = divisors[i]
                dsi = sig_tuples[di]

                for j in range(i, n_div):
                    dj = divisors[j]
                    dsj = sig_tuples[dj]

                    # Try all 4 negation combinations of di, dj
                    for ni in range(2):
                        for nj in range(2):
                            si = tuple(~s & MASK64 for s in dsi) if ni else dsi
                            sj = tuple(~s & MASK64 for s in dsj) if nj else dsj

                            # Check: target == AND(si, sj)?
                            match = True
                            for r in range(nr):
                                if tsig[r] != (si[r] & sj[r]):
                                    match = False
                                    break
                            if not match:
                                continue

                            # Also check complement: target == NAND(si, sj)?
                            # Actually just check the AND match; NAND would
                            # be caught by 0-resub + complement

                            # Candidate found — build and verify
                            li = negate(make_lit(di)) if ni else make_lit(di)
                            lj = negate(make_lit(dj)) if nj else make_lit(dj)

                            # Only accept if we can reuse an existing gate
                            # (creating a new gate for 1-resub is net-zero
                            # before DCE and can cause regressions)
                            k = (min(li, lj), max(li, lj))
                            if k not in gate_hash:
                                continue
                            if gate_hash[k] == target:
                                continue  # would replace target with itself
                            new_lit = make_lit(gate_hash[k])

                            if lit_to_var(new_lit) == target:
                                continue  # would replace with self
                            if _verify_resub(aig, target, new_lit):
                                subs = {make_lit(target): new_lit,
                                        negate(make_lit(target)): negate(new_lit)}
                                aig.remap_literals(subs)
                                changed = found = True
                                break
                        if found:
                            break
                    if found:
                        break

            if changed:
                aig = dead_node_elimination(aig)
                aig = structural_hashing(aig)
                break

    return aig
