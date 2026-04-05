"""NPN canonicalization, precomputed optimal gate counts, and multi-decomposition synthesis.

NPN (Negation-Permutation-Negation) equivalence groups Boolean functions that
differ only in input negations, input permutations, and output negation.
For k=4 there are 222 classes; for k=5 there are ~617k classes.

By precomputing the optimal AND-gate count for each NPN class, we can quickly
decide whether a subgraph is worth resynthesizing, and by trying all k!
variable orderings we find better implementations than single-heuristic
Shannon decomposition.
"""

from __future__ import annotations

from itertools import permutations

from .aig import CONST_FALSE, CONST_TRUE, make_lit, negate


# ---------------------------------------------------------------------------
# Truth table manipulation (optimized with bitwise ops)
# ---------------------------------------------------------------------------

def _tt_mask(n: int) -> int:
    return (1 << (1 << n)) - 1 if n > 0 else 1


def negate_input_tt(tt: int, var_idx: int, n: int) -> int:
    """Negate input variable var_idx by swapping 2^var_idx-sized bit groups."""
    step = 1 << var_idx
    mask = _tt_mask(n)
    tt &= mask
    # Build low-group mask: bits where bit var_idx of position = 0
    lo_mask = 0
    for i in range(0, 1 << n, 2 * step):
        lo_mask |= ((1 << step) - 1) << i
    hi_mask = lo_mask << step
    return ((tt & lo_mask) << step) | ((tt & hi_mask) >> step)


# Precompute swap masks per (var_idx, n) for fast negation
_SWAP_MASKS: dict[tuple[int, int], tuple[int, int, int]] = {}

for _n in range(1, 6):
    for _v in range(_n):
        _step = 1 << _v
        _lo = 0
        for _i in range(0, 1 << _n, 2 * _step):
            _lo |= ((1 << _step) - 1) << _i
        _SWAP_MASKS[(_v, _n)] = (_lo, _lo << _step, _step)


def _negate_input_fast(tt: int, var_idx: int, n: int) -> int:
    """Fast input negation using precomputed masks."""
    lo, hi, step = _SWAP_MASKS[(var_idx, n)]
    return ((tt & lo) << step) | ((tt & hi) >> step)


# Precompute permutation index maps for n=1..5
# For each (n, perm), store a mapping: old_index -> new_index
_PERM_MAPS: dict[tuple[int, tuple[int, ...]], tuple[int, ...]] = {}


def _build_perm_map(n: int, perm: tuple[int, ...]) -> tuple[int, ...]:
    """Build index mapping for a permutation: result[old_idx] = new_idx."""
    mapping = []
    for i in range(1 << n):
        j = 0
        for p in range(n):
            if (i >> perm[p]) & 1:
                j |= (1 << p)
        mapping.append(j)
    return tuple(mapping)


def _precompute_perm_maps() -> None:
    for n in range(1, 6):
        for perm in permutations(range(n)):
            _PERM_MAPS[(n, perm)] = _build_perm_map(n, perm)


_precompute_perm_maps()


def permute_tt(tt: int, perm: tuple[int, ...], n: int) -> int:
    """Permute input variables using precomputed index mapping."""
    mapping = _PERM_MAPS.get((n, perm))
    if mapping is None:
        mapping = _build_perm_map(n, perm)
    result = 0
    while tt:
        i = (tt & -tt).bit_length() - 1  # lowest set bit
        result |= 1 << mapping[i]
        tt &= tt - 1  # clear lowest set bit
    return result


# ---------------------------------------------------------------------------
# NPN canonical form
# ---------------------------------------------------------------------------

def npn_canonical(tt: int, n: int) -> int:
    """Compute NPN canonical form of an n-input truth table.

    Returns the smallest truth table under all NPN transforms.
    For n=4: 768 transforms, n=5: 7680 transforms.
    """
    mask = _tt_mask(n)
    tt &= mask
    best = tt

    # For each permutation, try all input negations and both output polarities
    for perm in permutations(range(n)):
        ptt = permute_tt(tt, perm, n)
        # Try all 2^n input negation patterns
        for neg_mask in range(1 << n):
            ntt = ptt
            for bit in range(n):
                if (neg_mask >> bit) & 1:
                    ntt = _negate_input_fast(ntt, bit, n)
            if ntt < best:
                best = ntt
            comp = (ntt ^ mask) & mask
            if comp < best:
                best = comp

    return best


# Faster NPN for n=4: precompute all canonical forms at once
def _precompute_npn4_classes() -> dict[int, int]:
    """Assign each of 65536 4-input TTs to its NPN canonical form.

    Returns: tt -> canonical_tt mapping for all 65536 TTs.
    Uses orbit enumeration: for each unvisited TT, generate all NPN
    equivalents and mark them with the smallest (canonical) form.
    """
    n = 4
    mask = _tt_mask(n)
    canon_map: dict[int, int] = {}

    perms = list(permutations(range(n)))

    for seed in range(1 << 16):
        if seed in canon_map:
            continue

        # Generate entire NPN orbit from seed
        orbit: set[int] = set()
        for perm in perms:
            ptt = permute_tt(seed, perm, n)
            for neg_mask in range(1 << n):
                ntt = ptt
                for bit in range(n):
                    if (neg_mask >> bit) & 1:
                        ntt = _negate_input_fast(ntt, bit, n)
                orbit.add(ntt & mask)
                orbit.add((ntt ^ mask) & mask)

        canonical = min(orbit)
        for member in orbit:
            canon_map[member] = canonical

    return canon_map


# ---------------------------------------------------------------------------
# Multi-decomposition synthesis
# ---------------------------------------------------------------------------

def synthesize_optimal(
    tt: int,
    num_inputs: int,
    leaf_lits: list[int],
    existing_hash: dict[tuple[int, int], int],
    next_var: int,
) -> tuple[int, dict[int, tuple[int, int]], int]:
    """Try all num_inputs! decomposition orderings, return the best result.

    Returns (result_lit, new_gates_dict, new_next_var).
    k=4: 24 trials, k=5: 120 trials — both fast.
    """
    from .rewriter import SynthesisContext, synthesize_tt

    best_lit = CONST_FALSE
    best_gates: dict[int, tuple[int, int]] = {}
    best_next_var = next_var
    best_count = float('inf')

    for perm in permutations(range(num_inputs)):
        perm_tt = permute_tt(tt, perm, num_inputs)
        perm_lits = [leaf_lits[perm[i]] for i in range(num_inputs)]

        ctx = SynthesisContext(existing_hash, next_var)
        result_lit = synthesize_tt(perm_tt, num_inputs, perm_lits, ctx)

        if ctx.num_new_gates < best_count:
            best_count = ctx.num_new_gates
            best_lit = result_lit
            best_gates = dict(ctx.new_gates)
            best_next_var = ctx.next_var

            if best_count == 0:
                break

    return best_lit, best_gates, best_next_var


# ---------------------------------------------------------------------------
# Precomputed k=4 optimal gate counts
# ---------------------------------------------------------------------------

def _synthesize_and_count(tt: int, n: int) -> int:
    """Synthesize a truth table with multi-decomposition and return min gate count."""
    from .rewriter import SynthesisContext, synthesize_tt

    leaf_lits = [make_lit(i + 1) for i in range(n)]
    empty_hash: dict[tuple[int, int], int] = {}
    next_var = n + 1

    best_count = float('inf')
    for perm in permutations(range(n)):
        perm_tt = permute_tt(tt, perm, n)
        perm_lits = [leaf_lits[perm[i]] for i in range(n)]
        ctx = SynthesisContext(empty_hash, next_var)
        synthesize_tt(perm_tt, n, perm_lits, ctx)
        if ctx.num_new_gates < best_count:
            best_count = ctx.num_new_gates
            if best_count == 0:
                break
    return best_count


_NPN4_CANON_MAP: dict[int, int] = {}   # tt -> canonical_tt (all 65536 entries)
_NPN4_OPTIMAL: dict[int, int] = {}      # canonical_tt -> min AND gates (222 entries)


def _precompute_npn4() -> None:
    """Build the k=4 NPN lookup table."""
    global _NPN4_CANON_MAP, _NPN4_OPTIMAL

    _NPN4_CANON_MAP = _precompute_npn4_classes()

    # Find unique canonical forms
    unique_canons = set(_NPN4_CANON_MAP.values())

    # Synthesize optimal gate count for each
    for canon in unique_canons:
        _NPN4_OPTIMAL[canon] = _synthesize_and_count(canon, 4)


# k=5 on-the-fly cache
_NPN5_CACHE: dict[int, int] = {}


def get_optimal_gate_count(tt: int, n: int) -> int | None:
    """Look up the optimal AND-gate count for an n-input truth table.

    For n <= 3: synthesize directly (trivial).
    For n == 4: uses precomputed _NPN4_OPTIMAL table.
    For n == 5: computes NPN canonical, checks cache, synthesizes on miss.
    For n > 5: returns None.
    """
    if n <= 0:
        return 0

    mask = _tt_mask(n)
    tt &= mask
    if tt == 0 or tt == mask:
        return 0

    if n <= 3:
        return _synthesize_and_count(tt, n)

    if n == 4:
        if not _NPN4_OPTIMAL:
            _precompute_npn4()
        canon = _NPN4_CANON_MAP.get(tt)
        if canon is None:
            canon = npn_canonical(tt, 4)
        return _NPN4_OPTIMAL.get(canon)

    if n == 5:
        canon = npn_canonical(tt, 5)
        if canon in _NPN5_CACHE:
            return _NPN5_CACHE[canon]
        cost = _synthesize_and_count(canon, 5)
        _NPN5_CACHE[canon] = cost
        return cost

    return None


# Precompute k=4 at module import
_precompute_npn4()
