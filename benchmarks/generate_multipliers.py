#!/usr/bin/env python3
"""Generate 4-bit unsigned and signed multiplier circuits as .aag files.

Unsigned: 4-bit × 4-bit -> 8-bit (standard binary multiplication)
Signed: 4-bit × 4-bit -> 8-bit (two's complement, using Baugh-Wooley method)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aig_opt.aig import AIG, CONST_FALSE, CONST_TRUE, make_lit, negate
from aig_opt.aiger import write_aag


class Builder:
    """Helper to build AIG circuits incrementally."""

    def __init__(self, inputs: list[int]):
        self.inputs = inputs
        self.max_var = max(inputs) if inputs else 0
        self.and_gates: dict[int, tuple[int, int]] = {}

    def new_var(self) -> int:
        self.max_var += 1
        return self.max_var

    def make_and(self, a: int, b: int) -> int:
        """Create AND gate, return positive literal."""
        v = self.new_var()
        self.and_gates[v] = (a, b)
        return make_lit(v)

    def make_or(self, a: int, b: int) -> int:
        """a OR b = NOT(NOT(a) AND NOT(b))"""
        return negate(self.make_and(negate(a), negate(b)))

    def make_xor(self, a: int, b: int) -> int:
        """a XOR b = (a AND NOT b) OR (NOT a AND b)"""
        t1 = self.make_and(a, negate(b))
        t2 = self.make_and(negate(a), b)
        return self.make_or(t1, t2)

    def make_half_adder(self, a: int, b: int) -> tuple[int, int]:
        """Returns (sum, carry)."""
        s = self.make_xor(a, b)
        c = self.make_and(a, b)
        return s, c

    def make_full_adder(self, a: int, b: int, cin: int) -> tuple[int, int]:
        """Returns (sum, carry_out)."""
        s1, c1 = self.make_half_adder(a, b)
        s2, c2 = self.make_half_adder(s1, cin)
        cout = self.make_or(c1, c2)
        return s2, cout

    def to_aig(self, outputs: list[int]) -> AIG:
        return AIG(
            max_var=self.max_var,
            inputs=list(self.inputs),
            outputs=outputs,
            and_gates=dict(self.and_gates),
        )


def generate_unsigned_multiplier() -> AIG:
    """Generate a 4-bit unsigned multiplier (4+4 inputs, 8 outputs).

    Standard shift-and-add multiplication:
    P = A * B where A = a3 a2 a1 a0, B = b3 b2 b1 b0
    """
    # Inputs: a0..a3 (vars 1-4), b0..b3 (vars 5-8)
    inputs = list(range(1, 9))
    b = Builder(inputs)

    a = [make_lit(i) for i in range(1, 5)]  # a[0]=LSB
    bl = [make_lit(i) for i in range(5, 9)]  # b[0]=LSB

    # Partial products: pp[i][j] = a[j] AND b[i]
    pp: list[list[int]] = []
    for i in range(4):
        row = []
        for j in range(4):
            row.append(b.make_and(a[j], bl[i]))
        pp.append(row)

    # Add partial products using full/half adders
    # Row 0: p[0] = pp[0][0], rest carry forward
    result = [CONST_FALSE] * 8

    # Bit 0: just pp[0][0]
    result[0] = pp[0][0]

    # Add rows progressively
    # Row 0 contributes bits at positions 0,1,2,3
    # Row 1 contributes bits at positions 1,2,3,4
    # Row 2 contributes bits at positions 2,3,4,5
    # Row 3 contributes bits at positions 3,4,5,6

    # Accumulate partial sums
    # Start with row 0
    acc = [pp[0][j] for j in range(4)] + [CONST_FALSE] * 4

    # Add row 1 (shifted by 1)
    carry = CONST_FALSE
    for j in range(4):
        s, carry = b.make_full_adder(acc[j + 1], pp[1][j], carry)
        acc[j + 1] = s
    acc[5] = carry

    # Add row 2 (shifted by 2)
    carry = CONST_FALSE
    for j in range(4):
        s, carry = b.make_full_adder(acc[j + 2], pp[2][j], carry)
        acc[j + 2] = s
    acc[6] = carry

    # Add row 3 (shifted by 3)
    carry = CONST_FALSE
    for j in range(4):
        s, carry = b.make_full_adder(acc[j + 3], pp[3][j], carry)
        acc[j + 3] = s
    acc[7] = carry

    return b.to_aig(acc[:8])


def generate_signed_multiplier() -> AIG:
    """Generate a 4-bit signed (two's complement) multiplier.

    Uses Baugh-Wooley method: negate partial products involving the sign bits,
    then add correction terms.

    Inputs: a = a3(sign) a2 a1 a0, b = b3(sign) b2 b1 b0
    Output: 8-bit two's complement product
    """
    inputs = list(range(1, 9))
    b = Builder(inputs)

    a = [make_lit(i) for i in range(1, 5)]  # a[0]=LSB, a[3]=sign
    bl = [make_lit(i) for i in range(5, 9)]  # b[0]=LSB, b[3]=sign

    # Baugh-Wooley partial products:
    # For i<3, j<3: pp = a[j] AND b[i]  (positive)
    # For i=3, j<3: pp = NOT(a[j] AND b[3])  (negated)
    # For i<3, j=3: pp = NOT(a[3] AND b[i])  (negated)
    # For i=3, j=3: pp = a[3] AND b[3]  (positive)

    pp: list[list[int]] = []
    for i in range(4):
        row = []
        for j in range(4):
            p = b.make_and(a[j], bl[i])
            if (i == 3) != (j == 3):  # XOR: exactly one is sign bit
                p = negate(p)
            row.append(p)
        pp.append(row)

    # Build accumulator starting with row 0
    acc = [pp[0][j] for j in range(4)] + [CONST_FALSE] * 4

    # Add row 1 (shifted by 1)
    carry = CONST_FALSE
    for j in range(4):
        s, carry = b.make_full_adder(acc[j + 1], pp[1][j], carry)
        acc[j + 1] = s
    acc[5] = carry

    # Add row 2 (shifted by 2)
    carry = CONST_FALSE
    for j in range(4):
        s, carry = b.make_full_adder(acc[j + 2], pp[2][j], carry)
        acc[j + 2] = s
    acc[6] = carry

    # Add row 3 (shifted by 3)
    carry = CONST_FALSE
    for j in range(4):
        s, carry = b.make_full_adder(acc[j + 3], pp[3][j], carry)
        acc[j + 3] = s
    acc[7] = carry

    # Baugh-Wooley correction constant.
    # Each NOT(x) = 1-x contributes +1 at its position. There are 6 negated
    # partial products: 2 each at positions 3, 4, 5. Total excess = +112.
    # To subtract 112 in 8-bit arithmetic: add 256 - 112 = 144 = 0b10010000.
    correction = 0x90  # 144 = 2^4 + 2^7
    carry = CONST_FALSE
    for pos in range(8):
        bit_set = bool((correction >> pos) & 1)
        if bit_set and carry == CONST_FALSE:
            # Adding 1 + acc[pos]
            s, carry = b.make_half_adder(acc[pos], CONST_TRUE)
            acc[pos] = s
        elif bit_set and carry != CONST_FALSE:
            # Adding 1 + carry + acc[pos]
            s, carry = b.make_full_adder(acc[pos], CONST_TRUE, carry)
            acc[pos] = s
        elif not bit_set and carry != CONST_FALSE:
            # Adding carry + acc[pos]
            s, carry = b.make_half_adder(acc[pos], carry)
            acc[pos] = s

    return b.to_aig(acc[:8])


def main() -> None:
    circuits_dir = Path(__file__).parent / "circuits"

    unsigned = generate_unsigned_multiplier()
    write_aag(unsigned, circuits_dir / "mul4_unsigned.aag")
    print(f"mul4_unsigned.aag: {len(unsigned.inputs)} inputs, "
          f"{len(unsigned.outputs)} outputs, {unsigned.num_ands()} AND gates")

    signed = generate_signed_multiplier()
    write_aag(signed, circuits_dir / "mul4_signed.aag")
    print(f"mul4_signed.aag: {len(signed.inputs)} inputs, "
          f"{len(signed.outputs)} outputs, {signed.num_ands()} AND gates")

    # Verify unsigned multiplier
    for i in range(16):
        for j in range(16):
            expected = i * j
            vals = {}
            for bit in range(4):
                vals[bit + 1] = bool((i >> bit) & 1)
                vals[bit + 5] = bool((j >> bit) & 1)
            result = unsigned.evaluate(vals)
            actual = sum(int(result[k]) << k for k in range(8))
            assert actual == expected, f"{i}*{j}: got {actual}, expected {expected}"
    print("Unsigned multiplier verified for all 256 input combinations.")

    # Verify signed multiplier
    for i in range(-8, 8):
        for j in range(-8, 8):
            expected = i * j
            # Two's complement encoding
            ui = i & 0xF
            uj = j & 0xF
            vals = {}
            for bit in range(4):
                vals[bit + 1] = bool((ui >> bit) & 1)
                vals[bit + 5] = bool((uj >> bit) & 1)
            result = signed.evaluate(vals)
            actual = sum(int(result[k]) << k for k in range(8))
            # Sign extend from 8 bits
            if actual >= 128:
                actual -= 256
            assert actual == expected, f"{i}*{j}: got {actual}, expected {expected}"
    print("Signed multiplier verified for all 256 input combinations.")


if __name__ == "__main__":
    main()
