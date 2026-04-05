# aig-opt

A pure Python AIG (And-Inverter Graph) optimizer that reads/writes AIGER ASCII (.aag) files, applies optimization passes, and benchmarks against Yosys.

## Features

- AIGER ASCII (.aag) parser and writer with round-trip fidelity
- Optimization passes:
  - Constant propagation (`0 AND x = 0`, `1 AND x = x`)
  - Trivial simplification (`x AND x = x`, `x AND !x = 0`)
  - Structural hashing (merge duplicate gates)
  - Dead node elimination (remove unreachable gates)
  - Simple rewriting (factor shared inputs in 2-level cones)
  - DAG-aware rewriting with 4-input cut enumeration and truth-table-based resynthesis
- CLI tool for optimizing .aag files
- Benchmark script comparing against Yosys (via pyosys)
- Random circuit generator for benchmarking

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
  rewriter.py      # DAG-aware rewriting engine
  cli.py           # CLI entry point
benchmarks/
  benchmark.py     # Yosys comparison benchmark
  generate_circuits.py
  circuits/        # Sample .aag files
tests/
  test_aiger.py
  test_optimizer.py
```

## Requirements

- Python >= 3.10 (no dependencies for core optimizer)
- `pyosys` (optional, for Yosys benchmarking): `pip install pyosys`
- `pytest` (for tests): `pip install pytest`

## License

LGPL-3.0-or-later
