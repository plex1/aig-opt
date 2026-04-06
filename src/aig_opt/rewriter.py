"""DAG-aware AIG rewriting with k-feasible cuts.

Implements the core ideas from:
  "DAG-Aware AIG Rewriting" (Mishchenko et al., DAC 2006)

For each node, enumerates 4-input cuts, computes truth tables,
resynthesizes optimal implementations, and replaces subgraphs
when a smaller implementation exists (accounting for shared nodes).
"""

from __future__ import annotations

import random

from .aig import (
    AIG,
    CONST_FALSE,
    CONST_TRUE,
    lit_to_var,
    is_negated,
    negate,
    make_lit,
)


# ---------------------------------------------------------------------------
# Cut enumeration
# ---------------------------------------------------------------------------

def enumerate_cuts(aig: AIG, max_cut_size: int = 4) -> dict[int, list[frozenset[int]]]:
    """Enumerate k-feasible cuts for every node.

    A cut of node v is a set of nodes (leaves) such that every path from
    a primary input to v passes through at least one leaf.
    """
    input_set = set(aig.inputs)
    cuts: dict[int, list[frozenset[int]]] = {}

    for v in aig.inputs:
        cuts[v] = [frozenset([v])]

    for var in sorted(aig.and_gates.keys()):
        r0, r1 = aig.and_gates[var]
        v0, v1 = lit_to_var(r0), lit_to_var(r1)

        node_cuts: list[frozenset[int]] = [frozenset([var])]

        cuts0 = cuts.get(v0, [frozenset([v0])]) if v0 > 0 else [frozenset()]
        cuts1 = cuts.get(v1, [frozenset([v1])]) if v1 > 0 else [frozenset()]

        seen: set[frozenset[int]] = {node_cuts[0]}
        for c0 in cuts0:
            for c1 in cuts1:
                merged = c0 | c1
                if len(merged) <= max_cut_size and merged not in seen:
                    seen.add(merged)
                    node_cuts.append(merged)

        cuts[var] = node_cuts[:48]

    return cuts


# ---------------------------------------------------------------------------
# Simulation-based truth table computation
# ---------------------------------------------------------------------------

def compute_cut_truth_table(
    aig: AIG,
    root_var: int,
    cut_leaves: frozenset[int],
) -> tuple[int, list[int]]:
    """Compute truth table of the function at root_var with cut_leaves as inputs.

    Uses direct simulation, treating cut leaves as free variables.
    Returns: (truth_table_as_int, sorted_leaf_list)
    """
    leaves = sorted(cut_leaves)
    n = len(leaves)
    if n == 0:
        return 0, leaves

    # Build the set of internal nodes between root and leaves
    # (all nodes reachable from root that are not leaves)
    internal_order: list[int] = []
    visited: set[int] = set()

    def _collect(v: int) -> None:
        if v in visited or v == 0:
            return
        visited.add(v)
        if v in cut_leaves:
            return
        if v in aig.and_gates:
            r0, r1 = aig.and_gates[v]
            _collect(lit_to_var(r0))
            _collect(lit_to_var(r1))
            internal_order.append(v)

    _collect(root_var)

    tt = 0
    leaf_set = set(leaves)
    leaf_idx = {v: i for i, v in enumerate(leaves)}

    for i in range(1 << n):
        # Assign values to leaves
        val: dict[int, int] = {0: 0}
        for j, leaf in enumerate(leaves):
            val[leaf] = (i >> j) & 1

        # Evaluate internal nodes in topological order
        for v in internal_order:
            r0, r1 = aig.and_gates[v]
            v0 = lit_to_var(r0)
            v1 = lit_to_var(r1)
            a = val.get(v0, 0) ^ is_negated(r0)
            b = val.get(v1, 0) ^ is_negated(r1)
            val[v] = a & b

        result = val.get(root_var, 0)
        if result:
            tt |= (1 << i)

    return tt, leaves


# ---------------------------------------------------------------------------
# Truth table synthesis — build optimal AIG from truth table
# ---------------------------------------------------------------------------

class SynthesisContext:
    """Builds new AND gates in isolation, with structural hashing."""

    def __init__(self, existing_hash: dict[tuple[int, int], int], next_var: int):
        self.hash_table = dict(existing_hash)
        self.next_var = next_var
        self.new_gates: dict[int, tuple[int, int]] = {}

    def make_and(self, lit0: int, lit1: int) -> int:
        """Create AND(lit0, lit1), returning the result literal. Reuses existing gates."""
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

        key = (min(lit0, lit1), max(lit0, lit1))
        if key in self.hash_table:
            return make_lit(self.hash_table[key])

        var = self.next_var
        self.next_var += 1
        self.new_gates[var] = key
        self.hash_table[key] = var
        return make_lit(var)

    def make_or(self, lit0: int, lit1: int) -> int:
        """OR(a, b) = NOT(AND(NOT(a), NOT(b)))"""
        return negate(self.make_and(negate(lit0), negate(lit1)))

    def make_mux(self, sel: int, lit1: int, lit0: int) -> int:
        """MUX(sel, d1, d0) = (sel AND d1) OR (!sel AND d0)"""
        a = self.make_and(sel, lit1)
        b = self.make_and(negate(sel), lit0)
        return self.make_or(a, b)

    @property
    def num_new_gates(self) -> int:
        return len(self.new_gates)


def synthesize_tt(
    tt: int,
    num_inputs: int,
    leaf_lits: list[int],
    ctx: SynthesisContext,
) -> int:
    """Synthesize an AIG implementing the given truth table.

    Returns the result literal. New gates are stored in ctx.
    """
    mask = (1 << (1 << num_inputs)) - 1 if num_inputs > 0 else 1
    tt &= mask

    # Constant
    if tt == 0:
        return CONST_FALSE
    if tt == mask:
        return CONST_TRUE

    # Single variable
    if num_inputs == 1:
        if tt == 0b10:
            return leaf_lits[0]
        if tt == 0b01:
            return negate(leaf_lits[0])

    # Check if function equals a single literal
    for i in range(num_inputs):
        var_tt = _var_truth_table(i, num_inputs)
        if tt == var_tt:
            return leaf_lits[i]
        if tt == (var_tt ^ mask):
            return negate(leaf_lits[i])

    # Try AND/NAND of two literals (1 gate)
    if num_inputs >= 2:
        result = _try_two_literal_synth(tt, num_inputs, leaf_lits, mask, ctx)
        if result is not None:
            return result

    # Try XOR detection for 2 inputs: XOR = 3 gates in AIG
    if num_inputs == 2:
        if tt == 0b0110:  # XOR
            a, b = leaf_lits
            return negate(ctx.make_and(
                negate(ctx.make_and(a, negate(b))),
                negate(ctx.make_and(negate(a), b)),
            ))
        if tt == 0b1001:  # XNOR
            a, b = leaf_lits
            return ctx.make_and(
                negate(ctx.make_and(a, negate(b))),
                negate(ctx.make_and(negate(a), b)),
            )

    # Shannon decomposition
    best_var = _pick_decomposition_var(tt, num_inputs)
    pos_cof = _cofactor(tt, best_var, 1, num_inputs)
    neg_cof = _cofactor(tt, best_var, 0, num_inputs)

    reduced_lits = leaf_lits[:best_var] + leaf_lits[best_var + 1:]
    reduced_n = num_inputs - 1
    xi = leaf_lits[best_var]

    # Synthesize cofactors
    lit_pos = synthesize_tt(pos_cof, reduced_n, reduced_lits, ctx)
    lit_neg = synthesize_tt(neg_cof, reduced_n, reduced_lits, ctx)

    # f = MUX(xi, f1, f0) = (xi AND f1) OR (!xi AND f0)
    return ctx.make_mux(xi, lit_pos, lit_neg)


def _var_truth_table(var_idx: int, num_inputs: int) -> int:
    """Truth table for a single variable."""
    tt = 0
    for i in range(1 << num_inputs):
        if (i >> var_idx) & 1:
            tt |= (1 << i)
    return tt


def _cofactor(tt: int, var_idx: int, val: int, num_inputs: int) -> int:
    """Compute the cofactor of tt with respect to variable var_idx = val."""
    result = 0
    out_bit = 0
    for i in range(1 << num_inputs):
        if ((i >> var_idx) & 1) == val:
            if (tt >> i) & 1:
                result |= (1 << out_bit)
            out_bit += 1
    return result


def _pick_decomposition_var(tt: int, num_inputs: int) -> int:
    """Pick the variable that gives the most simplification."""
    best_var = 0
    best_score = -1

    for i in range(num_inputs):
        pos_cof = _cofactor(tt, i, 1, num_inputs)
        neg_cof = _cofactor(tt, i, 0, num_inputs)
        reduced_mask = (1 << (1 << (num_inputs - 1))) - 1

        score = 0
        if pos_cof == 0 or pos_cof == reduced_mask:
            score += 10
        if neg_cof == 0 or neg_cof == reduced_mask:
            score += 10
        if pos_cof == neg_cof:
            score += 20
        score += abs(bin(pos_cof).count('1') - bin(neg_cof).count('1'))

        if score > best_score:
            best_score = score
            best_var = i

    return best_var


def _try_two_literal_synth(
    tt: int, num_inputs: int, leaf_lits: list[int],
    mask: int, ctx: SynthesisContext,
) -> int | None:
    """Try to implement tt as AND/OR/NAND/NOR of two (possibly negated) inputs."""
    var_tts = [_var_truth_table(i, num_inputs) for i in range(num_inputs)]

    candidates = []
    for i in range(num_inputs):
        candidates.append((var_tts[i], leaf_lits[i]))
        candidates.append((var_tts[i] ^ mask, negate(leaf_lits[i])))

    for ai in range(len(candidates)):
        tt_a, lit_a = candidates[ai]
        for bi in range(ai + 1, len(candidates)):
            tt_b, lit_b = candidates[bi]
            if (tt_a & tt_b) & mask == tt:
                return ctx.make_and(lit_a, lit_b)
            if ((tt_a & tt_b) ^ mask) & mask == tt:
                return negate(ctx.make_and(lit_a, lit_b))
            # OR
            if ((tt_a | tt_b) & mask) == tt:
                return ctx.make_or(lit_a, lit_b)
            # NOR
            if (((tt_a | tt_b) ^ mask) & mask) == tt:
                return negate(ctx.make_or(lit_a, lit_b))

    return None


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_synthesis(
    aig: AIG,
    new_gates: dict[int, tuple[int, int]],
    result_lit: int,
    expected_tt: int,
    leaves: list[int],
) -> bool:
    """Verify that the synthesized circuit produces the correct truth table."""
    n = len(leaves)
    for i in range(1 << n):
        val: dict[int, int] = {0: 0}
        for j, leaf in enumerate(leaves):
            val[leaf] = (i >> j) & 1

        # Evaluate existing gates that might be referenced
        for var in sorted(aig.and_gates.keys()):
            if var in val:
                continue
            r0, r1 = aig.and_gates[var]
            v0, v1 = lit_to_var(r0), lit_to_var(r1)
            if v0 in val and v1 in val:
                a = val[v0] ^ is_negated(r0)
                b = val[v1] ^ is_negated(r1)
                val[var] = a & b

        # Evaluate new gates in order (they have increasing var indices)
        for var in sorted(new_gates.keys()):
            r0, r1 = new_gates[var]
            v0, v1 = lit_to_var(r0), lit_to_var(r1)
            a = val.get(v0, 0) ^ is_negated(r0)
            b = val.get(v1, 0) ^ is_negated(r1)
            val[var] = a & b

        v = lit_to_var(result_lit)
        result = val.get(v, 0) ^ is_negated(result_lit)
        expected = (expected_tt >> i) & 1

        if result != expected:
            return False

    return True


# ---------------------------------------------------------------------------
# Fanout / reference counting
# ---------------------------------------------------------------------------

def compute_fanout(aig: AIG) -> dict[int, int]:
    """Count how many times each variable is referenced."""
    fanout: dict[int, int] = {}

    for var, (r0, r1) in aig.and_gates.items():
        for lit in (r0, r1):
            v = lit_to_var(lit)
            fanout[v] = fanout.get(v, 0) + 1

    for o in aig.outputs:
        v = lit_to_var(o)
        fanout[v] = fanout.get(v, 0) + 1

    for _, nxt in aig.latches:
        v = lit_to_var(nxt)
        fanout[v] = fanout.get(v, 0) + 1

    return fanout


def compute_subgraph_cost(
    aig: AIG,
    root_var: int,
    cut_leaves: frozenset[int],
    fanout: dict[int, int],
) -> int:
    """Compute the DAG-aware cost of the subgraph between root and cut leaves.

    Only counts gates whose only consumers are within the subgraph.
    """
    internal: set[int] = set()
    stack = [root_var]

    while stack:
        v = stack.pop()
        if v in cut_leaves or v in internal:
            continue
        if v not in aig.and_gates:
            continue
        internal.add(v)
        r0, r1 = aig.and_gates[v]
        stack.append(lit_to_var(r0))
        stack.append(lit_to_var(r1))

    cost = 0
    for v in internal:
        if v == root_var or fanout.get(v, 0) <= 1:
            cost += 1

    return cost


# ---------------------------------------------------------------------------
# Main DAG-aware rewriting pass
# ---------------------------------------------------------------------------

def _validate_cut(aig: AIG, root_var: int, cut_leaves: frozenset[int]) -> bool:
    """Check that all cut leaves are actually on a path from root to inputs."""
    reachable: set[int] = set()
    stack = [root_var]
    while stack:
        v = stack.pop()
        if v in reachable or v == 0:
            continue
        reachable.add(v)
        if v in cut_leaves:
            continue  # don't go below leaves
        if v in aig.and_gates:
            r0, r1 = aig.and_gates[v]
            stack.append(lit_to_var(r0))
            stack.append(lit_to_var(r1))
    return all(leaf in reachable for leaf in cut_leaves)


def dag_rewrite(
    aig: AIG,
    iterations: int = 10,
    max_cut_size: int = 5,
    perturbation: float = 0.0,
    rng: random.Random | None = None,
) -> AIG:
    """DAG-aware AIG rewriting.

    For each node, enumerates k-feasible cuts, evaluates the truth table,
    resynthesizes, verifies correctness, and replaces if beneficial.
    After each replacement, cuts are recomputed to avoid stale references.

    Args:
        perturbation: probability [0, 1] of picking a random improving
            replacement instead of the best one.  Shuffles node order too.
            Set > 0 for stochastic exploration.
        rng: random.Random instance (used when perturbation > 0).
    """
    from .npn import get_optimal_gate_count, synthesize_optimal

    if rng is None:
        rng = random.Random(42)

    for _iter in range(iterations):
        improved = False

        all_cuts = enumerate_cuts(aig, max_cut_size)
        fanout = compute_fanout(aig)

        # Build structural hash table from existing gates
        hash_table: dict[tuple[int, int], int] = {}
        for var, (r0, r1) in aig.and_gates.items():
            key = (min(r0, r1), max(r0, r1))
            hash_table[key] = var

        nodes = list(aig.and_gates.keys())
        if perturbation > 0:
            rng.shuffle(nodes)
        else:
            nodes.sort()

        for var in nodes:
            if var not in aig.and_gates:
                continue

            node_cuts = all_cuts.get(var, [])
            improving: list[tuple[int, int, dict, int]] = []

            for cut in node_cuts:
                if len(cut) <= 1 or len(cut) > max_cut_size:
                    continue

                # Validate cut is still valid after prior modifications
                if not _validate_cut(aig, var, cut):
                    continue

                current_cost = compute_subgraph_cost(aig, var, cut, fanout)
                if current_cost <= 1:
                    continue

                tt, leaves = compute_cut_truth_table(aig, var, cut)
                n = len(leaves)
                if n == 0:
                    continue

                # Quick NPN check: can we possibly improve?
                optimal = get_optimal_gate_count(tt, n)
                if optimal is not None and optimal >= current_cost:
                    continue  # No possible improvement

                leaf_lits = [make_lit(v) for v in leaves]

                # Multi-decomposition synthesis (tries all k! variable orderings)
                new_lit, new_gates, new_next_var = synthesize_optimal(
                    tt, n, leaf_lits, hash_table, aig.max_var + 1,
                )
                new_cost = len(new_gates)

                saving = current_cost - new_cost
                if saving > 0:
                    # Verify correctness before accepting
                    if verify_synthesis(aig, new_gates, new_lit, tt, leaves):
                        improving.append((saving, new_lit, new_gates, new_next_var))

            if not improving:
                continue

            # Pick replacement: best or random (with perturbation probability)
            if len(improving) > 1 and perturbation > 0 and rng.random() < perturbation:
                _, new_lit, new_gates, next_var = rng.choice(improving)
            else:
                _, new_lit, new_gates, next_var = max(improving, key=lambda x: x[0])

            # Add new gates to the AIG
            aig.and_gates.update(new_gates)
            aig.max_var = max(aig.max_var, next_var - 1)

            # Substitute old root with new literal
            old_lit = make_lit(var)
            if new_lit != old_lit:
                subs = {
                    old_lit: new_lit,
                    negate(old_lit): negate(new_lit),
                }
                aig.remap_literals(subs)

            improved = True
            # Break to recompute cuts — they're stale after remap
            break

        if not improved:
            break

    return aig
