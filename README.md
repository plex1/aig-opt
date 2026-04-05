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
    -> dag_rewrite (10 iterations, k=5 cuts, NPN + multi-decomposition)
    -> functional_reduction (iterative)
    -> constant_propagation -> structural_hashing -> dead_node_elimination
```

Functional reduction runs both early (to reduce the circuit before expensive DAG rewriting) and late (to catch equivalences exposed by rewriting).

## Usage

```bash
# Optimize a circuit
python -m aig_opt input.aag -o output.aag --stats

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

## License

LGPL-3.0-or-later
