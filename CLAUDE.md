# CLAUDE.md

## Project Overview

**aig-opt** is a pure Python AIG (And-Inverter Graph) optimizer. It reads/writes AIGER ASCII (.aag) files, applies a pipeline of optimization passes, and benchmarks against Yosys and Berkeley ABC.

No compiled dependencies for the core optimizer. Python >= 3.10.

## Repository Structure

```
src/aig_opt/           # Main package
  aig.py               # Core AIG data structure (literals, gates, evaluate, truth_table)
  aiger.py             # .aag file parser and writer
  optimizer.py         # Pass pipeline orchestration, optimize() entry point
  rewriter.py          # DAG-aware rewriting (cuts, truth tables, Shannon synthesis)
  npn.py               # NPN canonicalization, precomputed tables, synthesis dispatch
  fraig.py             # Simulation-based functional reduction (FRAIG sweeping)
  balance.py           # AIG balancing (AND chains -> balanced trees)
  multioutput.py       # Multi-output exact synthesis (exhaustive enumeration)
  npn4_networks.json   # Precomputed optimal gate networks for 4-input NPN classes
  cli.py               # CLI entry point
  __main__.py           # python -m aig_opt support

benchmarks/
  benchmark.py         # Three-way comparison: aig-opt vs Yosys vs ABC &deepsyn
  generate_circuits.py # Random circuit generator
  generate_multipliers.py  # 4-bit multiplier generator
  circuits/            # .aag benchmark files

tests/
  test_aiger.py        # Parser/writer tests
  test_optimizer.py    # Optimization pass tests (38 tests)
```

## Commands

```bash
# Run tests
python -m pytest tests/ -v

# Run a quick test
python -m pytest tests/ -x -q

# Optimize a circuit
python -m aig_opt benchmarks/circuits/half_adder.aag --stats

# Run benchmarks (requires: pip install pyosys)
python benchmarks/benchmark.py

# Generate multiplier circuits
python benchmarks/generate_multipliers.py
```

## Key Architecture

### AIG representation (`aig.py`)
- Literals: even = positive, odd = negated. `var * 2 = positive lit`, `var * 2 + 1 = negated`.
- Constants: `CONST_FALSE = 0`, `CONST_TRUE = 1`.
- `AIG` dataclass: `inputs` (var list), `outputs` (literal list), `and_gates` (dict: var -> (lit0, lit1)).
- `remap_literals(subs)` applies substitutions. Gates whose output is substituted are removed.

### Optimization pipeline (`optimizer.py`)
- `optimize(aig, balance=False, multioutput=False, stochastic=0)` runs `DEFAULT_PASSES`.
- Optional flags add balance-rewrite cycles (`--balance`), multi-output exact synthesis (`--multioutput`), or stochastic multi-restart (`--stochastic N`).
- Passes modify the AIG in place and return it.

### DAG rewriting (`rewriter.py`)
- `dag_rewrite()` is the core optimization loop. For each node: enumerate cuts, compute truth tables, synthesize, replace if better.
- `SynthesisContext` provides structural hashing for new gate creation.
- `synthesize_tt()` implements Shannon decomposition with special cases (AND, XOR, MUX).
- `compute_subgraph_cost()` is DAG-aware: only counts gates exclusive to the subgraph.

### NPN and synthesis dispatch (`npn.py`)
- 222 NPN equivalence classes for 4-input functions, precomputed at module load (~0.5s).
- `synthesize_optimal()` tries precomputed exact networks first, then all k! Shannon orderings.
- `npn4_networks.json` stores exhaustive-search-optimal networks for 24 NPN classes.

### Functional reduction (`fraig.py`)
- Simulates all nodes with random 64-bit vectors, groups by signature, merges equivalent/complementary nodes.
- Batched verification: exhaustive for ≤20 inputs, statistical for larger.

## Conventions

- **No external dependencies** for core optimizer. `pyosys` only for benchmarking.
- **Truth tables as integers**: bit `i` of the integer = function output for input pattern `i`.
- **All optimizations must preserve functional equivalence**. Verify with `truth_table()` for ≤16 inputs or random simulation for larger.
- **Tests**: `python -m pytest tests/ -v`. All 38 tests must pass before committing.
- **Benchmark**: `python benchmarks/benchmark.py` for the full three-way comparison.

## Testing Patterns

- Truth table equivalence: `original.truth_table() == optimized.truth_table()`
- For large circuits (>16 inputs): random simulation with 2000+ patterns
- Test both default and optional passes (e.g., `optimize(aig, multioutput=True)`)

## Common Pitfalls

- `remap_literals` removes gates whose output is substituted — don't reference them after.
- After any gate replacement in `dag_rewrite`, cuts become stale — must break and recompute.
- `npn.py` module load takes ~0.5s (NPN4 precomputation). This is normal.
- The `exhaustive_multioutput_synth` search is only tractable for ≤5 inputs. Use `time_budget` parameter.
- `compute_subgraph_cost` with DAG-aware fanout means a "5-gate subgraph" might only have cost 2 if 3 gates are shared with other fanouts.
