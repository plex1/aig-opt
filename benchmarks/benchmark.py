#!/usr/bin/env python3
"""Benchmark AIG optimizer against Yosys."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aig_opt.aiger import parse_aag, write_aag
from aig_opt.optimizer import optimize


CIRCUITS_DIR = Path(__file__).parent / "circuits"

YOSYS_SCRIPT = """\
read_aiger {input}
synth -flatten
aigmap
write_aiger -ascii {output}
"""


def run_our_optimizer(input_path: Path) -> tuple[int, float]:
    """Run our optimizer. Returns (gate_count, time_seconds)."""
    start = time.perf_counter()
    aig = parse_aag(input_path)
    aig = optimize(aig)
    aig.compact()
    elapsed = time.perf_counter() - start
    return aig.num_ands(), elapsed


def run_yosys(input_path: Path) -> tuple[int | None, float | None]:
    """Run Yosys optimization. Returns (gate_count, time_seconds) or (None, None)."""
    yosys_bin = shutil.which("yosys")
    if yosys_bin is None:
        return None, None

    try:
        with tempfile.NamedTemporaryFile(suffix=".aag", delete=False) as out_f:
            out_path = out_f.name
        with tempfile.NamedTemporaryFile(suffix=".ys", mode="w", delete=False) as script_f:
            script_f.write(YOSYS_SCRIPT.format(input=input_path, output=out_path))
            script_path = script_f.name

        start = time.perf_counter()
        result = subprocess.run(
            [yosys_bin, "-s", script_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        elapsed = time.perf_counter() - start

        if result.returncode != 0:
            print(f"  Yosys error: {result.stderr[:200]}", file=sys.stderr)
            return None, None

        out_aig = parse_aag(out_path)
        return out_aig.num_ands(), elapsed

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        print(f"  Yosys failed: {e}", file=sys.stderr)
        return None, None
    finally:
        Path(out_path).unlink(missing_ok=True)
        Path(script_path).unlink(missing_ok=True)


def main() -> None:
    circuits = sorted(CIRCUITS_DIR.glob("*.aag"))
    if not circuits:
        print("No .aag files found in", CIRCUITS_DIR)
        return

    has_yosys = shutil.which("yosys") is not None

    # Header
    header = f"{'Circuit':<20} {'Original':>8} {'aig-opt':>8}"
    sep = f"{'─' * 20} {'─' * 8} {'─' * 8}"
    if has_yosys:
        header += f" {'Yosys':>8} {'Our Time':>10} {'Yosys Time':>10}"
        sep += f" {'─' * 8} {'─' * 10} {'─' * 10}"
    else:
        header += f" {'Our Time':>10}"
        sep += f" {'─' * 10}"
        print("Note: Yosys not found. Install yosys to enable comparison.\n")

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


if __name__ == "__main__":
    main()
