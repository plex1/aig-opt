#!/usr/bin/env python3
"""Benchmark AIG optimizer against Yosys (via pyosys)."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aig_opt.aiger import parse_aag, write_aag
from aig_opt.optimizer import optimize


CIRCUITS_DIR = Path(__file__).parent / "circuits"


def _check_pyosys() -> bool:
    """Check if pyosys is available."""
    try:
        from pyosys import libyosys  # noqa: F401
        return True
    except ImportError:
        return False


def run_our_optimizer(input_path: Path) -> tuple[int, float]:
    """Run our optimizer. Returns (gate_count, time_seconds)."""
    start = time.perf_counter()
    aig = parse_aag(input_path)
    aig = optimize(aig)
    aig.compact()
    elapsed = time.perf_counter() - start
    return aig.num_ands(), elapsed


def run_yosys(input_path: Path) -> tuple[int | None, float | None]:
    """Run Yosys optimization via pyosys. Returns (gate_count, time_seconds) or (None, None)."""
    try:
        from pyosys import libyosys as ys
    except ImportError:
        return None, None

    abs_input = str(input_path.resolve())

    with tempfile.NamedTemporaryFile(suffix=".aag", delete=False) as out_f:
        out_path = out_f.name

    try:
        start = time.perf_counter()

        design = ys.Design()
        # Suppress yosys log output by redirecting stdout/stderr
        old_stdout_fd = os.dup(1)
        old_stderr_fd = os.dup(2)
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        try:
            ys.run_pass(f"read_aiger {abs_input}", design)
            ys.run_pass("synth -flatten", design)
            ys.run_pass("aigmap", design)
            ys.run_pass(f"write_aiger -ascii {out_path}", design)
        finally:
            os.dup2(old_stdout_fd, 1)
            os.dup2(old_stderr_fd, 2)
            os.close(devnull_fd)
            os.close(old_stdout_fd)
            os.close(old_stderr_fd)

        elapsed = time.perf_counter() - start

        out_aig = parse_aag(out_path)
        return out_aig.num_ands(), elapsed

    except Exception as e:
        print(f"  Yosys failed on {input_path.name}: {e}", file=sys.stderr)
        return None, None
    finally:
        Path(out_path).unlink(missing_ok=True)


def main() -> None:
    circuits = sorted(CIRCUITS_DIR.glob("*.aag"))
    if not circuits:
        print("No .aag files found in", CIRCUITS_DIR)
        return

    has_yosys = _check_pyosys()

    # Header
    header = f"{'Circuit':<20} {'Original':>8} {'aig-opt':>8}"
    sep = f"{'─' * 20} {'─' * 8} {'─' * 8}"
    if has_yosys:
        header += f" {'Yosys':>8} {'Our Time':>10} {'Yosys Time':>10}"
        sep += f" {'─' * 8} {'─' * 10} {'─' * 10}"
    else:
        header += f" {'Our Time':>10}"
        sep += f" {'─' * 10}"
        print("Note: pyosys not found. Install with: pip install pyosys\n")

    print(header)
    print(sep)

    for circuit_path in circuits:
        name = circuit_path.name
        original_aig = parse_aag(circuit_path)
        orig_gates = original_aig.num_ands()

        opt_gates, opt_time = run_our_optimizer(circuit_path)

        line = f"{name:<20} {orig_gates:>8} {opt_gates:>8}"

        if has_yosys:
            yosys_gates, yosys_time = run_yosys(circuit_path)
            yosys_str = str(yosys_gates) if yosys_gates is not None else "N/A"
            yosys_time_str = f"{yosys_time:.4f}s" if yosys_time is not None else "N/A"
            line += f" {yosys_str:>8} {opt_time:>9.4f}s {yosys_time_str:>10}"
        else:
            line += f" {opt_time:>9.4f}s"

        print(line)

    print()
    print("Gate counts represent number of AND gates in the AIG.")
    if has_yosys:
        print("Yosys pipeline: read_aiger -> synth -flatten -> aigmap -> write_aiger")


if __name__ == "__main__":
    main()
