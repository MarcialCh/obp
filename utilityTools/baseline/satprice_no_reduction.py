#!/usr/bin/env python3
"""SATPrice without reductions, using the complete WPM3 algorithm.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Timer
from typing import Optional, Tuple

from pricing.common import Deadline, DeadlineExceeded, KCError
from satprice.satprice import run_satprice, satisfied_cnf


@dataclass
class _AtMost:
    # Tuple, deliberately NOT a set: overlapping cores retain multiplicities.
    variables: Tuple[int, ...]
    bound: int
    weight: int
    selector: Optional[int]


class _WPM3:
    """Algorithm 1's AM collection, SAT oracle and optimize subroutine."""

    def __init__(self, solver, card_enc, enc_type, weights, nvars, deadline):
        self.solver = solver
        self.card_enc = card_enc
        self.enc_type = enc_type
        self.weights = weights
        self.nvars = nvars
        self.top = nvars
        self.deadline = deadline
        self.stats = dict(
            wpm3_sat_calls=0,
            wpm3_main_sat_calls=0,
            wpm3_optimize_sat_calls=0,
            wpm3_cores=0,
            wpm3_weight_splits=0,
            wpm3_optimizations=0,
            wpm3_strata=0,
            wpm3_max_multiset_size=0,
            wpm3_lower_bound_units=0,
        )

    def check(self):
        if self.deadline is not None:
            self.deadline.check()

    def add_bound(self, variables, bound, weight):
        """Install a guarded cardinality constraint; positive selector disables it."""
        self.check()
        variables = tuple(variables)
        if bound < 0 or bound > len(variables):
            raise KCError('WPM3 generated an invalid cardinality bound')
        self.stats['wpm3_max_multiset_size'] = max(
            self.stats['wpm3_max_multiset_size'], len(variables))
        if bound == len(variables):
            return _AtMost(variables, bound, weight, None)
        self.top += 1
        selector = self.top
        # Pass the full list, including repeated variables. Totalizer leaves
        # count occurrences, so [v, v, u] encodes 2*v + u, not v + u.
        cnf = self.card_enc.atmost(
            lits=list(variables), bound=bound, top_id=self.top,
            encoding=self.enc_type.totalizer)
        self.top = max(self.top, cnf.nv)
        self.check()
        for i, clause in enumerate(cnf.clauses):
            if i % 1024 == 0:
                self.check()
            self.solver.add_clause(list(clause) + [selector])
        return _AtMost(variables, bound, weight, selector)

    def retire(self, constraint):
        if constraint.selector is not None:
            self.solver.add_clause([constraint.selector])

    def sat(self, constraints, optimizing=False):
        """Activate only the supplied bounds, with interruptible SAT calls.

        In particular, optimize passes only its own temporary bound. The outer
        AM bounds must NOT be assumed during that independent subproblem.
        """
        self.check()
        active = {c.selector: c for c in constraints if c.selector is not None}
        assumptions = [-selector for selector in active]
        self.stats['wpm3_sat_calls'] += 1
        self.stats['wpm3_optimize_sat_calls' if optimizing
                   else 'wpm3_main_sat_calls'] += 1
        if self.deadline is None:
            status = self.solver.solve(assumptions=assumptions)
        else:
            timer = Timer(self.deadline.remaining(), self.solver.interrupt)
            timer.daemon = True
            timer.start()
            try:
                status = self.solver.solve_limited(
                    assumptions=assumptions, expect_interrupt=True)
            finally:
                timer.cancel()
                # Join before clearing the interrupt or deleting the solver:
                # an already-running timer callback must not reach a freed oracle.
                timer.join()
                self.solver.clear_interrupt()
        self.check()
        if status is None:
            raise DeadlineExceeded('WPM3 SAT search was interrupted')
        if status:
            raw = self.solver.get_model()
            if raw is None:
                raise KCError('WPM3 SAT oracle returned SAT without a model')
            values = {abs(lit): lit > 0 for lit in raw}
            model = {v: values.get(v, False) for v in range(1, self.nvars + 1)}
            return model, []
        raw_core = self.solver.get_core() or []
        if any(-lit not in active for lit in raw_core):
            raise KCError('WPM3 SAT oracle returned an unknown core assumption')
        core_selectors = {-lit for lit in raw_core}
        return None, [c for selector, c in active.items() if selector in core_selectors]

    def optimize(self, core):
        """Find the EXACT minimum number of violations in the core multiset.

        Each SAT model supplies an upper bound; UNSAT at upper-1 proves it.
        We count original clause violations, not arbitrary auxiliary values.
        """
        self.stats['wpm3_optimizations'] += 1
        variables = tuple(v for constraint in core for v in constraint.variables)
        upper = len(variables)
        while upper > 0:
            self.check()
            temporary = self.add_bound(variables, upper - 1, 0)
            try:
                model, _ = self.sat([temporary], optimizing=True)
            finally:
                self.retire(temporary)
            if model is None:
                break
            new_upper = sum(model[v] for v in variables)
            if new_upper >= upper:
                raise KCError('WPM3 subproblem failed to improve its upper bound')
            upper = new_upper
        return variables, upper

    def solve(self):
        # The infinite stratum in Algorithm 1 checks the original hard formula.
        model, _ = self.sat([])
        if model is None:
            return None, None
        if not self.weights:
            return model, 0

        am = [self.add_bound((v,), 0, w) for v, w in sorted(self.weights.items())]
        stratum = max(c.weight for c in am)
        self.stats['wpm3_strata'] = 1
        while True:
            self.check()
            model, core = self.sat([c for c in am if c.weight >= stratum])
            if model is not None:
                lower_weights = [c.weight for c in am if c.weight < stratum]
                if lower_weights:
                    # Stratification must use CURRENT residual AM weights.
                    stratum = max(lower_weights)
                    self.stats['wpm3_strata'] += 1
                    continue
                cost = sum(w for v, w in self.weights.items() if model[v])
                lower_bound = sum(c.bound * c.weight for c in am)
                if cost != lower_bound:
                    raise KCError('WPM3 final model does not match its proven lower bound')
                self.stats['wpm3_lower_bound_units'] = lower_bound
                return model, cost

            if not core:
                # Hard SAT was already checked; no active assumption in a core
                # would indicate a broken oracle/encoding, not a valid optimum.
                raise KCError('WPM3 encountered a hard conflict after the hard SAT check')
            self.stats['wpm3_cores'] += 1
            minimum = min(c.weight for c in core)
            variables, bound = self.optimize(core)
            if bound <= sum(c.bound for c in core):
                raise KCError('WPM3 merged core did not increase the proven lower bound')

            # Algorithm 1, lines 9-12: retain every positive residual copy,
            # then replace the equal-weight copies by their optimized multiset.
            for constraint in core:
                constraint.weight -= minimum
                if constraint.weight:
                    self.stats['wpm3_weight_splits'] += 1
                else:
                    self.retire(constraint)
            am = [c for c in am if c.weight > 0]
            am.append(self.add_bound(variables, bound, minimum))
            self.stats['wpm3_lower_bound_units'] = sum(c.bound * c.weight for c in am)


def solve_wpm3(hard, weights, nvars, deadline=None):
    """Minimize sum(weights[v] * v) subject to hard CNF, using WPM3.

    Variable IDs are 1..nvars; prices are nonnegative Python integers. Unpriced
    variables are free. Return (original_model, cost_units, metadata), with
    (None, None, metadata) for hard UNSAT. Timeout raises DeadlineExceeded.
    Inputs are copied, zero-price soft clauses are omitted, and no application
    reduction, clause deletion or price rounding is performed.
    """
    if deadline is not None:
        deadline.check()
    if isinstance(nvars, bool) or not isinstance(nvars, int) or nvars < 0:
        raise KCError('WPM3 expects a nonnegative integer variable count')
    original_weights = dict(weights)
    for i, (v, w) in enumerate(original_weights.items()):
        if deadline is not None and i % 1024 == 0:
            deadline.check()
        if (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= nvars
                or isinstance(w, bool) or not isinstance(w, int) or w < 0):
            raise KCError('WPM3 expects valid variable IDs and nonnegative integer prices')
    original_hard = []
    for i, clause in enumerate(hard):
        if deadline is not None and i % 1024 == 0:
            deadline.check()
        clause = tuple(clause)
        if any(isinstance(lit, bool) or not isinstance(lit, int)
               or lit == 0 or abs(lit) > nvars for lit in clause):
            raise KCError('WPM3 CNF literals must be nonzero integers within 1..nvars')
        original_hard.append(clause)
    positive_weights = {v: w for v, w in original_weights.items() if w > 0}
    try:
        import pysat
        from pysat.card import CardEnc, EncType
        from pysat.solvers import Solver
    except ImportError as exc:
        raise KCError('PySAT is missing in this Python interpreter. Install: '
                      'python -m pip install -r utilityTools/pricing/requirements.txt') from exc

    with Solver(name='g3') as oracle:
        engine = _WPM3(oracle, CardEnc, EncType, positive_weights, nvars, deadline)
        for i, clause in enumerate(original_hard):
            if i % 1024 == 0:
                engine.check()
            oracle.add_clause(list(clause))
        model, cost = engine.solve()
        engine.check()
        metadata = dict(
            maxsat_backend='WPM3',
            python_sat_version=pysat.__version__,
            wpm3_implementation='pricing-wpm3-1.0',
            wpm3_reference='https://www.ijcai.org/Proceedings/15/Papers/046.pdf',
            solver_options=dict(
                solver='g3', cardinality_encoding='totalizer',
                stratification='descending residual weights',
                core_optimization='exact model-guided cardinality search'),
            **engine.stats)
    if model is not None:
        if not satisfied_cnf(original_hard, model):
            raise KCError('WPM3 model violates original hard clauses')
        if cost != sum(w for v, w in original_weights.items() if model[v]):
            raise KCError('WPM3 cost differs from the original tuple-price objective')
    return model, cost, metadata


def run_no_reduction(graph, prices, out, deadline, encoding='tseitin'):
    """Keep SATPrice's encoding/audit/return contract, using WPM3 without reductions."""
    deadline.check()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    return run_satprice(
        graph, prices, out, deadline, reductions=False, encoding=encoding,
        backend=partial(solve_wpm3, deadline=deadline))
