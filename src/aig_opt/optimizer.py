"""AIG optimization passes."""

from __future__ import annotations

from .aig import (
    AIG,
    CONST_FALSE,
    CONST_TRUE,
    lit_to_var,
    is_negated,
    negate,
    make_lit,
    resolve,
)


def constant_propagation(aig: AIG) -> AIG:
    """Propagate constants and simplify trivial patterns.

    - 0 AND x = 0
    - 1 AND x = x
    - x AND x = x
    - x AND !x = 0
    """
    changed = True
    while changed:
        changed = False
        subs: dict[int, int] = {}
        to_remove = []

        for var in sorted(aig.and_gates.keys()):
            r0, r1 = aig.and_gates[var]
            r0 = resolve(subs, r0)
            r1 = resolve(subs, r1)
            out = make_lit(var)

            replacement = None

            # Constant propagation
            if r0 == CONST_FALSE or r1 == CONST_FALSE:
                replacement = CONST_FALSE
            elif r0 == CONST_TRUE:
                replacement = r1
            elif r1 == CONST_TRUE:
                replacement = r0
            # Trivial simplifications
            elif r0 == r1:
                replacement = r0
            elif r0 == negate(r1):
                replacement = CONST_FALSE

            if replacement is not None:
                subs[out] = replacement
                subs[negate(out)] = negate(replacement)
                to_remove.append(var)
                changed = True
            else:
                # Update gate inputs with resolved values
                aig.and_gates[var] = (r0, r1)

        for var in to_remove:
            del aig.and_gates[var]

        if subs:
            aig.remap_literals(subs)

    return aig


def structural_hashing(aig: AIG) -> AIG:
    """Merge AND gates with identical (possibly permuted) inputs."""
    seen: dict[tuple[int, int], int] = {}  # normalized (r0, r1) -> first var
    subs: dict[int, int] = {}
    to_remove = []

    for var in sorted(aig.and_gates.keys()):
        r0, r1 = aig.and_gates[var]
        r0 = resolve(subs, r0)
        r1 = resolve(subs, r1)

        # Normalize: smaller literal first
        key = (min(r0, r1), max(r0, r1))

        if key in seen:
            existing_var = seen[key]
            out = make_lit(var)
            existing_out = make_lit(existing_var)
            subs[out] = existing_out
            subs[negate(out)] = negate(existing_out)
            to_remove.append(var)
        else:
            seen[key] = var
            aig.and_gates[var] = (r0, r1)

    for var in to_remove:
        del aig.and_gates[var]

    if subs:
        aig.remap_literals(subs)

    return aig


def dead_node_elimination(aig: AIG) -> AIG:
    """Remove AND gates not reachable from any output or latch next-state."""
    # Collect live variables via BFS from outputs and latch next-states
    live: set[int] = set()
    worklist: list[int] = []

    for o in aig.outputs:
        v = lit_to_var(o)
        if v > 0 and v not in live:
            live.add(v)
            worklist.append(v)

    for _, nxt in aig.latches:
        v = lit_to_var(nxt)
        if v > 0 and v not in live:
            live.add(v)
            worklist.append(v)

    while worklist:
        v = worklist.pop()
        if v in aig.and_gates:
            r0, r1 = aig.and_gates[v]
            for lit in (r0, r1):
                dep = lit_to_var(lit)
                if dep > 0 and dep not in live:
                    live.add(dep)
                    worklist.append(dep)

    # Remove dead gates
    dead = [v for v in aig.and_gates if v not in live]
    for v in dead:
        del aig.and_gates[v]

    return aig


def simple_rewrite(aig: AIG) -> AIG:
    """Simple local rewriting of 2-level cones.

    Key pattern: (a AND b) AND (a AND c) -> a AND (b AND c)
    This saves one AND gate by factoring out the shared input.
    """
    changed = True
    while changed:
        changed = False
        for var in sorted(aig.and_gates.keys()):
            if var not in aig.and_gates:
                continue
            r0, r1 = aig.and_gates[var]

            # Both inputs must be positive (non-negated) AND gate outputs
            v0, v1 = lit_to_var(r0), lit_to_var(r1)
            if is_negated(r0) or is_negated(r1):
                continue
            if v0 not in aig.and_gates or v1 not in aig.and_gates:
                continue

            a0, b0 = aig.and_gates[v0]
            a1, b1 = aig.and_gates[v1]

            # Check all combinations for a shared input
            shared = None
            other0 = None
            other1 = None

            pairs = [
                (a0, b0, a1, b1),  # a0==a1
                (a0, b0, b1, a1),  # a0==b1
                (b0, a0, a1, b1),  # b0==a1
                (b0, a0, b1, a1),  # b0==b1
            ]

            for s0, o0, s1, o1 in pairs:
                if s0 == s1:
                    shared = s0
                    other0 = o0
                    other1 = o1
                    break

            if shared is None:
                continue

            # Check that the inner gates (v0, v1) are only used by this gate
            # to avoid increasing the graph size
            v0_used = False
            v1_used = False
            for check_var, (cr0, cr1) in aig.and_gates.items():
                if check_var == var:
                    continue
                if lit_to_var(cr0) == v0 or lit_to_var(cr1) == v0:
                    v0_used = True
                if lit_to_var(cr0) == v1 or lit_to_var(cr1) == v1:
                    v1_used = True
            # Also check outputs
            for o in aig.outputs:
                if lit_to_var(o) == v0:
                    v0_used = True
                if lit_to_var(o) == v1:
                    v1_used = True

            if v0_used or v1_used:
                continue

            # Rewrite: var = shared AND (other0 AND other1)
            # Reuse v0 as the inner gate: v0 = other0 AND other1
            # var = shared AND v0
            aig.and_gates[v0] = (min(other0, other1), max(other0, other1))
            aig.and_gates[var] = (shared, make_lit(v0))
            # Remove v1
            del aig.and_gates[v1]
            changed = True
            break  # restart after modification

    return aig


def dag_rewrite_pass(aig: AIG) -> AIG:
    """DAG-aware rewriting pass (wrapper)."""
    from .rewriter import dag_rewrite
    return dag_rewrite(aig, iterations=10, max_cut_size=5)


def functional_reduction_pass(aig: AIG) -> AIG:
    """Simulation-based functional equivalence detection and merging.

    Iterates until no more merges are found, since merging nodes can
    expose new equivalences (e.g., a gate becomes constant after its
    input is merged with another node).
    """
    from .fraig import functional_reduction
    for _ in range(20):  # safety limit
        prev = aig.num_ands()
        aig = functional_reduction(aig)
        aig = constant_propagation(aig)
        aig = structural_hashing(aig)
        aig = dead_node_elimination(aig)
        if aig.num_ands() >= prev:
            break
    return aig


def resubstitution_pass(aig: AIG) -> AIG:
    """Simulation-guided resubstitution: express nodes as functions of other existing nodes."""
    from .resub import resubstitution
    return resubstitution(aig)


def balance_pass(aig: AIG) -> AIG:
    """Balance the AIG by restructuring AND chains into balanced trees."""
    from .balance import balance
    return balance(aig)


def multioutput_resynth_pass(aig: AIG) -> AIG:
    """Multi-output resynthesis: find gate sharing across outputs."""
    from .multioutput import multioutput_resynth
    return multioutput_resynth(aig)


DEFAULT_PASSES = [
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
    # Functional reduction (catches equivalences structural passes miss)
    functional_reduction_pass,
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
    simple_rewrite,
    # Cleanup after simple rewriting
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
    # DAG-aware rewriting
    dag_rewrite_pass,
    # Resubstitution (express nodes as functions of other existing nodes)
    resubstitution_pass,
    # Post-rewrite functional reduction (rewriting may expose new equivalences)
    functional_reduction_pass,
    # Final cleanup
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
]

# Extended pipeline with balance-rewrite cycles (opt-in via --balance)
BALANCE_PASSES = [
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
    functional_reduction_pass,
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
    simple_rewrite,
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
    # Balance before rewriting (exposes different cut structures)
    balance_pass,
    structural_hashing,
    dead_node_elimination,
    dag_rewrite_pass,
    # Balance after rewriting (minimize depth of rewritten circuit)
    balance_pass,
    structural_hashing,
    dead_node_elimination,
    # Rewrite again on balanced structure (may find new savings)
    dag_rewrite_pass,
    functional_reduction_pass,
    constant_propagation,
    structural_hashing,
    dead_node_elimination,
]


def _stochastic_optimize(aig: AIG, restarts: int, balance: bool, multioutput: bool) -> AIG:
    """Multi-restart stochastic optimization.

    Each restart uses a randomly selected "script" — a different ordering
    of optimization passes (rewrite, resub, balance, fraig). This explores
    different parts of the optimization space. Perturbed rewriting uses
    random node ordering and cut selection. Resub allows new gate creation
    (safe because we track the best result and discard regressions).
    """
    import random as _random
    from .rewriter import dag_rewrite
    from .balance import balance as balance_fn
    from .resub import resubstitution

    # First run the deterministic pipeline to get a baseline
    base_passes = list(BALANCE_PASSES if balance else DEFAULT_PASSES)
    if multioutput:
        base_passes[-3:-3] = [multioutput_resynth_pass]
    best_aig = aig.copy()
    for p in base_passes:
        best_aig = p(best_aig)
    best_gates = best_aig.num_ands()

    # Prepare: run everything up to (not including) dag_rewrite
    prep_passes = [
        constant_propagation, structural_hashing, dead_node_elimination,
        functional_reduction_pass,
        constant_propagation, structural_hashing, dead_node_elimination,
        simple_rewrite,
        constant_propagation, structural_hashing, dead_node_elimination,
    ]
    prepared = aig.copy()
    for p in prep_passes:
        prepared = p(prepared)

    cleanup = [constant_propagation, structural_hashing, dead_node_elimination]

    def do_cleanup(w):
        for p in cleanup:
            w = p(w)
        return w

    # Script templates: different orderings of (rewrite, resub, balance)
    scripts = [
        ["rewrite", "resub", "balance", "rewrite", "resub"],
        ["rewrite", "balance", "resub", "rewrite", "resub"],
        ["resub", "rewrite", "balance", "rewrite", "resub"],
        ["balance", "rewrite", "resub", "rewrite", "balance", "resub"],
        ["rewrite", "resub", "rewrite", "resub", "rewrite"],
        ["resub", "balance", "rewrite", "resub", "balance", "rewrite"],
    ]

    for restart in range(restarts):
        rng = _random.Random(restart)
        work = prepared.copy()

        # Pick a random script
        script = scripts[restart % len(scripts)]

        for step_idx, step in enumerate(script):
            perturbation = 0.6 * (0.8 ** step_idx)  # anneal over steps

            if step == "rewrite":
                work = dag_rewrite(work, iterations=10, max_cut_size=5,
                                   perturbation=perturbation,
                                   rng=_random.Random(restart * 100 + step_idx))
            elif step == "resub":
                work = resubstitution(work, max_resub=1, allow_new_gates=True,
                                      rng=_random.Random(restart * 200 + step_idx))
            elif step == "balance":
                work = balance_fn(work)

            work = do_cleanup(work)

        # Deterministic finishing pass
        work = dag_rewrite(work, iterations=10, max_cut_size=5)
        work = resubstitution(work, max_resub=1, allow_new_gates=False)
        work = functional_reduction_pass(work)
        work = do_cleanup(work)

        if multioutput:
            work = multioutput_resynth_pass(work)
            work = do_cleanup(work)

        if work.num_ands() < best_gates:
            best_gates = work.num_ands()
            best_aig = work

    return best_aig


def optimize(
    aig: AIG,
    passes: list | None = None,
    balance: bool = False,
    multioutput: bool = False,
    stochastic: int = 0,
) -> AIG:
    """Run optimization passes on the AIG.

    Args:
        aig: The AIG to optimize (modified in place)
        passes: Optional list of pass functions. Overrides all flags.
        balance: If True, use balance-rewrite-balance-rewrite cycle (slower,
            helps circuits with deep AND chains).
        multioutput: If True, include multi-output resynthesis pass (slower,
            helps small circuits with few-input output groups).
        stochastic: Number of random restarts (0 = off). Each restart uses
            perturbed rewriting with annealing temperature, interleaved with
            balance passes. Tracks the best result across all restarts.

    Returns:
        The optimized AIG
    """
    if passes is not None:
        for pass_fn in passes:
            aig = pass_fn(aig)
        return aig

    if stochastic > 0:
        return _stochastic_optimize(aig, stochastic, balance, multioutput)

    pipeline = list(BALANCE_PASSES if balance else DEFAULT_PASSES)
    if multioutput:
        pipeline[-3:-3] = [multioutput_resynth_pass]
    for pass_fn in pipeline:
        aig = pass_fn(aig)
    return aig
