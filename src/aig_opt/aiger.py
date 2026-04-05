"""AIGER ASCII (.aag) format parser and writer."""

from __future__ import annotations

from pathlib import Path

from .aig import AIG, lit_to_var


def parse_aag(source: str | Path) -> AIG:
    """Parse an AIGER ASCII (.aag) file or string into an AIG.

    Args:
        source: file path (str or Path) or raw AAG string content
    """
    text = str(source)
    # If it looks like a file path (no newlines, exists on disk), read it
    if "\n" not in text:
        path = Path(text)
        if path.exists():
            text = path.read_text()

    lines = text.strip().split("\n")
    idx = 0

    # Header
    header = lines[idx].split()
    idx += 1
    if header[0] != "aag":
        raise ValueError(f"Expected 'aag' header, got '{header[0]}'")

    M, I, L, O, A = int(header[1]), int(header[2]), int(header[3]), int(header[4]), int(header[5])

    aig = AIG(max_var=M)

    # Inputs
    for _ in range(I):
        lit = int(lines[idx])
        idx += 1
        aig.inputs.append(lit_to_var(lit))

    # Latches
    for _ in range(L):
        parts = lines[idx].split()
        idx += 1
        cur_lit = int(parts[0])
        nxt_lit = int(parts[1])
        aig.latches.append((lit_to_var(cur_lit), nxt_lit))

    # Outputs
    for _ in range(O):
        lit = int(lines[idx])
        idx += 1
        aig.outputs.append(lit)

    # AND gates
    for _ in range(A):
        parts = lines[idx].split()
        idx += 1
        lhs = int(parts[0])
        rhs0 = int(parts[1])
        rhs1 = int(parts[2])
        aig.and_gates[lit_to_var(lhs)] = (rhs0, rhs1)

    # Optional symbol table and comments
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("c"):
            # Rest is comments
            aig.comments = [l for l in lines[idx + 1:]]
            break
        elif line and line[0] in ("i", "l", "o"):
            aig.symbols[line] = line
        idx += 1

    return aig


def write_aag(aig: AIG, output: str | Path | None = None) -> str:
    """Write an AIG to AIGER ASCII format.

    Args:
        aig: The AIG to write
        output: Optional file path to write to

    Returns:
        The AAG string
    """
    lines = []

    M = aig.max_var
    I = len(aig.inputs)
    L = len(aig.latches)
    O = len(aig.outputs)
    A = len(aig.and_gates)

    lines.append(f"aag {M} {I} {L} {O} {A}")

    # Inputs
    for var in aig.inputs:
        lines.append(str(var * 2))

    # Latches
    for cur_var, nxt_lit in aig.latches:
        lines.append(f"{cur_var * 2} {nxt_lit}")

    # Outputs
    for lit in aig.outputs:
        lines.append(str(lit))

    # AND gates (in topological order)
    for var in sorted(aig.and_gates.keys()):
        r0, r1 = aig.and_gates[var]
        lines.append(f"{var * 2} {r0} {r1}")

    # Symbol table
    for sym in sorted(aig.symbols.keys()):
        lines.append(sym)

    # Comments
    if aig.comments:
        lines.append("c")
        lines.extend(aig.comments)

    text = "\n".join(lines) + "\n"

    if output is not None:
        Path(output).write_text(text)

    return text
