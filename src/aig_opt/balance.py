"""AIG balancing: restructure AND/OR chains into balanced binary trees.

Reduces circuit depth (longest path from input to output) by converting
linear chains like ((a AND b) AND c) AND d (depth 3) into balanced trees
like (a AND b) AND (c AND d) (depth 2), without increasing gate count.

This is analogous to ABC's `balance` command. Alternating balance with
size-focused rewriting helps break convergence plateaus.
"""

from __future__ import annotations

from .aig import (
    AIG,
    CONST_FALSE,
    CONST_TRUE,
    lit_to_var,
    is_negated,
    make_lit,
    negate,
)


def compute_depths(aig: AIG) -> dict[int, int]:
    """Compute the depth (longest path from any input) for each node."""
    depth: dict[int, int] = {0: 0}
    for v in aig.inputs:
        depth[v] = 0
    for v in aig.topological_sort_gates():
        r0, r1 = aig.and_gates[v]
        d0 = depth.get(lit_to_var(r0), 0)
        d1 = depth.get(lit_to_var(r1), 0)
        depth[v] = max(d0, d1) + 1
    return depth


def _collect_and_leaves(
    aig: AIG,
    lit: int,
    fanout: dict[int, int],
    leaves: list[int],
) -> None:
    """Collect leaf literals of an AND chain rooted at `lit`.

    A node is part of the chain if:
    - It is a positive (non-negated) AND gate reference
    - It has fanout == 1 (only used by this chain)

    Otherwise it's a leaf of the chain.
    """
    var = lit_to_var(lit)
    if (
        not is_negated(lit)
        and var in aig.and_gates
        and fanout.get(var, 0) <= 1
    ):
        r0, r1 = aig.and_gates[var]
        _collect_and_leaves(aig, r0, fanout, leaves)
        _collect_and_leaves(aig, r1, fanout, leaves)
    else:
        leaves.append(lit)


def balance(aig: AIG) -> AIG:
    """Balance the AIG by restructuring AND chains into balanced trees.

    For each AND gate that is the root of a chain (has fanout > 1 or is
    referenced by an output), collect all leaf literals of the chain,
    then rebuild as a balanced binary tree sorted by depth (deepest
    leaves paired first to minimize critical path).
    """
    # Compute fanout
    fanout: dict[int, int] = {}
    for var, (r0, r1) in aig.and_gates.items():
        fanout[lit_to_var(r0)] = fanout.get(lit_to_var(r0), 0) + 1
        fanout[lit_to_var(r1)] = fanout.get(lit_to_var(r1), 0) + 1
    for out_lit in aig.outputs:
        v = lit_to_var(out_lit)
        fanout[v] = fanout.get(v, 0) + 1
    for _, nxt in aig.latches:
        v = lit_to_var(nxt)
        fanout[v] = fanout.get(v, 0) + 1

    # Compute depths for sorting leaves
    depth = compute_depths(aig)

    # Structural hash table for new gates
    hash_table: dict[tuple[int, int], int] = {}
    # Seed with existing gates that won't be replaced
    input_set = set(aig.inputs)

    next_var = aig.max_var + 1
    new_gates: dict[int, tuple[int, int]] = {}

    # Track new depths
    new_depth: dict[int, int] = {0: 0}
    for v in aig.inputs:
        new_depth[v] = 0

    def make_and(a: int, b: int) -> int:
        nonlocal next_var
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
        key = (min(a, b), max(a, b))
        if key in hash_table:
            return make_lit(hash_table[key])
        var = next_var
        next_var += 1
        new_gates[var] = key
        hash_table[key] = var
        d0 = new_depth.get(lit_to_var(a), 0)
        d1 = new_depth.get(lit_to_var(b), 0)
        new_depth[var] = max(d0, d1) + 1
        return make_lit(var)

    def build_balanced_tree(leaves: list[int]) -> int:
        """Build a balanced AND tree from a list of literals.

        Sorts by depth (shallowest first) and repeatedly pairs adjacent
        elements, which naturally produces a balanced tree.
        """
        if not leaves:
            return CONST_TRUE
        if len(leaves) == 1:
            return leaves[0]

        # Sort by depth so shallowest are paired first (reduces critical path)
        leaves = sorted(leaves, key=lambda l: new_depth.get(lit_to_var(l), 0))

        while len(leaves) > 1:
            next_level = []
            i = 0
            while i + 1 < len(leaves):
                combined = make_and(leaves[i], leaves[i + 1])
                next_level.append(combined)
                i += 2
            if i < len(leaves):
                next_level.append(leaves[i])
            leaves = next_level

        return leaves[0]

    # Process gates in topological order
    # Map: old_lit -> new_lit
    lit_map: dict[int, int] = {CONST_FALSE: CONST_FALSE, CONST_TRUE: CONST_TRUE}
    for v in aig.inputs:
        lit_map[make_lit(v)] = make_lit(v)
        lit_map[negate(make_lit(v))] = negate(make_lit(v))

    def resolve(lit: int) -> int:
        if lit in lit_map:
            return lit_map[lit]
        # Derive from positive literal
        pos = lit & ~1
        if pos in lit_map:
            result = lit_map[pos]
            if lit & 1:
                return negate(result)
            return result
        return lit

    for var in aig.topological_sort_gates():
        r0, r1 = aig.and_gates[var]
        mr0 = resolve(r0)
        mr1 = resolve(r1)

        # Check if this gate is a chain root (fanout > 1 or referenced externally)
        is_chain_root = fanout.get(var, 0) != 1

        if is_chain_root and not is_negated(mr0) and not is_negated(mr1):
            # Collect AND chain leaves (using original structure for chain detection)
            leaves: list[int] = []
            _collect_and_leaves(aig, r0, fanout, leaves)
            _collect_and_leaves(aig, r1, fanout, leaves)

            if len(leaves) > 2:
                # Resolve all leaves to new literals
                resolved_leaves = [resolve(l) for l in leaves]
                new_lit = build_balanced_tree(resolved_leaves)
                lit_map[make_lit(var)] = new_lit
                lit_map[negate(make_lit(var))] = negate(new_lit)
                continue

        # Default: just create the gate with resolved inputs
        new_lit = make_and(mr0, mr1)
        lit_map[make_lit(var)] = new_lit
        lit_map[negate(make_lit(var))] = negate(new_lit)

    # Build new AIG
    new_outputs = [resolve(o) for o in aig.outputs]
    new_latches = [(v, resolve(nxt)) for v, nxt in aig.latches]

    return AIG(
        max_var=next_var - 1,
        inputs=list(aig.inputs),
        outputs=new_outputs,
        latches=new_latches,
        and_gates=new_gates,
        symbols=dict(aig.symbols),
        comments=list(aig.comments),
    )
