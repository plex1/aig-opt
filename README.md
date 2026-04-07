# aig-opt

A pure Python AIG (And-Inverter Graph) optimizer that reads/writes AIGER ASCII (.aag) files. Implements a full optimization pipeline including simulation-based functional reduction, NPN-guided DAG-aware rewriting, and multi-decomposition synthesis. Benchmarks against Yosys and Berkeley ABC.

See [BENCHMARKS.md](BENCHMARKS.md) for full results. On a suite of 17 circuits, aig-opt **beats Yosys on 14/17** and **matches ABC &deepsyn on 9/17** — in pure Python with no compiled dependencies.

## Getting Started

### Prerequisites

- Python >= 3.10

### Clone and install

```bash
git clone https://github.com/plex1/testrepo.git
cd testrepo
```

**Option A: using uv (recommended)**

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
uv pip install pytest          # for tests
uv pip install pyosys          # optional, for Yosys/ABC benchmarks
```

**Option B: using pip**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest             # for tests
pip install pyosys             # optional, for Yosys/ABC benchmarks
```

### Quick examples

```bash
# Optimize a circuit and print statistics
python -m aig_opt benchmarks/circuits/half_adder.aag --stats

# Optimize and write output to a file
python -m aig_opt benchmarks/circuits/mul4_unsigned.aag -o optimized.aag --stats

# Stochastic multi-restart (auto-parallelized across all CPU cores)
python -m aig_opt benchmarks/circuits/mul4_unsigned.aag --stochastic 16 --stats

# Run the test suite
python -m pytest tests/ -v

# Run three-way benchmarks against Yosys and ABC (requires pyosys)
python benchmarks/benchmark.py
```

See [Usage](#usage) below for all CLI flags and options.

## Overview

### Optimization Passes

- [Constant propagation](#1-constant-propagation) (`src/aig_opt/optimizer.py`): Simplifies trivial patterns (`0 AND x`, `x AND x`, `x AND !x`)
- [Structural hashing](#2-structural-hashing) (`src/aig_opt/optimizer.py`): Merges duplicate AND gates with identical inputs
- [Dead node elimination](#3-dead-node-elimination) (`src/aig_opt/optimizer.py`): Removes unreachable gates
- [Simple rewriting](#4-simple-rewriting) (`src/aig_opt/optimizer.py`): Local two-level algebraic factoring of shared inputs
- [Simulation-based functional reduction](#5-simulation-based-functional-reduction-fraig-sweeping) (`src/aig_opt/fraig.py`): Detects functionally equivalent nodes via random simulation and exhaustive verification
- [DAG-aware rewriting](#6-dag-aware-rewriting-with-npn-guided-multi-decomposition) (`src/aig_opt/rewriter.py`): Enumerates k-feasible cuts, computes truth tables, and resynthesizes optimal implementations using NPN canonicalization
- [Resubstitution](#7-resubstitution) (`src/aig_opt/resub.py`): Replaces gates with simpler combinations of existing nodes
- [Multi-output exact synthesis](#8-multi-output-exact-synthesis-optional---multioutput) (`src/aig_opt/multioutput.py`): Groups outputs by shared inputs for joint resynthesis *(optional)*
- [AIG balancing](#9-aig-balancing-optional---balance) (`src/aig_opt/balance.py`): Restructures AND chains into balanced trees to reduce depth *(optional)*
- [Decompression](#10-decompression) (`src/aig_opt/decompress.py`): Intentionally restructures circuits to escape local minima *(used in stochastic mode)*
- [Stochastic multi-restart optimization](#11-stochastic-multi-restart-optimization-optional---stochastic-n) (`src/aig_opt/optimizer.py`): Parallel randomized decompress-compress cycles *(optional)*

### Advanced Techniques

- [NPN canonicalization](#6-dag-aware-rewriting-with-npn-guided-multi-decomposition) (`src/aig_opt/npn.py`): Groups Boolean functions by NPN equivalence with precomputed optimal gate counts for k=4 and k=5
- [Multi-decomposition synthesis](#6-dag-aware-rewriting-with-npn-guided-multi-decomposition) (`src/aig_opt/rewriter.py`): Tries all k! variable orderings for Shannon decomposition to find the smallest implementation
- [Precomputed networks](#6-dag-aware-rewriting-with-npn-guided-multi-decomposition) (`src/aig_opt/npn4_networks.json`): Provably optimal AND-gate implementations for all 222 NPN classes of 4-input functions

## Optimization Passes (Detail)

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

**Synthesis** has two tiers:

1. **Precomputed exact networks** (4-input cuts only): For 24 of the 222 NPN classes, exhaustive enumeration found provably optimal AND-gate networks that are smaller than any Shannon decomposition. These are stored in `npn4_networks.json` and loaded at startup. Some improvements are dramatic: 9→3 gates (4-input XOR), 8→3, 7→4. When a cut's truth table maps to one of these classes (via NPN transform), the precomputed network is instantiated directly.

2. **Multi-decomposition Shannon synthesis** (fallback): For the remaining NPN classes and all 5-input cuts, Shannon decomposition is used. Different variable orderings produce different decomposition trees with different gate counts. The engine tries all k! orderings (24 for k=4, 120 for k=5) and picks the one producing fewest AND gates.

Shannon decomposition expresses any function as:

```
f = (x AND f|x=1) OR (NOT(x) AND f|x=0)
```

where `f|x=1` and `f|x=0` are the positive and negative cofactors. Special cases (constant, single variable, AND, OR, XOR, MUX patterns) are detected and synthesized directly without decomposition.

**Limitation of Shannon decomposition**: It always produces MUX trees — it cannot discover arbitrary gate topologies with reconvergent paths or shared internal fanout. For 198 of the 222 NPN4 classes, Shannon decomposition (across all 24 variable orderings) already produces the optimal result. For the remaining 24 classes, the precomputed exact networks close the gap.

**Verification and replacement**: Each candidate replacement is verified against the original truth table before being accepted. If the new implementation uses fewer AND gates than the existing subgraph (accounting for shared nodes via DAG-aware cost), the replacement is applied.

The entire rewrite loop runs for up to 10 iterations, with cuts recomputed after each pass of replacements.

### 7. Resubstitution

Resubstitution is a fundamentally different optimization from cut-based rewriting. Where rewriting asks "can I build a better circuit for this function from primary inputs?", resubstitution asks "can I express this node as a simple function of **other existing nodes** already in the circuit?"

**Why rewriting isn't enough**: After DAG-aware rewriting, the multiplier has 104 gates. Only 1 of ~2800 cuts has subgraph cost above the NPN-optimal minimum — the rewriter has squeezed every per-cut opportunity dry. But 64 of those 104 gates have fanout=1 (used exactly once). These are candidates for removal if their function can be expressed using other signals that already exist in the circuit.

**How it works**: For each gate X computing some function f:

1. **Collect divisors**: gather other nodes in X's neighborhood — nodes in its fanin cone and nearby nodes at similar topological depth. These are the "building blocks" available for resubstitution.

2. **Simulate**: compute 64-bit random simulation signatures for X and all divisors. This enables fast candidate filtering:
   - **0-resub**: does any divisor's signature match X? (X is redundant — equivalent to an existing node)
   - **1-resub**: do any two divisors d_i, d_j satisfy `sig_X == sig_i AND sig_j`? (X = d_i AND d_j — replace with one gate)
   - **2-resub**: can X be expressed as `(d_i OP d_j) OP d_k`? (replace with two gates)

3. **Verify**: confirm simulation candidates with exhaustive checking.

4. **Replace**: substitute X with the simpler expression. Dead node elimination removes X's now-unreferenced subgraph.

**Why it helps multipliers**: Multipliers have dense gate sharing — partial products and carry chains create many signals that are simple functions of each other. A carry bit might equal `existing_node_A AND existing_node_B`, but the rewriter can't see this because it only looks at cuts rooted at that node. Resubstitution checks all pairs of existing nodes, finding relationships invisible to per-cut analysis.

This is equivalent to ABC's `resub` command, one of the most effective passes for arithmetic circuits.

## Pipeline Order

Default pipeline:
```
constant_propagation -> structural_hashing -> dead_node_elimination
    -> functional_reduction (iterative)
    -> simple_rewrite -> cleanup
    -> dag_rewrite (10 iterations, k=5 cuts, NPN + multi-decomposition)
    -> resubstitution (simulation-guided, 0/1/2-resub)
    -> functional_reduction (iterative)
    -> final cleanup
```

With `--balance`:
```
    ... -> simple_rewrite -> cleanup
    -> balance -> cleanup -> dag_rewrite
    -> balance -> cleanup -> dag_rewrite    (second rewrite on balanced structure)
    -> functional_reduction -> final cleanup
```

With `--multioutput`, multi-output exact synthesis runs after the last rewrite pass.

Functional reduction runs both early (to reduce the circuit before expensive DAG rewriting) and late (to catch equivalences exposed by rewriting).

### 8. Multi-Output Exact Synthesis (optional, `--multioutput`)

This pass is **off by default** because it only helps circuits with small output groups (≤5 shared inputs) and adds significant runtime for larger circuits where it cannot find improvements.

When enabled, it groups outputs that share inputs and jointly resynthesizes them. Two strategies are tried:

- **Exhaustive synthesis** (Strategy B): for small groups (≤5 combined inputs, ≤3 outputs), enumerates all possible AND-gate networks bottom-up using iterative-deepening DFS. Each signal carries a precomputed truth table integer, so evaluating a new gate is a single bitwise AND. This finds provably optimal implementations — for example, the 3-gate half adder where `AND(a,b)` serves as both the carry output and part of the XOR computation.

- **Shared-context synthesis** (Strategy A): synthesizes multiple outputs sequentially in a single `SynthesisContext`, so structural hashing naturally reuses gates created for earlier outputs. Tries all output orderings and input permutations.

**Why it's off by default**: the exhaustive search is only tractable for output groups with few combined inputs (≤5). For the 4-bit multipliers (8 shared inputs per output pair), the search space is too large and no improvement is found. The pass adds 0.2-10s per output group examined. Enable it with `--multioutput` when optimizing circuits with small multi-output subcircuits (adders, comparators, small ALUs).

**Result when enabled**: half_adder achieves the optimal 3-gate implementation (vs 4 gates default), matching ABC `&deepsyn`.

### 9. AIG Balancing (optional, `--balance`)

This pass is **off by default** because it doubles runtime for marginal gains on most circuits.

When enabled, it restructures AND chains into balanced binary trees to minimize circuit depth. For example, `((a AND b) AND c) AND d` (depth 3) becomes `(a AND b) AND (c AND d)` (depth 2) with the same number of gates. Leaves are sorted by depth so shallowest are paired first, reducing the critical path.

The key benefit is not the depth reduction itself but its interaction with DAG rewriting: balancing exposes different cut structures, and the pipeline runs `balance -> rewrite -> balance -> rewrite` to break convergence plateaus where rewriting alone gets stuck.

**Why it's off by default**: the extra rewrite pass roughly doubles runtime (e.g., 14s vs 8s on multipliers). Only one benchmark circuit improved: rand_deep_large 10→8 gates. Enable it with `--balance` for circuits with deep AND chains.

### 10. Decompression

All compression passes (rewriting, resubstitution, FRAIG) are greedy — they only accept moves that reduce gate count. This means they always converge to the same local minimum. To escape, we need to go *uphill* first — intentionally increase the gate count to reach a structurally different circuit, then compress back down to a potentially lower minimum. Three decompression strategies are implemented in `src/aig_opt/decompress.py`:

- **Truth-table resynthesis** (`resynthesize_from_truth_tables`): completely rebuilds the circuit from output truth tables with a random variable ordering. Creates a structurally unrelated circuit (typically 3-4x larger) as a fresh starting point. Most aggressive — the blown-up circuit needs many compression steps to get back down.

- **Subgraph perturbation** (`perturb_subgraphs`): randomly resynthesizes a fraction of nodes with non-optimal Shannon decompositions (random variable ordering instead of best). Increases gate count by 20-50% while preserving most of the circuit structure.

- **Algebraic rewrite** (`algebraic_rewrite`): applies algebraic identities to randomly selected gates that change circuit topology without changing function. Two transformations:
  - *Distributive expansion*: `AND(a, OR(b,c)) → OR(AND(a,b), AND(a,c))` — unfactors a shared input, creating two new AND gates. The reverse of the `simple_rewrite` factoring pass.
  - *Associative reshuffling*: `AND(AND(a,b), AND(c,d)) → AND(AND(a,c), AND(b,d))` — regroups inputs of an AND chain, creating different intermediate signals.

  This is the most effective decompression for arithmetic circuits: it increases gate count by only 25-35% but changes which signals are available for subsequent resubstitution and rewriting.

These passes are not used standalone — they are building blocks for the [stochastic multi-restart optimizer](#11-stochastic-multi-restart-optimization-optional---stochastic-n), which alternates decompression with compression in randomized scripts.

### 11. Stochastic Multi-Restart Optimization (optional, `--stochastic N`)

This mode is **off by default** because it multiplies runtime by the number of restarts.

The default optimization pipeline is fully deterministic and greedy — every pass only accepts moves that reduce gate count. This means it always converges to the same local minimum. Stochastic mode escapes these local minima by alternating [decompression](#10-decompression) with compression in randomized scripts across multiple parallel restarts.

**Perturbed compression**: Node processing order is shuffled, and with an annealing probability a random improving replacement is chosen instead of the greedy best. Cut sizes vary across steps (k=3, 4, 5) to expose different optimization opportunities. The best result found at any intermediate step across all restarts is saved.

**Parallel execution**: Restarts are fully independent and run in parallel across all available CPU cores using `multiprocessing.Pool`. With 4 cores, `--stochastic 16` takes roughly the same wall time as 4 sequential restarts. The parallelization is automatic — no configuration needed.

**Randomized scripts**: Each restart generates a unique "script" — a sequence of compression and decompression steps — from its seed via `_generate_script()`. Parameters are randomized per restart:

```
# Example generated scripts (each restart is different):
algebraic(0.3) -> rw(k=5,p=0.32) -> resub -> rw(k=4,p=0.18) -> algebraic(0.3) -> ...  (7 cycles)
algebraic(0.15) -> bal -> rw(k=5,p=0.41) -> resub -> rw(k=5,p=0.27) -> resub -> ...   (6 cycles)
algebraic(0.4) -> rw(k=3,p=0.15) -> resub -> rw(k=5,p=0.44) -> fraig -> ...            (5 cycles)
```

#### Search Parameters

The following parameters control search quality. All are randomized per restart to maximize trajectory diversity:

| Parameter | Range | Effect | Notes |
|---|---|---|---|
| `restarts` (N in --stochastic N) | User-specified | Number of independent attempts, run in parallel | Key knob — more restarts = more trajectory diversity |
| `n_cycles` | 5-8 per restart | Decompress-compress rounds per script | More cycles = more chances to find improvements |
| `n_compress` | 2-3 per cycle | Compression steps per cycle (rw + resub) | 3 is favored; more = more thorough compression |
| `algebraic frac` | 0.15-0.4 | Fraction of gates perturbed per decompression | 0.3 is most common; higher = more exploration, slower convergence |
| `max_cut_size` (k) | 3, 4, or 5 | Rewrite window size, randomized per step | k=5 favored; smaller k finds different optimizations |
| `perturbation` | 0.1-0.5 | Probability of choosing a random (not greedy-best) replacement | Anneals down over steps within a script |
| `dag_rewrite iterations` | 15 | Replacements per rewrite call | Higher than default (10) for more thorough compression |
| `use_balance` | 25% chance per restart | Whether to interleave balance passes | Helps deep AND chains; adds overhead |
| `fraig` | 15% chance per cycle | Whether to add functional reduction | Catches equivalences exposed by decompression |
| `max_resub` | 1 | Resubstitution complexity (0=equiv, 1=AND pair) | Each resub checks existing node pairs |

**Why randomized > hardcoded scripts**: Early versions used 8 hardcoded script templates with fixed k and perturbation values per step. This limited the search space — the optimizer could only explore 8 distinct trajectories regardless of restart count. With randomized generation, every restart explores a unique trajectory, and with enough restarts the optimizer finds the specific parameter combinations that work best for a given circuit.

**Why it's off by default**: each restart takes roughly as long as the default pipeline. With `--stochastic 16` on 4 cores, wall time is ~4x the default pipeline. Enable it for high-effort optimization where gate count matters more than runtime.

**Results when enabled** (verified correct): mul4_unsigned 124→**92** (`--stochastic 16`, 4 cores, 208s), mul4_signed 106→103, rand_deep_large 10→7. The algebraic decompression was the key breakthrough for the multiplier — it exposes fundamentally different circuit topologies that the compress passes can exploit.

**Empirical tuning**: The decompress/compress ratio was tuned via systematic experiment (`benchmarks/experiment_decompress.py`). Key findings on the 4-bit unsigned multiplier:

| Decompression | Fraction | Compress steps/cycle | Best gates (verified) |
|---|---|---|---|
| algebraic | 0.3 | 3 (5-7 cycles) | **92** |
| algebraic | 0.15 | 3 (4 cycles + bal) | 93 |
| algebraic | 0.2 | 3 (3 cycles) | 96 |
| perturb | 0.2-0.5 | 2-5 | 103-104 |

The sweet spot is light algebraic decompression (20-30% of gates, +25% size increase) followed by 2-3 compression steps (rewrite with varied k + resub), repeated for 5-8 cycles. Heavier decompression explores more but takes longer to compress back; lighter decompression doesn't change enough structure. Algebraic rewrite consistently outperforms subgraph perturbation because it creates structurally meaningful changes (redistributing inputs across gates) rather than random ones.

Note: the aggressive resub bug (creating gates before verification, causing self-referential simulation) has been fixed. Results are now verified via truth-table checking at every step.

## Usage

```bash
# Optimize a circuit
python -m aig_opt input.aag -o output.aag --stats

# Enable balance-rewrite cycles (slower, breaks convergence on deep circuits)
python -m aig_opt input.aag -o output.aag --balance --stats

# Enable multi-output optimization (slower, finds cross-output gate sharing)
python -m aig_opt input.aag -o output.aag --multioutput --stats

# Stochastic multi-restart (high-effort, N restarts, auto-parallelized across cores)
python -m aig_opt input.aag -o output.aag --stochastic 16 --stats

# Combine flags for maximum effort
python -m aig_opt input.aag -o output.aag --balance --multioutput --stochastic 16 --stats

# Run benchmarks (requires: pip install pyosys)
python benchmarks/benchmark.py

# Generate random test circuits
python benchmarks/generate_circuits.py

# Run decompression ratio experiment on a specific circuit
python benchmarks/experiment_decompress.py benchmarks/circuits/mul4_unsigned.aag

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
  npn.py           # NPN canonicalization, precomputed tables, synthesis dispatch
  npn4_networks.json # Precomputed exact gate networks for 24 NPN4 classes
  fraig.py         # Simulation-based functional reduction
  resub.py         # Simulation-guided resubstitution
  balance.py       # AIG balancing (AND chain -> balanced tree)
  decompress.py    # Decompression: resynth, subgraph perturbation, algebraic rewrite
  multioutput.py   # Multi-output exact synthesis
  cli.py           # CLI entry point
benchmarks/
  benchmark.py     # Three-way comparison: aig-opt vs Yosys vs ABC &deepsyn
  generate_circuits.py       # Random circuit generator
  generate_multipliers.py    # 4-bit multiplier generator (unsigned + signed)
  experiment_decompress.py   # Decompress/compress ratio experiment
  circuits/        # Sample .aag files (adders, multipliers, random circuits)
tests/
  test_aiger.py
  test_optimizer.py
```

## Requirements

- Python >= 3.10 (no dependencies for core optimizer)
- `pyosys` (optional, for Yosys and ABC benchmarking): `pip install pyosys`
- `pytest` (for tests): `pip install pytest`

## Known Limitations and Future Work

The optimizer matches ABC `&deepsyn` on 9/17 benchmark circuits. The remaining gaps fall into three categories:

### Shannon decomposition limits per-cut synthesis quality

The DAG rewriter's synthesis engine uses Shannon decomposition, which always builds MUX trees. It cannot discover arbitrary gate topologies with reconvergent paths or shared internal fanout. For 4-input functions, we mitigate this with precomputed exact networks (stored in `npn4_networks.json`) that cover 24 of 222 NPN classes where Shannon is suboptimal. For 5-input functions, Shannon decomposition remains the only option.

In practice, this matters less than expected: the DAG-aware cost model only counts gates exclusive to a subgraph (gates shared with other fanouts are "free"). On well-optimized circuits, most subgraphs have low exclusive cost that already matches the exact optimum. The precomputed networks provide a theoretical improvement but current benchmarks show no difference because the per-cut exclusive costs are already optimal.

**Affected circuits**: primarily multipliers and arithmetic circuits with dense gate sharing.

**Possible fix**: SAT-based exact synthesis for 5-input functions, or extending the exhaustive precomputation to cover more NPN classes with a C extension for speed.

### Cross-output gate sharing is limited to small output groups

The optional `--multioutput` pass finds cross-output gate sharing via exhaustive exact synthesis, but it is only tractable for output groups with ≤5 combined inputs. It solves the half_adder (4→3 gates) but cannot help the full_adder (3 inputs but 9 gates requires depth-8 search, too slow in Python) or the multipliers (8 shared inputs per output pair).

**Affected circuits**: full_adder (9 vs ABC's 7), mul4_unsigned (104 default, 92 stochastic-16 vs ABC's 82), mul4_signed (106 default, 103 stochastic vs ABC's 83).

**Possible fix**: SAT-based exact synthesis instead of brute-force enumeration, or a C extension for the inner search loop.

### k=5 cut window is too small for large circuits

The rewriter uses cuts of up to 5 inputs. For 32-input circuits, this means each rewrite only sees a small fraction of the logic at once and cannot perform the large-scale restructuring that ABC achieves.

**Affected circuits**: rand_xlarge_clean (102 vs ABC's 94), rand_xlarge_redund (89 vs 81).

**Fix**: larger cut sizes (k=6+), window-based rewriting that considers larger subgraphs, or global restructuring passes like BDD-based resynthesis.

## License

LGPL-3.0-or-later
