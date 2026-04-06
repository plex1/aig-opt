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


def _simulate_all(aig: AIG, num_rounds: int = 8, seed: int = 42) -> dict[int, list[int]]:
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


def _make_and_gate(aig: AIG, gate_hash: dict, lit0: int, lit1: int) -> int:
    """Create an AND gate, reusing existing if available."""
    if lit0 == CONST_FALSE or lit1 == CONST_FALSE:
        return CONST_FALSE
    if lit0 == CONST_TRUE:
        return lit1
    if lit1 == CONST_TRUE:
        return lit0
    if lit0 == lit1:
        return lit0
    if lit0 == negate(lit1):
        return CONST_FALSE

    k = (min(lit0, lit1), max(lit0, lit1))
    if k in gate_hash:
        return make_lit(gate_hash[k])

    var = aig.max_var + 1
    aig.max_var = var
    aig.and_gates[var] = k
    gate_hash[k] = var
    return make_lit(var)


def resubstitution(
    aig: AIG,
    max_resub: int = 1,
    allow_new_gates: bool = False,
    rng: random.Random | None = None,
) -> AIG:
    """Resubstitution pass.

    Args:
        max_resub: 0 = only node equivalence, 1 = also AND of two divisors.
        allow_new_gates: If True, 1-resub may create new gates (needs DCE
            afterward). If False, only reuse existing gates (safe).
        rng: Random instance for shuffled processing order. None = deterministic.
    """
    from .optimizer import dead_node_elimination, structural_hashing

    max_iters = 50 if aig.num_ands() < 200 else 10
    for _iteration in range(max_iters):
        changed = False

        sigs = _simulate_all(aig, num_rounds=8)
        gates = aig.topological_sort_gates()
        topo = _topo_index(aig)
        nr = len(sigs.get(0, [0]))

        gate_hash: dict[tuple[int, int], int] = {}
        for var, (r0, r1) in aig.and_gates.items():
            gate_hash[(min(r0, r1), max(r0, r1))] = var

        all_vars = sorted(
            list(aig.inputs) + gates,
            key=lambda v: topo.get(v, 0),
        )

        sig_tuples: dict[int, tuple[int, ...]] = {}
        for v in all_vars:
            s = sigs.get(v)
            if s is not None:
                sig_tuples[v] = tuple(s)

        # Process order: reversed topo, optionally shuffled
        targets = [v for v in reversed(gates) if v in aig.and_gates and v in sig_tuples]
        if rng is not None:
            rng.shuffle(targets)

        for target in targets:
            if target not in aig.and_gates:
                continue

            tsig = sig_tuples.get(target)
            if tsig is None:
                continue

            target_topo = topo.get(target, 0)
            divisors = [v for v in all_vars
                        if topo.get(v, 0) < target_topo
                        and v in sig_tuples]
            # Cap divisors to keep 1-resub O(D²) tractable
            if len(divisors) > 100:
                divisors = divisors[-100:]  # prefer nearby (higher topo) nodes

            # --- 0-resub ---
            for d in divisors:
                dsig = sig_tuples[d]
                if dsig == tsig:
                    lit = make_lit(d)
                    if _verify_resub(aig, target, lit):
                        subs = {make_lit(target): lit, negate(make_lit(target)): negate(lit)}
                        aig.remap_literals(subs)
                        changed = True
                        break
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

            # --- 1-resub ---
            n_div = len(divisors)
            found = False

            for i in range(n_div):
                if found:
                    break
                di = divisors[i]
                dsi = sig_tuples[di]

                for j in range(i, n_div):
                    if found:
                        break
                    dj = divisors[j]
                    dsj = sig_tuples[dj]

                    for ni in range(2):
                        if found:
                            break
                        for nj in range(2):
                            si = tuple(~s & MASK64 for s in dsi) if ni else dsi
                            sj = tuple(~s & MASK64 for s in dsj) if nj else dsj

                            match = True
                            for r in range(nr):
                                if tsig[r] != (si[r] & sj[r]):
                                    match = False
                                    break
                            if not match:
                                continue

                            li = negate(make_lit(di)) if ni else make_lit(di)
                            lj = negate(make_lit(dj)) if nj else make_lit(dj)

                            k = (min(li, lj), max(li, lj))
                            if k in gate_hash:
                                if gate_hash[k] == target:
                                    continue
                                new_lit = make_lit(gate_hash[k])
                            elif allow_new_gates:
                                new_lit = _make_and_gate(aig, gate_hash, li, lj)
                            else:
                                continue

                            if lit_to_var(new_lit) == target:
                                continue
                            if _verify_resub(aig, target, new_lit):
                                subs = {make_lit(target): new_lit,
                                        negate(make_lit(target)): negate(new_lit)}
                                aig.remap_literals(subs)
                                changed = found = True
                                break

            if changed:
                aig = dead_node_elimination(aig)
                aig = structural_hashing(aig)
                break

        if not changed:
            break

    return aig
