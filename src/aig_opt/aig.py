"""Core AIG (And-Inverter Graph) data structure."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy


# Constant literals
CONST_FALSE = 0
CONST_TRUE = 1


def lit_to_var(lit: int) -> int:
    return lit >> 1


def is_negated(lit: int) -> bool:
    return bool(lit & 1)


def negate(lit: int) -> int:
    return lit ^ 1


def make_lit(var: int, neg: bool = False) -> int:
    return var * 2 + int(neg)


def resolve(subs: dict[int, int], lit: int) -> int:
    """Follow substitution chains, handling negation correctly."""
    visited = set()
    while True:
        if lit in subs:
            if lit in visited:
                break
            visited.add(lit)
            lit = subs[lit]
        elif negate(lit) in subs:
            if negate(lit) in visited:
                break
            visited.add(negate(lit))
            lit = negate(subs[negate(lit)])
        else:
            break
    return lit


@dataclass
class AIG:
    max_var: int = 0
    inputs: list[int] = field(default_factory=list)        # variable indices
    latches: list[tuple[int, int]] = field(default_factory=list)  # (cur_var, next_lit)
    outputs: list[int] = field(default_factory=list)        # output literals
    and_gates: dict[int, tuple[int, int]] = field(default_factory=dict)  # var -> (rhs0, rhs1)
    symbols: dict[str, str] = field(default_factory=dict)
    comments: list[str] = field(default_factory=list)

    def num_ands(self) -> int:
        return len(self.and_gates)

    def num_inputs(self) -> int:
        return len(self.inputs)

    def num_outputs(self) -> int:
        return len(self.outputs)

    def num_latches(self) -> int:
        return len(self.latches)

    def copy(self) -> AIG:
        return deepcopy(self)

    def remap_literals(self, subs: dict[int, int]) -> None:
        """Apply a substitution map to all gate inputs and outputs."""
        # Remap outputs
        self.outputs = [resolve(subs, o) for o in self.outputs]

        # Remap latch next-state literals
        self.latches = [(v, resolve(subs, nxt)) for v, nxt in self.latches]

        # Remap AND gate inputs
        new_gates = {}
        for var, (r0, r1) in self.and_gates.items():
            out_lit = make_lit(var)
            resolved_out = resolve(subs, out_lit)
            r0 = resolve(subs, r0)
            r1 = resolve(subs, r1)
            if resolved_out == out_lit:
                new_gates[var] = (r0, r1)
            # If the output was substituted, this gate is being replaced — skip it
        self.and_gates = new_gates

    def topological_sort_gates(self) -> list[int]:
        """Return gate variable indices in topological order (respecting dependencies)."""
        order: list[int] = []
        visited: set[int] = set()
        gate_set = set(self.and_gates.keys())

        def visit(v: int) -> None:
            if v in visited or v not in gate_set:
                return
            visited.add(v)
            r0, r1 = self.and_gates[v]
            visit(lit_to_var(r0))
            visit(lit_to_var(r1))
            order.append(v)

        for v in self.and_gates:
            visit(v)

        return order

    def compact(self) -> AIG:
        """Renumber variables to a contiguous 1..N range using topological order. Returns self."""
        # Topological sort of gates to get correct ordering
        topo_gates = self.topological_sort_gates()

        # Build ordered variable list: inputs first, then latches, then gates in topo order
        ordered_vars: list[int] = []
        seen: set[int] = set()

        for v in self.inputs:
            if v not in seen:
                ordered_vars.append(v)
                seen.add(v)
        for v, _ in self.latches:
            if v not in seen:
                ordered_vars.append(v)
                seen.add(v)
        for v in topo_gates:
            if v not in seen:
                ordered_vars.append(v)
                seen.add(v)

        # Also include vars referenced but not defined (shouldn't happen in valid AIG)
        all_referenced: set[int] = set()
        for r0, r1 in self.and_gates.values():
            for lit in (r0, r1):
                v = lit_to_var(lit)
                if v > 0:
                    all_referenced.add(v)
        for o in self.outputs:
            v = lit_to_var(o)
            if v > 0:
                all_referenced.add(v)
        for _, nxt in self.latches:
            v = lit_to_var(nxt)
            if v > 0:
                all_referenced.add(v)
        for v in sorted(all_referenced - seen):
            ordered_vars.append(v)
            seen.add(v)

        sorted_vars = ordered_vars
        var_map = {old: new for new, old in enumerate(sorted_vars, start=1)}
        var_map[0] = 0  # constant

        # Build literal mapping
        lit_map: dict[int, int] = {}
        lit_map[CONST_FALSE] = CONST_FALSE
        lit_map[CONST_TRUE] = CONST_TRUE
        for old_var, new_var in var_map.items():
            lit_map[make_lit(old_var)] = make_lit(new_var)
            lit_map[make_lit(old_var, True)] = make_lit(new_var, True)

        def remap_lit(lit: int) -> int:
            if lit in lit_map:
                return lit_map[lit]
            return lit

        # Remap inputs
        self.inputs = [var_map[v] for v in self.inputs]

        # Remap latches
        self.latches = [(var_map[v], remap_lit(nxt)) for v, nxt in self.latches]

        # Remap outputs
        self.outputs = [remap_lit(o) for o in self.outputs]

        # Remap AND gates
        new_gates = {}
        for old_var in sorted(self.and_gates.keys()):
            r0, r1 = self.and_gates[old_var]
            new_var = var_map[old_var]
            new_gates[new_var] = (remap_lit(r0), remap_lit(r1))
        self.and_gates = new_gates

        # Update max_var
        self.max_var = len(sorted_vars)

        # Clear symbols (they reference old indices)
        self.symbols = {}

        return self

    def evaluate(self, input_values: dict[int, bool]) -> dict[int, bool]:
        """Evaluate the AIG for given input values. Returns output values.

        input_values: maps input variable index -> bool value
        Returns: dict mapping output index (0-based) -> bool value
        """
        val: dict[int, int] = {}  # literal -> bool (0 or 1)
        val[CONST_FALSE] = 0
        val[CONST_TRUE] = 1

        for var in self.inputs:
            v = 1 if input_values.get(var, False) else 0
            val[make_lit(var)] = v
            val[make_lit(var, True)] = 1 - v

        # Evaluate AND gates in topological order
        for var in self.topological_sort_gates():
            r0, r1 = self.and_gates[var]
            v0 = val.get(r0, 0)
            v1 = val.get(r1, 0)
            result = v0 & v1
            val[make_lit(var)] = result
            val[make_lit(var, True)] = 1 - result

        # Collect outputs
        outputs = {}
        for i, o in enumerate(self.outputs):
            outputs[i] = bool(val.get(o, 0))
        return outputs

    def truth_table(self) -> list[dict[int, bool]]:
        """Compute the complete truth table for a combinational AIG.
        Returns a list of output dicts, one per input combination."""
        n = len(self.inputs)
        results = []
        for i in range(1 << n):
            input_values = {}
            for j, inp_var in enumerate(self.inputs):
                input_values[inp_var] = bool((i >> j) & 1)
            results.append(self.evaluate(input_values))
        return results
