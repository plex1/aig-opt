"""Tests for AIG optimization passes."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aig_opt.aig import AIG, CONST_FALSE, CONST_TRUE, make_lit, negate
from aig_opt.aiger import parse_aag
from aig_opt.optimizer import (
    constant_propagation,
    dead_node_elimination,
    optimize,
    simple_rewrite,
    structural_hashing,
)

CIRCUITS_DIR = Path(__file__).parent.parent / "benchmarks" / "circuits"


def truth_tables_match(aig1: AIG, aig2: AIG) -> bool:
    """Check that two AIGs compute the same function."""
    return aig1.truth_table() == aig2.truth_table()


class TestConstantPropagation:
    def test_and_with_zero(self):
        """x AND 0 = 0"""
        aig = AIG(
            max_var=2,
            inputs=[1],
            outputs=[make_lit(2)],
            and_gates={2: (make_lit(1), CONST_FALSE)},
        )
        aig = constant_propagation(aig)
        # Output should be constant 0
        assert aig.outputs[0] == CONST_FALSE
        assert aig.num_ands() == 0

    def test_and_with_one(self):
        """x AND 1 = x"""
        aig = AIG(
            max_var=2,
            inputs=[1],
            outputs=[make_lit(2)],
            and_gates={2: (make_lit(1), CONST_TRUE)},
        )
        aig = constant_propagation(aig)
        assert aig.outputs[0] == make_lit(1)
        assert aig.num_ands() == 0

    def test_x_and_x(self):
        """x AND x = x"""
        aig = AIG(
            max_var=2,
            inputs=[1],
            outputs=[make_lit(2)],
            and_gates={2: (make_lit(1), make_lit(1))},
        )
        aig = constant_propagation(aig)
        assert aig.outputs[0] == make_lit(1)
        assert aig.num_ands() == 0

    def test_x_and_not_x(self):
        """x AND !x = 0"""
        aig = AIG(
            max_var=2,
            inputs=[1],
            outputs=[make_lit(2)],
            and_gates={2: (make_lit(1), negate(make_lit(1)))},
        )
        aig = constant_propagation(aig)
        assert aig.outputs[0] == CONST_FALSE
        assert aig.num_ands() == 0

    def test_chain_propagation(self):
        """Propagation through chains: var3 = 1 AND var2, var2 = 0 AND x"""
        aig = AIG(
            max_var=3,
            inputs=[1],
            outputs=[make_lit(3)],
            and_gates={
                2: (make_lit(1), CONST_FALSE),  # var2 = x AND 0 = 0
                3: (CONST_TRUE, make_lit(2)),    # var3 = 1 AND var2 = var2 = 0
            },
        )
        aig = constant_propagation(aig)
        assert aig.outputs[0] == CONST_FALSE
        assert aig.num_ands() == 0


class TestStructuralHashing:
    def test_duplicate_gates(self):
        """Two identical gates should be merged."""
        aig = AIG(
            max_var=4,
            inputs=[1, 2],
            outputs=[make_lit(3), make_lit(4)],
            and_gates={
                3: (make_lit(1), make_lit(2)),
                4: (make_lit(1), make_lit(2)),
            },
        )
        original = aig.copy()
        aig = structural_hashing(aig)
        assert aig.num_ands() == 1
        assert truth_tables_match(original, aig)

    def test_permuted_inputs(self):
        """Gates with permuted inputs should be merged."""
        aig = AIG(
            max_var=4,
            inputs=[1, 2],
            outputs=[make_lit(3), make_lit(4)],
            and_gates={
                3: (make_lit(1), make_lit(2)),
                4: (make_lit(2), make_lit(1)),  # same but swapped
            },
        )
        original = aig.copy()
        aig = structural_hashing(aig)
        assert aig.num_ands() == 1
        assert truth_tables_match(original, aig)


class TestDeadNodeElimination:
    def test_remove_dead_gate(self):
        """Gates not connected to outputs should be removed."""
        aig = AIG(
            max_var=4,
            inputs=[1, 2],
            outputs=[make_lit(3)],  # only var3 is output
            and_gates={
                3: (make_lit(1), make_lit(2)),
                4: (make_lit(1), make_lit(2)),  # dead - not in any output
            },
        )
        aig = dead_node_elimination(aig)
        assert aig.num_ands() == 1
        assert 4 not in aig.and_gates


class TestOptimize:
    def test_redundant_circuit(self):
        """The redundant circuit should be heavily optimized."""
        aig = parse_aag(CIRCUITS_DIR / "redundant.aag")
        original = aig.copy()
        assert aig.num_ands() == 7

        aig = optimize(aig)
        assert aig.num_ands() < 7  # should be significantly reduced
        assert truth_tables_match(original, aig)

    def test_half_adder_equivalence(self):
        """Optimization should preserve half adder truth table."""
        aig = parse_aag(CIRCUITS_DIR / "half_adder.aag")
        original = aig.copy()
        aig = optimize(aig)
        assert truth_tables_match(original, aig)

    def test_full_adder_equivalence(self):
        """Optimization should preserve full adder truth table."""
        aig = parse_aag(CIRCUITS_DIR / "full_adder.aag")
        original = aig.copy()
        aig = optimize(aig)
        assert truth_tables_match(original, aig)

    def test_mux_equivalence(self):
        """Optimization should preserve mux truth table."""
        aig = parse_aag(CIRCUITS_DIR / "mux2.aag")
        original = aig.copy()
        aig = optimize(aig)
        assert truth_tables_match(original, aig)

    def test_half_adder_truth_table(self):
        """Verify half adder computes correct truth table."""
        aig = parse_aag(CIRCUITS_DIR / "half_adder.aag")
        tt = aig.truth_table()
        # a=0,b=0 -> sum=0, carry=0
        assert tt[0] == {0: False, 1: False}
        # a=1,b=0 -> sum=1, carry=0
        assert tt[1] == {0: True, 1: False}
        # a=0,b=1 -> sum=1, carry=0
        assert tt[2] == {0: True, 1: False}
        # a=1,b=1 -> sum=0, carry=1
        assert tt[3] == {0: False, 1: True}

    def test_full_adder_truth_table(self):
        """Verify full adder computes correct truth table."""
        aig = parse_aag(CIRCUITS_DIR / "full_adder.aag")
        # Test a few key cases
        # a=0,b=0,cin=0 -> sum=0, cout=0
        tt = aig.truth_table()
        assert tt[0] == {0: False, 1: False}
        # a=1,b=1,cin=0 -> sum=0, cout=1
        assert tt[3] == {0: False, 1: True}
        # a=1,b=1,cin=1 -> sum=1, cout=1
        assert tt[7] == {0: True, 1: True}


class TestCompact:
    def test_compact_renumbers(self):
        """Compact should renumber variables to 1..N."""
        aig = AIG(
            max_var=10,
            inputs=[1, 5],
            outputs=[make_lit(10)],
            and_gates={10: (make_lit(1), make_lit(5))},
        )
        original = aig.copy()
        aig.compact()
        assert aig.max_var == 3  # 2 inputs + 1 gate
        assert aig.inputs == [1, 2]
        assert 3 in aig.and_gates
        assert truth_tables_match(original, aig)
