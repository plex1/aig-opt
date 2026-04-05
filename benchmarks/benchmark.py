#!/usr/bin/env python3
"""Benchmark AIG optimizer against Yosys and ABC &deepsyn."""

from __future__ import annotations

import os
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


def _check_pyosys() -> bool:
    try:
        from pyosys import libyosys  # noqa: F401
        return True
    except ImportError:
        return False


def _find_abc_binary() -> str | None:
    """Find the yosys-abc binary bundled with pyosys."""
    try:
        import pyosys
        abc = Path(pyosys.__file__).parent / "yosys-abc"
        if abc.exists():
            return str(abc)
    except ImportError:
        pass
    return None


def _suppress_fds():
    """Context-manager-like helpers to suppress stdout/stderr at fd level."""
    old1 = os.dup(1)
    old2 = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    return old1, old2, devnull


def _restore_fds(old1, old2, devnull):
    os.dup2(old1, 1)
    os.dup2(old2, 2)
    os.close(devnull)
    os.close(old1)
    os.close(old2)


def _aag_to_aig(input_aag: Path, output_aig: str) -> bool:
    """Convert ASCII .aag to binary .aig using pyosys."""
    try:
        from pyosys import libyosys as ys
        design = ys.Design()
        old1, old2, devnull = _suppress_fds()
        try:
            ys.run_pass(f"read_aiger {input_aag.resolve()}", design)
            ys.run_pass(f"write_aiger {output_aig}", design)
        finally:
            _restore_fds(old1, old2, devnull)
        return True
    except Exception:
        return False


def _aig_to_gate_count(aig_path: str) -> int | None:
    """Read a binary .aig via pyosys and count AND gates."""
    aag_path = None
    try:
        from pyosys import libyosys as ys
        with tempfile.NamedTemporaryFile(suffix=".aag", delete=False) as f:
            aag_path = f.name
        design = ys.Design()
        old1, old2, devnull = _suppress_fds()
        try:
            ys.run_pass(f"read_aiger {aig_path}", design)
            ys.run_pass("aigmap", design)
            ys.run_pass(f"write_aiger -ascii {aag_path}", design)
        finally:
            _restore_fds(old1, old2, devnull)
        aig = parse_aag(aag_path)
        return aig.num_ands()
    except Exception:
        return None
    finally:
        if aag_path:
            Path(aag_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def run_our_optimizer(input_path: Path) -> tuple[int, float]:
    """Run our optimizer. Returns (gate_count, time_seconds)."""
    start = time.perf_counter()
    aig = parse_aag(input_path)
    aig = optimize(aig)
    aig.compact()
    elapsed = time.perf_counter() - start
    return aig.num_ands(), elapsed


def run_yosys(input_path: Path) -> tuple[int | None, float | None]:
    """Run Yosys synth -flatten -> aigmap. Returns (gate_count, time) or (None, None)."""
    try:
        from pyosys import libyosys as ys
    except ImportError:
        return None, None

    with tempfile.NamedTemporaryFile(suffix=".aag", delete=False) as f:
        out_path = f.name

    try:
        start = time.perf_counter()
        design = ys.Design()
        old1, old2, devnull = _suppress_fds()
        try:
            ys.run_pass(f"read_aiger {input_path.resolve()}", design)
            ys.run_pass("synth -flatten", design)
            ys.run_pass("aigmap", design)
            ys.run_pass(f"write_aiger -ascii {out_path}", design)
        finally:
            _restore_fds(old1, old2, devnull)
        elapsed = time.perf_counter() - start
        out_aig = parse_aag(out_path)
        return out_aig.num_ands(), elapsed
    except Exception as e:
        print(f"  Yosys failed on {input_path.name}: {e}", file=sys.stderr)
        return None, None
    finally:
        Path(out_path).unlink(missing_ok=True)


def run_abc_deepsyn(
    input_path: Path, timeout: int = 5, iterations: int = 2,
) -> tuple[int | None, float | None]:
    """Run ABC &deepsyn on the circuit. Returns (gate_count, time) or (None, None).

    First runs Yosys synth to reduce the circuit (ABC &deepsyn works best on
    already-optimized AIGs), then applies &deepsyn for deeper optimization.
    """
    abc_bin = _find_abc_binary()
    if abc_bin is None:
        return None, None

    in_aig = None
    out_aig = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".aig", delete=False) as f:
            in_aig = f.name
        with tempfile.NamedTemporaryFile(suffix=".aig", delete=False) as f:
            out_aig = f.name

        # Convert .aag -> binary .aig via Yosys (includes synth pre-optimization)
        try:
            from pyosys import libyosys as ys
            design = ys.Design()
            old1, old2, devnull = _suppress_fds()
            try:
                ys.run_pass(f"read_aiger {input_path.resolve()}", design)
                ys.run_pass("synth -flatten", design)
                ys.run_pass("aigmap", design)
                ys.run_pass(f"write_aiger {in_aig}", design)
            finally:
                _restore_fds(old1, old2, devnull)
        except Exception:
            return None, None

        # Run ABC &deepsyn (&put converts back to old-style for write_aiger)
        cmd = f"&read {in_aig}; &deepsyn -T {timeout} -I {iterations}; &put; write_aiger {out_aig}"
        start = time.perf_counter()
        result = subprocess.run(
            [abc_bin, "-c", cmd],
            capture_output=True,
            timeout=timeout * iterations + 15,
        )
        elapsed = time.perf_counter() - start

        if result.returncode != 0:
            return None, None

        # Count gates in result
        gates = _aig_to_gate_count(out_aig)
        if gates is None:
            return None, None
        return gates, elapsed

    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"  ABC failed on {input_path.name}: {e}", file=sys.stderr)
        return None, None
    finally:
        if in_aig:
            Path(in_aig).unlink(missing_ok=True)
        if out_aig:
            Path(out_aig).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    circuits = sorted(CIRCUITS_DIR.glob("*.aag"))
    if not circuits:
        print("No .aag files found in", CIRCUITS_DIR)
        return

    has_yosys = _check_pyosys()
    has_abc = _find_abc_binary() is not None

    # Header
    cols = [f"{'Circuit':<25}", f"{'Orig':>5}", f"{'aig-opt':>7}"]
    seps = [f"{'─' * 25}", f"{'─' * 5}", f"{'─' * 7}"]
    if has_yosys:
        cols.append(f"{'Yosys':>7}")
        seps.append(f"{'─' * 7}")
    if has_abc:
        cols.append(f"{'ABC ds':>7}")
        seps.append(f"{'─' * 7}")
    cols += [f"{'Our Time':>9}", f"{'Yosys T':>8}", f"{'ABC T':>8}"]
    seps += [f"{'─' * 9}", f"{'─' * 8}", f"{'─' * 8}"]

    if not has_yosys:
        print("Note: pyosys not found. Install with: pip install pyosys\n")
    if not has_abc:
        print("Note: yosys-abc not found.\n")

    print(" ".join(cols))
    print(" ".join(seps))

    for circuit_path in circuits:
        name = circuit_path.name
        original_aig = parse_aag(circuit_path)
        orig_gates = original_aig.num_ands()

        opt_gates, opt_time = run_our_optimizer(circuit_path)

        parts = [f"{name:<25}", f"{orig_gates:>5}", f"{opt_gates:>7}"]

        if has_yosys:
            yg, yt = run_yosys(circuit_path)
            parts.append(f"{yg if yg is not None else 'N/A':>7}")
        else:
            yg, yt = None, None
            parts.append(f"{'—':>7}")

        if has_abc:
            ag, at = run_abc_deepsyn(circuit_path)
            parts.append(f"{ag if ag is not None else 'N/A':>7}")
        else:
            ag, at = None, None
            parts.append(f"{'—':>7}")

        parts.append(f"{opt_time:>8.3f}s")
        parts.append(f"{yt:>7.3f}s" if yt is not None else f"{'—':>8}")
        parts.append(f"{at:>7.3f}s" if at is not None else f"{'—':>8}")

        print(" ".join(parts))

    print()
    print("Gate counts = number of AND gates in the AIG.")
    print("Yosys: read_aiger -> synth -flatten -> aigmap -> write_aiger")
    print("ABC ds: Yosys synth -> &deepsyn -T 5 -I 2 -> &write")


if __name__ == "__main__":
    main()
