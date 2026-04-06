# aig-opt

A pure Python AIG (And-Inverter Graph) optimizer that reads/writes AIGER ASCII (.aag) files. Implements a full optimization pipeline including simulation-based functional reduction, NPN-guided DAG-aware rewriting, and multi-decomposition synthesis. Benchmarks against Yosys and Berkeley ABC.

See [BENCHMARKS.md](BENCHMARKS.md) for full results. On a suite of 15 circuits, aig-opt **beats Yosys on 12/15** and **matches ABC &deepsyn on 8/15** — in pure Python with no compiled dependencies.

## Optimization Passes

The optimizer runs a pipeline of passes, each targeting a different class of redundancy. Passes are applied in a specific order and repeated where beneficial.

### 1. Constant Propagation

Detects and simplifies AND gates with constant or trivially determined inputs:

- **Zero annihilation**: `0 AND x = 0` — if either input is constant false, the gate output is false regardless of the other input.
- **Identity elimination**: `1 AND x = x` — if either input is constant true, the gate reduces to its other input.
- **Idempotence**: `x AND x = x` — a signal AND'd with itself is just itself.
- **Complementary contradiction**: `x AND NOT(x) = 0` — a signal AND'd with its negation is always false.

These simplifications are applied iteratively until no more changes occur, since simplifying one gate can make others constant.

### 2. Structural Hashing

Merges AND gates that have identical inputs (possibly in different order). Since `a AND b = b AND a`, gates are normalized so the smaller literal comes first. Any two gates with the same normalized input pair are merged into a single gate, with all references redirected.

### 3. Dead Node Elimination

Removes gates that are unreachable from any output. Starting from the primary outputs and latch next-states, a backward traversal marks all gates that contribute to at least one output. Unmarked gates are deleted, reducing circuit size without affecting functionality.

### 4. Simple Rewriting

Local two-level algebraic factoring. Detects patterns where two AND gates share an input and their outputs are AND'd together:

```
(a AND b) AND (a AND c)  ->  a AND (b AND c)
```

This saves one AND gate by factoring out the shared input `a`. The pattern is detected by examining all pairs of gates feeding into a common gate.

### 5. Simulation-Based Functional Reduction (FRAIG Sweeping)

This is the most powerful pass for circuits with deep functional redundancy. Many circuits contain nodes that compute the same Boolean function despite having completely different structure — structural hashing cannot detect these.

**How it works:**

1. **Random simulation**: All nodes are simulated in parallel using random 64-bit input vectors. Each node gets a 64-bit "signature" representing its output across 64 random input combinations.

2. **Candidate grouping**: Nodes with identical signatures (or complementary signatures, where one is the bitwise NOT of the other) are grouped as equivalence candidates. Constant nodes appear as candidates equivalent to the constant-false node.

3. **Batched verification**: All candidate pairs are verified in a single pass:
   - For circuits with up to 20 inputs: exhaustive enumeration of all 2^n input patterns, packed 64 at a time into 64-bit words for parallel evaluation.
   - For larger circuits: statistical verification with 4096 random patterns.

4. **Merging**: Verified equivalent nodes are merged by literal substitution — all references to the redundant node are replaced with the representative node (possibly negated for complementary pairs).

5. **Iteration**: The pass repeats until convergence, since merging nodes can expose new equivalences (e.g., a gate may become constant after its input is merged with another node).

This pass alone reduces `rand_deep_xlarge` from 1000 gates to 5, matching ABC's SAT-based deep synthesis.

### 6. DAG-Aware Rewriting with NPN-Guided Multi-Decomposition

The core optimization engine, based on the approach from Mishchenko et al. (DAC 2006). For each node in the circuit, it considers replacing the node's local subgraph with a potentially smaller implementation.

**Cut enumeration**: For each node, all k-feasible cuts are enumerated (up to k=5 inputs). A cut is a set of "leaf" nodes such that every path from any primary input to the target node passes through at least one leaf. Each cut defines a small Boolean function.

**Truth table computation**: For each cut, the Boolean function from leaves to root is computed as a packed truth table integer. For a 5-input cut, this is a 32-bit integer encoding all 32 output values.

**NPN-guided pruning**: Before attempting resynthesis, the truth table's NPN canonical form is looked up in a precomputed table to determine the minimum possible AND-gate count. If the current subgraph already uses that many gates (or fewer), the cut is skipped. For 4-input functions, all 222 NPN equivalence classes are precomputed at module load time (~0.5s). For 5-input functions, results are computed on demand and cached.

**NPN canonicalization** groups Boolean functions that differ only in:
- **N**: input negations (complementing any subset of inputs)
- **P**: input permutations (reordering inputs)
- **N**: output negation (complementing the output)

This reduces the 65,536 possible 4-input functions to just 222 equivalence classes.

**Multi-decomposition synthesis**: For each promising cut, the function is resynthesized using Shannon decomposition. The key insight is that different variable orderings produce different decomposition trees with different gate counts. The engine tries all k! orderings (24 for k=4, 120 for k=5) and picks the one producing fewest AND gates.

Shannon decomposition expresses any function as:

```
f = (x AND f|x=1) OR (NOT(x) AND f|x=0)
```

where `f|x=1` and `f|x=0` are the positive and negative cofactors. Special cases (constant, single variable, AND, OR, XOR, MUX patterns) are detected and synthesized directly without decomposition.

**Verification and replacement**: Each candidate replacement is verified against the original truth table before being accepted. If the new implementation uses fewer AND gates than the existing subgraph (accounting for shared nodes via DAG-aware cost), the replacement is applied.

The entire rewrite loop runs for up to 10 iterations, with cuts recomputed after each pass of replacements.

## Pipeline Order

```
constant_propagation -> structural_hashing -> dead_node_elimination
    -> functional_reduction (iterative: fraig + cleanup until convergence)
    -> constant_propagation -> structural_hashing -> dead_node_elimination
    -> simple_rewrite
    -> constant_propagation -> structural_hashing -> dead_node_elimination
    -> [--balance: balance -> cleanup -> rewrite -> balance -> cleanup ->] rewrite
    -> dag_rewrite (10 iterations, k=5 cuts, NPN + multi-decomposition)
    -> [--multioutput: multi-output exact synthesis]
    -> functional_reduction (iterative)
    -> constant_propagation -> structural_hashing -> dead_node_elimination
```

Steps in `[brackets]` are optional and enabled via CLI flags.

Functional reduction runs both early (to reduce the circuit before expensive DAG rewriting) and late (to catch equivalences exposed by rewriting).

### 7. Multi-Output Exact Synthesis (optional, `--multioutput`)

This pass is **off by default** because it only helps circuits with small output groups (≤5 shared inputs) and adds significant runtime for larger circuits where it cannot find improvements.

When enabled, it groups outputs that share inputs and jointly resynthesizes them. Two strategies are tried:

- **Exhaustive synthesis** (Strategy B): for small groups (≤5 combined inputs, ≤3 outputs), enumerates all possible AND-gate networks bottom-up using iterative-deepening DFS. Each signal carries a precomputed truth table integer, so evaluating a new gate is a single bitwise AND. This finds provably optimal implementations — for example, the 3-gate half adder where `AND(a,b)` serves as both the carry output and part of the XOR computation.

- **Shared-context synthesis** (Strategy A): synthesizes multiple outputs sequentially in a single `SynthesisContext`, so structural hashing naturally reuses gates created for earlier outputs. Tries all output orderings and input permutations.

**Why it's off by default**: the exhaustive search is only tractable for output groups with few combined inputs (≤5). For the 4-bit multipliers (8 shared inputs per output pair), the search space is too large and no improvement is found. The pass adds 0.2-10s per output group examined. Enable it with `--multioutput` when optimizing circuits with small multi-output subcircuits (adders, comparators, small ALUs).

**Result when enabled**: half_adder achieves the optimal 3-gate implementation (vs 4 gates default), matching ABC `&deepsyn`.

### 8. AIG Balancing (optional, `--balance`)

This pass is **off by default** because it doubles runtime for marginal gains on most circuits.

When enabled, it restructures AND chains into balanced binary trees to minimize circuit depth. For example, `((a AND b) AND c) AND d` (depth 3) becomes `(a AND b) AND (c AND d)` (depth 2) with the same number of gates. Leaves are sorted by depth so shallowest are paired first, reducing the critical path.

The key benefit is not the depth reduction itself but its interaction with DAG rewriting: balancing exposes different cut structures, and the pipeline runs `balance -> rewrite -> balance -> rewrite` to break convergence plateaus where rewriting alone gets stuck.

**Why it's off by default**: the extra rewrite pass roughly doubles runtime (e.g., 14s vs 8s on multipliers). Only one benchmark circuit improved: rand_deep_large 10→8 gates. Enable it with `--balance` for circuits with deep AND chains.

## Usage

```bash
# Optimize a circuit
python -m aig_opt input.aag -o output.aag --stats

# Enable balance-rewrite cycles (slower, breaks convergence on deep circuits)
python -m aig_opt input.aag -o output.aag --balance --stats

# Enable multi-output optimization (slower, finds cross-output gate sharing)
python -m aig_opt input.aag -o output.aag --multioutput --stats

# Enable both optional passes
python -m aig_opt input.aag -o output.aag --balance --multioutput --stats

# Run benchmarks (requires: pip install pyosys)
python benchmarks/benchmark.py

# Generate random test circuits
python benchmarks/generate_circuits.py

# Run tests
python -m pytest tests/ -v
```

## Project Structure

```
src/aig_opt/
  aig.py           # AIG data structure and helpers
  aiger.py         # .aag parser and writer
  optimizer.py     # Optimization pass orchestration
  rewriter.py      # DAG-aware rewriting engine (cuts, synthesis, verification)
  npn.py           # NPN canonicalization, precomputed tables, multi-decomposition
  fraig.py         # Simulation-based functional reduction
  cli.py           # CLI entry point
benchmarks/
  benchmark.py     # Three-way comparison: aig-opt vs Yosys vs ABC &deepsyn
  generate_circuits.py
  circuits/        # Sample .aag files
tests/
  test_aiger.py
  test_optimizer.py
```

## Requirements

- Python >= 3.10 (no dependencies for core optimizer)
- `pyosys` (optional, for Yosys and ABC benchmarking): `pip install pyosys`
- `pytest` (for tests): `pip install pytest`

## Known Limitations and Future Work

The optimizer matches ABC `&deepsyn` on 8/15 benchmark circuits. The remaining gaps fall into two categories:

### Cross-output gate sharing is limited to small output groups

The optional `--multioutput` pass finds cross-output gate sharing via exhaustive exact synthesis, but it is only tractable for output groups with ≤5 combined inputs. It solves the half_adder (4→3 gates) but cannot help the full_adder (3 inputs but 9 gates requires depth-8 search, too slow in Python) or the multipliers (8 shared inputs per output pair).

**Affected circuits**: full_adder (9 vs ABC's 7), mul4_unsigned (104 vs 82), mul4_signed (106 vs 83).

**Possible fix**: SAT-based exact synthesis instead of brute-force enumeration, or a C extension for the inner search loop.

### k=5 cut window is too small for large circuits

The rewriter uses cuts of up to 5 inputs. For 32-input circuits, this means each rewrite only sees a small fraction of the logic at once and cannot perform the large-scale restructuring that ABC achieves.

**Affected circuits**: rand_xlarge_clean (104 vs ABC's 94), rand_xlarge_redund (89 vs 81).

**Fix**: larger cut sizes (k=6+), window-based rewriting that considers larger subgraphs, or global restructuring passes like BDD-based resynthesis.

## License

LGPL-3.0-or-later
