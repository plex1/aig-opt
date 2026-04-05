"""Multi-output optimization: finds gate sharing across outputs.

The single-output DAG rewriter optimizes one node at a time and cannot
discover that two outputs could share intermediate gates. This pass
groups outputs by shared input support and jointly resynthesizes them.

Two strategies:
- Strategy A (shared-context): synthesize outputs sequentially in a shared
  SynthesisContext so structural hashing reuses gates across outputs.
- Strategy B (exhaustive): for small groups (≤5 inputs, ≤3 outputs),
  enumerate all possible AND-gate networks bottom-up to find the minimum
  implementation.
"""

from __future__ import annotations

from itertools import combinations, permutations

from .aig import (
    AIG,
    CONST_FALSE,
    CONST_TRUE,
    lit_to_var,
    is_negated,
    make_lit,
    negate,
)


# ---------------------------------------------------------------------------
# Output support computation
# ---------------------------------------------------------------------------

def compute_output_support(aig: AIG) -> list[set[int]]:
    """For each output, return the set of primary inputs in its transitive fanin."""
    input_set = set(aig.inputs)
    # Cache support per variable to avoid redundant traversals
    cache: dict[int, set[int]] = {}

    def _support(var: int) -> set[int]:
        if var in cache:
            return cache[var]
        if var in input_set:
            cache[var] = {var}
            return cache[var]
        if var not in aig.and_gates:
            cache[var] = set()
            return cache[var]
        r0, r1 = aig.and_gates[var]
        s = _support(lit_to_var(r0)) | _support(lit_to_var(r1))
        cache[var] = s
        return s

    result = []
    for out_lit in aig.outputs:
        var = lit_to_var(out_lit)
        if var == 0:
            result.append(set())
        else:
            result.append(set(_support(var)))
    return result


# ---------------------------------------------------------------------------
# Output grouping
# ---------------------------------------------------------------------------

def find_output_groups(
    aig: AIG, max_inputs: int = 5, max_outputs: int = 3,
) -> list[list[int]]:
    """Find groups of output indices with small combined input support.

    Returns groups sorted by combined input count (smallest first).
    Only returns groups where joint optimization might help (shared inputs).
    """
    supports = compute_output_support(aig)
    n_outputs = len(aig.outputs)

    if n_outputs > 16:
        # Too many outputs for exhaustive pairing — only try adjacent pairs
        # that share inputs
        groups = []
        for i in range(n_outputs):
            for j in range(i + 1, min(i + 8, n_outputs)):
                union = supports[i] | supports[j]
                if len(union) <= max_inputs and supports[i] & supports[j]:
                    groups.append([i, j])
        groups.sort(key=lambda g: len(supports[g[0]] | supports[g[1]]))
        return groups

    groups: list[list[int]] = []

    # Try all pairs
    for i, j in combinations(range(n_outputs), 2):
        union = supports[i] | supports[j]
        if len(union) <= max_inputs and supports[i] & supports[j]:
            groups.append([i, j])

    # Try triples if max_outputs >= 3
    if max_outputs >= 3:
        for i, j, k in combinations(range(n_outputs), 3):
            union = supports[i] | supports[j] | supports[k]
            if len(union) <= max_inputs:
                # At least two must share inputs
                shared = (
                    bool(supports[i] & supports[j])
                    or bool(supports[i] & supports[k])
                    or bool(supports[j] & supports[k])
                )
                if shared:
                    groups.append([i, j, k])

    # Sort by combined input count (cheapest first)
    groups.sort(key=lambda g: len(set().union(*(supports[i] for i in g))))
    return groups


# ---------------------------------------------------------------------------
# Multi-output truth table computation
# ---------------------------------------------------------------------------

def compute_group_truth_tables(
    aig: AIG,
    output_indices: list[int],
) -> tuple[list[int], list[int]]:
    """Compute truth tables for a group of outputs over their combined input set.

    Returns (list_of_truth_tables, sorted_combined_input_vars).
    """
    supports = compute_output_support(aig)
    combined_inputs = sorted(set().union(*(supports[i] for i in output_indices)))
    n = len(combined_inputs)

    if n == 0:
        # All outputs are constant
        tts = []
        for oi in output_indices:
            out_lit = aig.outputs[oi]
            if out_lit == CONST_TRUE:
                tts.append(1)
            else:
                tts.append(0)
        return tts, combined_inputs

    # Collect all vars needed for evaluation (topological order)
    input_set = set(aig.inputs)
    needed_vars: set[int] = set()

    def _collect(var: int) -> None:
        if var in needed_vars or var in input_set or var == 0:
            return
        if var not in aig.and_gates:
            return
        needed_vars.add(var)
        r0, r1 = aig.and_gates[var]
        _collect(lit_to_var(r0))
        _collect(lit_to_var(r1))

    for oi in output_indices:
        _collect(lit_to_var(aig.outputs[oi]))

    # Topological order of needed gates
    ordered = [v for v in aig.topological_sort_gates() if v in needed_vars]

    # Simulate all 2^n patterns
    tts = [0] * len(output_indices)
    for pattern in range(1 << n):
        val: dict[int, int] = {0: 0}
        for j, inp_var in enumerate(combined_inputs):
            val[inp_var] = (pattern >> j) & 1
        # Set non-combined inputs to 0 (they don't affect these outputs)
        for v in aig.inputs:
            if v not in val:
                val[v] = 0

        for v in ordered:
            r0, r1 = aig.and_gates[v]
            a = val.get(lit_to_var(r0), 0) ^ (r0 & 1)
            b = val.get(lit_to_var(r1), 0) ^ (r1 & 1)
            val[v] = a & b

        for idx, oi in enumerate(output_indices):
            out_lit = aig.outputs[oi]
            out_val = val.get(lit_to_var(out_lit), 0) ^ (out_lit & 1)
            if out_val:
                tts[idx] |= (1 << pattern)

    return tts, combined_inputs


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------

def count_group_exclusive_gates(
    aig: AIG, output_indices: list[int],
) -> int:
    """Count AND gates exclusive to a group of outputs.

    A gate is "exclusive" if it's only reachable from outputs in the group,
    not from any other output. Only exclusive gates can be saved by
    resynthesis (shared gates must remain).
    """
    input_set = set(aig.inputs)

    # Collect gates reachable from group outputs
    group_gates: set[int] = set()

    def _collect_gates(var: int, target: set[int]) -> None:
        if var in target or var in input_set or var == 0:
            return
        if var not in aig.and_gates:
            return
        target.add(var)
        r0, r1 = aig.and_gates[var]
        _collect_gates(lit_to_var(r0), target)
        _collect_gates(lit_to_var(r1), target)

    group_set = set(output_indices)
    for oi in output_indices:
        _collect_gates(lit_to_var(aig.outputs[oi]), group_gates)

    # Collect gates reachable from OTHER outputs
    other_gates: set[int] = set()
    for oi in range(len(aig.outputs)):
        if oi not in group_set:
            _collect_gates(lit_to_var(aig.outputs[oi]), other_gates)

    # Also include latch dependencies
    for _, nxt_lit in aig.latches:
        _collect_gates(lit_to_var(nxt_lit), other_gates)

    return len(group_gates - other_gates)


# ---------------------------------------------------------------------------
# Strategy B: Exhaustive multi-output exact synthesis
# ---------------------------------------------------------------------------

def exhaustive_multioutput_synth(
    truth_tables: list[int],
    num_inputs: int,
    max_gates: int,
    time_budget: float = 10.0,
) -> tuple[list[int], list[tuple[int, int]]] | None:
    """Find minimum AND-gate network implementing all output truth tables.

    Uses iterative-deepening DFS over gate networks. Each signal in the pool
    has a precomputed truth table for O(1) gate evaluation.

    Returns (output_signal_indices, gate_list) where each gate is (sig_a, sig_b)
    indexing into the signal pool, or None if no improvement found.

    Signal pool layout:
      0: constant false (tt=0)
      1: constant true (tt=mask)
      2..2+2*num_inputs-1: inputs and their complements
      2+2*num_inputs..: new gates and their complements
    """
    import time as _time
    deadline = _time.monotonic() + time_budget

    mask = (1 << (1 << num_inputs)) - 1 if num_inputs > 0 else 1

    target_tts = [tt & mask for tt in truth_tables]

    # Initialize signal pool
    pool_tts: list[int] = [0, mask]  # const false, const true
    for i in range(num_inputs):
        var_tt = 0
        for pat in range(1 << num_inputs):
            if (pat >> i) & 1:
                var_tt |= (1 << pat)
        pool_tts.append(var_tt)
        pool_tts.append(var_tt ^ mask)

    seen_tts: set[int] = set(pool_tts)
    gates: list[tuple[int, int]] = []
    timed_out = False

    def _check_outputs() -> list[int] | None:
        result = []
        for target in target_tts:
            found = False
            for si, stt in enumerate(pool_tts):
                if stt == target:
                    result.append(si)
                    found = True
                    break
                if (stt ^ mask) == target:
                    result.append(~si)
                    found = True
                    break
            if not found:
                return None
        return result

    sol = _check_outputs()
    if sol is not None:
        return sol, []

    call_count = 0

    def _dfs(remaining: int, min_first: int) -> list[int] | None:
        nonlocal timed_out, call_count
        n_pool = len(pool_tts)
        for i in range(min_first, n_pool):
            for j in range(i, n_pool):
                if timed_out:
                    return None
                call_count += 1
                if call_count & 0xFFF == 0:  # check every 4096 iterations
                    if _time.monotonic() > deadline:
                        timed_out = True
                        return None

                new_tt = pool_tts[i] & pool_tts[j]
                if new_tt in seen_tts or (new_tt ^ mask) in seen_tts:
                    continue

                pool_tts.append(new_tt)
                pool_tts.append(new_tt ^ mask)
                seen_tts.add(new_tt)
                seen_tts.add(new_tt ^ mask)
                gates.append((i, j))

                if remaining == 1:
                    sol = _check_outputs()
                    if sol is not None:
                        return sol
                else:
                    sol = _dfs(remaining - 1, i)
                    if sol is not None:
                        return sol

                gates.pop()
                seen_tts.discard(new_tt)
                seen_tts.discard(new_tt ^ mask)
                pool_tts.pop()
                pool_tts.pop()

        return None

    for k in range(1, max_gates):
        if timed_out:
            break
        sol = _dfs(k, 0)
        if sol is not None:
            return sol, list(gates)

    return None


# ---------------------------------------------------------------------------
# Strategy A: Shared-context synthesis
# ---------------------------------------------------------------------------

def shared_context_resynth(
    truth_tables: list[int],
    num_inputs: int,
    leaf_lits: list[int],
    next_var: int,
) -> tuple[list[int], dict[int, tuple[int, int]], int]:
    """Synthesize multiple outputs in a shared SynthesisContext.

    Tries all output orderings and input permutations to maximize gate sharing.
    Returns (result_lits, new_gates, new_next_var).
    """
    from .rewriter import SynthesisContext, synthesize_tt
    from .npn import permute_tt

    n_outputs = len(truth_tables)
    best_lits: list[int] = [CONST_FALSE] * n_outputs
    best_gates: dict[int, tuple[int, int]] = {}
    best_next_var = next_var
    best_count = float('inf')

    # Try all orderings of outputs
    for out_perm in permutations(range(n_outputs)):
        # Try all input permutations
        for in_perm in permutations(range(num_inputs)):
            perm_lits = [leaf_lits[in_perm[i]] for i in range(num_inputs)]

            ctx = SynthesisContext({}, next_var)
            result_lits = [CONST_FALSE] * n_outputs
            for oi in out_perm:
                perm_tt = permute_tt(truth_tables[oi], in_perm, num_inputs)
                result_lits[oi] = synthesize_tt(perm_tt, num_inputs, perm_lits, ctx)

            if ctx.num_new_gates < best_count:
                best_count = ctx.num_new_gates
                best_lits = list(result_lits)
                best_gates = dict(ctx.new_gates)
                best_next_var = ctx.next_var

                if best_count == 0:
                    return best_lits, best_gates, best_next_var

    return best_lits, best_gates, best_next_var


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_multioutput(
    truth_tables: list[int],
    new_gates: dict[int, tuple[int, int]],
    result_lits: list[int],
    input_vars: list[int],
) -> bool:
    """Verify that new_gates + result_lits produce the expected truth tables."""
    n = len(input_vars)
    mask = (1 << (1 << n)) - 1 if n > 0 else 1

    gate_order = sorted(new_gates.keys())

    for pattern in range(1 << n):
        val: dict[int, int] = {0: 0}
        for j, v in enumerate(input_vars):
            val[v] = (pattern >> j) & 1

        for v in gate_order:
            r0, r1 = new_gates[v]
            a = val.get(lit_to_var(r0), 0) ^ (r0 & 1)
            b = val.get(lit_to_var(r1), 0) ^ (r1 & 1)
            val[v] = a & b

        for idx, (expected_tt, res_lit) in enumerate(zip(truth_tables, result_lits)):
            actual = val.get(lit_to_var(res_lit), 0) ^ (res_lit & 1)
            expected = (expected_tt >> pattern) & 1
            if actual != expected:
                return False

    return True


# ---------------------------------------------------------------------------
# Conversion: exhaustive result -> AIG gates
# ---------------------------------------------------------------------------

def _build_gates_from_exhaustive(
    sol_signals: list[int],
    gate_list: list[tuple[int, int]],
    num_inputs: int,
    input_vars: list[int],
    next_var: int,
    mask: int,
    pool_tts: list[int],
    target_tts: list[int],
) -> tuple[list[int], dict[int, tuple[int, int]], int]:
    """Convert exhaustive synthesis result to AIG gates.

    Maps signal indices back to AIG literals.
    """
    # Signal index -> AIG literal mapping
    sig_to_lit: dict[int, int] = {}
    sig_to_lit[0] = CONST_FALSE
    sig_to_lit[1] = CONST_TRUE
    for i, v in enumerate(input_vars):
        sig_to_lit[2 + 2 * i] = make_lit(v)          # positive
        sig_to_lit[2 + 2 * i + 1] = negate(make_lit(v))  # negative

    new_gates: dict[int, tuple[int, int]] = {}
    n_base = 2 + 2 * num_inputs

    for gi, (si, sj) in enumerate(gate_list):
        gate_sig_pos = n_base + 2 * gi  # positive signal index
        var = next_var
        next_var += 1

        lit_a = sig_to_lit[si]
        lit_b = sig_to_lit[sj]
        new_gates[var] = (min(lit_a, lit_b), max(lit_a, lit_b))

        sig_to_lit[gate_sig_pos] = make_lit(var)
        sig_to_lit[gate_sig_pos + 1] = negate(make_lit(var))

    # Map output signals to literals
    result_lits = []
    for sig_idx in sol_signals:
        if sig_idx >= 0:
            result_lits.append(sig_to_lit[sig_idx])
        else:
            # ~si means complement of signal si
            result_lits.append(negate(sig_to_lit[~sig_idx]))

    return result_lits, new_gates, next_var


# ---------------------------------------------------------------------------
# Top-level pass
# ---------------------------------------------------------------------------

def multioutput_resynth(aig: AIG) -> AIG:
    """Multi-output resynthesis: find gate sharing across outputs."""
    from .optimizer import dead_node_elimination

    improved = True
    while improved:
        improved = False
        groups = find_output_groups(aig, max_inputs=5, max_outputs=3)

        for group in groups:
            # Skip groups where any output is a constant or direct input
            skip = False
            for oi in group:
                var = lit_to_var(aig.outputs[oi])
                if var == 0 or var in set(aig.inputs):
                    skip = True
                    break
            if skip:
                continue

            tts, combined_inputs = compute_group_truth_tables(aig, group)
            n = len(combined_inputs)
            if n == 0:
                continue

            current_cost = count_group_exclusive_gates(aig, group)
            if current_cost <= 1:
                continue

            mask = (1 << (1 << n)) - 1

            # Strategy B: exhaustive (for small cases)
            best_lits = None
            best_gates: dict[int, tuple[int, int]] = {}
            best_next_var = aig.max_var + 1
            best_cost = current_cost

            # Strategy B is only tractable for small cases
            # Depth limits based on input count (empirically timed):
            #   2 inputs: instant up to ~10 gates
            #   3 inputs: ~9s at depth 7, ~0.5s at depth 6
            #   4 inputs: ~0.5s at depth 5
            #   5 inputs: ~0.5s at depth 4
            # Depth limits tuned empirically:
            #   2 inputs: instant up to depth 10
            #   3 inputs: depth 7 in ~9s, depth 6 in ~0.5s
            #   4 inputs: depth 5 in ~0.5s
            #   5 inputs: depth 4 in ~0.5s
            max_exact = {0: 10, 1: 10, 2: 10, 3: 7, 4: 5, 5: 4}.get(n, 0)
            if n <= 5 and len(group) <= 3 and current_cost <= max_exact:
                result = exhaustive_multioutput_synth(tts, n, current_cost)
                if result is not None:
                    sol_signals, gate_list = result
                    # Rebuild pool_tts for conversion
                    pool_tts = [0, mask]
                    for i in range(n):
                        var_tt = 0
                        for pat in range(1 << n):
                            if (pat >> i) & 1:
                                var_tt |= (1 << pat)
                        pool_tts.append(var_tt)
                        pool_tts.append(var_tt ^ mask)

                    lits, gates, nv = _build_gates_from_exhaustive(
                        sol_signals, gate_list, n, combined_inputs,
                        aig.max_var + 1, mask, pool_tts, tts,
                    )
                    new_cost = len(gates)
                    if new_cost < best_cost:
                        if verify_multioutput(tts, gates, lits, combined_inputs):
                            best_lits = lits
                            best_gates = gates
                            best_next_var = nv
                            best_cost = new_cost

            # Strategy A: shared-context (always try, may beat exhaustive for larger cases)
            leaf_lits = [make_lit(v) for v in combined_inputs]
            a_lits, a_gates, a_nv = shared_context_resynth(
                tts, n, leaf_lits, aig.max_var + 1,
            )
            a_cost = len(a_gates)
            if a_cost < best_cost:
                if verify_multioutput(tts, a_gates, a_lits, combined_inputs):
                    best_lits = a_lits
                    best_gates = a_gates
                    best_next_var = a_nv
                    best_cost = a_cost

            if best_lits is not None and best_cost < current_cost:
                # Apply replacement
                aig.and_gates.update(best_gates)
                aig.max_var = max(aig.max_var, best_next_var - 1)
                for idx, oi in enumerate(group):
                    aig.outputs[oi] = best_lits[idx]
                aig = dead_node_elimination(aig)
                improved = True
                break  # Restart grouping after modification

    return aig
