"""Tests for AIGER parser and writer."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aig_opt.aiger import parse_aag, write_aag
from aig_opt.aig import AIG, make_lit

CIRCUITS_DIR = Path(__file__).parent.parent / "benchmarks" / "circuits"


class TestParseAAG:
    def test_parse_half_adder(self):
        aig = parse_aag(CIRCUITS_DIR / "half_adder.aag")
        assert aig.num_inputs() == 2
        assert aig.num_outputs() == 2
        assert aig.num_latches() == 0
        assert aig.num_ands() == 4

    def test_parse_full_adder(self):
        aig = parse_aag(CIRCUITS_DIR / "full_adder.aag")
        assert aig.num_inputs() == 3
        assert aig.num_outputs() == 2
        assert aig.num_ands() == 9

    def test_parse_mux2(self):
        aig = parse_aag(CIRCUITS_DIR / "mux2.aag")
        assert aig.num_inputs() == 3
        assert aig.num_outputs() == 1
        assert aig.num_ands() == 3

    def test_parse_redundant(self):
        aig = parse_aag(CIRCUITS_DIR / "redundant.aag")
        assert aig.num_inputs() == 2
        assert aig.num_outputs() == 4
        assert aig.num_ands() == 7

    def test_parse_from_string(self):
        text = "aag 3 2 0 1 1\n2\n4\n6\n6 2 4\n"
        aig = parse_aag(text)
        assert aig.num_inputs() == 2
        assert aig.num_outputs() == 1
        assert aig.num_ands() == 1
        assert aig.and_gates[3] == (2, 4)


class TestWriteAAG:
    def test_write_simple(self):
        aig = AIG(
            max_var=3,
            inputs=[1, 2],
            outputs=[6],
            and_gates={3: (2, 4)},
        )
        text = write_aag(aig)
        assert text.startswith("aag 3 2 0 1 1\n")
        assert "6\n" in text  # output
        assert "6 2 4\n" in text  # AND gate

    def test_roundtrip(self):
        """Parse -> write -> parse should produce equivalent AIG."""
        for name in ["half_adder.aag", "full_adder.aag", "mux2.aag"]:
            path = CIRCUITS_DIR / name
            aig1 = parse_aag(path)
            text = write_aag(aig1)
            aig2 = parse_aag(text)

            assert aig2.num_inputs() == aig1.num_inputs(), f"Failed for {name}"
            assert aig2.num_outputs() == aig1.num_outputs(), f"Failed for {name}"
            assert aig2.num_ands() == aig1.num_ands(), f"Failed for {name}"
            assert aig2.inputs == aig1.inputs, f"Failed for {name}"
            assert aig2.outputs == aig1.outputs, f"Failed for {name}"
            assert aig2.and_gates == aig1.and_gates, f"Failed for {name}"

    def test_write_to_file(self, tmp_path):
        aig = AIG(
            max_var=3,
            inputs=[1, 2],
            outputs=[6],
            and_gates={3: (2, 4)},
        )
        out_path = tmp_path / "test.aag"
        write_aag(aig, out_path)
        assert out_path.exists()
        aig2 = parse_aag(out_path)
        assert aig2.num_ands() == 1
