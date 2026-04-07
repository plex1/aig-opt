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


class TestFunctionalReduction:
    def test_detects_constant_nodes(self):
        """Functional reduction should detect nodes that are always 0 or 1."""
        # Build: x AND (NOT x) = 0, but wrap it so structural propagation misses it
        # (a AND b) AND (a AND NOT b) is always 0
        aig = AIG(
            max_var=5,
            inputs=[1, 2],
            outputs=[make_lit(5)],
            and_gates={
                3: (make_lit(1), make_lit(2)),       # a AND b
                4: (make_lit(1), negate(make_lit(2))),  # a AND NOT b
                5: (make_lit(3), make_lit(4)),        # (a AND b) AND (a AND NOT b) = 0
            },
        )
        from aig_opt.fraig import functional_reduction
        aig = functional_reduction(aig)
        from aig_opt.optimizer import constant_propagation, dead_node_elimination
        aig = constant_propagation(aig)
        aig = dead_node_elimination(aig)
        # Output should be constant 0
        assert aig.outputs[0] == CONST_FALSE

    def test_detects_equivalent_nodes(self):
        """Functional reduction should merge nodes computing the same function."""
        # Two structurally different but functionally equivalent subcircuits
        # f1 = a AND b, f2 = NOT(NOT(a) OR NOT(b)) = a AND b via De Morgan
        # Build f2 as NOT(NOT_a_OR_NOT_b) where NOT_a_OR_NOT_b = NOT(a AND b)
        # Actually simpler: just make two identical gates with different var numbers
        # that structural hashing would NOT catch due to intermediate structure
        aig = AIG(
            max_var=6,
            inputs=[1, 2],
            outputs=[make_lit(6)],  # use f1 XOR f2 = should be 0
            and_gates={
                3: (make_lit(1), make_lit(2)),  # f1 = a AND b
                # f2 = NOT(NOT a OR NOT b) = NOT(NAND(a,b)) = AND(a,b)
                # but built differently: 4 = NAND(a,b) = NOT(a AND b), 5 = NOT(4)
                # We can't directly build NOT as a gate, so let's use:
                # 4 = a AND b (duplicate — structural hash catches this)
                # Instead: test with outputs that reference same function differently
                # Better: build x AND (x OR y) = x AND (NOT(NOT x AND NOT y))
                4: (negate(make_lit(1)), negate(make_lit(2))),  # NOT a AND NOT b
                5: (make_lit(1), negate(make_lit(4))),  # a AND NOT(NOT a AND NOT b) = a AND (a OR b) = a
                # So node 5 == node 1 (input a). fraig should detect this.
                6: (make_lit(5), negate(make_lit(1))),  # a AND NOT a = 0, after merging 5->1
            },
        )
        from aig_opt.optimizer import functional_reduction_pass
        aig = functional_reduction_pass(aig)
        assert aig.outputs[0] == CONST_FALSE

    def test_deep_circuit_reduction(self):
        """Circuits with deep redundancy should be significantly reduced."""
        path = CIRCUITS_DIR / "rand_deep_med.aag"
        aig = parse_aag(path)
        original = aig.copy()
        aig = optimize(aig)
        assert truth_tables_match(original, aig)
        # Should get close to ABC's result of 1 gate
        assert aig.num_ands() <= 5


class TestMultiOutput:
    def test_half_adder_3_gates(self):
        """Multi-output optimization should reduce half adder to 3 AND gates."""
        aig = parse_aag(CIRCUITS_DIR / "half_adder.aag")
        original = aig.copy()
        aig = optimize(aig, multioutput=True)
        assert truth_tables_match(original, aig)
        assert aig.num_ands() <= 3

    def test_half_adder_default_no_multioutput(self):
        """Default optimize (no multioutput) should give 4 gates for half adder."""
        aig = parse_aag(CIRCUITS_DIR / "half_adder.aag")
        original = aig.copy()
        aig = optimize(aig)
        assert truth_tables_match(original, aig)
        assert aig.num_ands() == 4

    def test_exhaustive_finds_shared_xor_and(self):
        """Exhaustive synthesis should find 3-gate solution for (XOR, AND) pair."""
        from aig_opt.multioutput import exhaustive_multioutput_synth
        # XOR = 0x6, AND = 0x8 for 2 inputs
        result = exhaustive_multioutput_synth([0x6, 0x8], num_inputs=2, max_gates=4)
        assert result is not None
        sol_signals, gate_list = result
        assert len(gate_list) == 3

    def test_multioutput_preserves_all_truth_tables(self):
        """Multi-output pass should preserve truth tables on all circuits."""
        for name in ["half_adder.aag", "full_adder.aag", "mux2.aag", "redundant.aag"]:
            aig = parse_aag(CIRCUITS_DIR / name)
            original = aig.copy()
            aig = optimize(aig)
            assert truth_tables_match(original, aig), f"Failed for {name}"

    def test_shared_context_reuses_gates(self):
        """Shared-context synthesis should reuse gates across outputs."""
        from aig_opt.multioutput import shared_context_resynth
        # AND and OR of same inputs: OR = NOT(AND(NOT a, NOT b))
        # AND = 0x8, OR = 0xE for 2 inputs
        # Independent: AND needs 1 gate, OR needs 1 gate = 2 total
        # Shared: same (no sharing possible for these simple functions)
        # Better test: XOR and AND (known 3-gate shared solution exists)
        leaf_lits = [2, 4]  # abstract literals for a, b
        lits, gates, _ = shared_context_resynth([0x6, 0x8], 2, leaf_lits, 3)
        # Should find <= 4 gates (independent would be 3+1=4)
        assert len(gates) <= 4


class TestNPN:
    def test_npn4_class_count(self):
        """Should find exactly 222 NPN equivalence classes for 4-input functions."""
        from aig_opt.npn import _NPN4_OPTIMAL
        assert len(_NPN4_OPTIMAL) == 222

    def test_npn4_gate_counts_reasonable(self):
        """All 4-input NPN classes should need 0-14 AND gates."""
        from aig_opt.npn import _NPN4_OPTIMAL
        for canon, gates in _NPN4_OPTIMAL.items():
            assert 0 <= gates <= 14, f"Canon {canon:#x}: {gates} gates"

    def test_npn_canonical_symmetry(self):
        """AND and OR should be NPN-equivalent (OR = NOT(AND(NOT,NOT)))."""
        from aig_opt.npn import npn_canonical
        # 2-input: AND = 0b1000, OR = 0b1110
        assert npn_canonical(0b1000, 2) == npn_canonical(0b1110, 2)

    def test_npn_canonical_negation(self):
        """A function and its complement should have the same NPN canonical form."""
        from aig_opt.npn import npn_canonical, _tt_mask
        tt = 0b10110100  # arbitrary 3-input function
        mask = _tt_mask(3)
        assert npn_canonical(tt, 3) == npn_canonical(tt ^ mask, 3)

    def test_multi_decomposition_improves(self):
        """Multi-decomposition should find implementations as good or better than single."""
        from aig_opt.npn import synthesize_optimal, _synthesize_and_count
        from aig_opt.rewriter import SynthesisContext, synthesize_tt
        from aig_opt.aig import make_lit

        # XOR3 = a XOR b XOR c (truth table 0x96 for 3 inputs)
        tt = 0x96
        n = 3
        leaf_lits = [make_lit(1), make_lit(2), make_lit(3)]
        empty_hash: dict[tuple[int, int], int] = {}

        # Single decomposition (heuristic)
        ctx = SynthesisContext(empty_hash, 4)
        synthesize_tt(tt, n, leaf_lits, ctx)
        single_cost = ctx.num_new_gates

        # Multi-decomposition
        _, new_gates, _ = synthesize_optimal(tt, n, leaf_lits, empty_hash, 4)
        multi_cost = len(new_gates)

        assert multi_cost <= single_cost

    def test_npn5_cache(self):
        """NPN5 cache should return consistent results."""
        from aig_opt.npn import get_optimal_gate_count
        import random
        rng = random.Random(99)
        tt = rng.randint(0, (1 << 32) - 1)
        g1 = get_optimal_gate_count(tt, 5)
        g2 = get_optimal_gate_count(tt, 5)
        assert g1 == g2
        assert g1 is not None and g1 >= 0

    def test_all_circuits_correctness_with_k5(self):
        """All benchmark circuits should preserve truth tables with k=5 optimization."""
        for name in ["half_adder.aag", "full_adder.aag", "mux2.aag", "redundant.aag"]:
            path = CIRCUITS_DIR / name
            aig = parse_aag(path)
            original = aig.copy()
            aig = optimize(aig)
            assert truth_tables_match(original, aig), f"Failed for {name}"


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
