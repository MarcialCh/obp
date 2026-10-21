#!/usr/bin/env python3
"""SATPrice: weighted partial MaxSAT with the paper's three reductions.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from pricing.common import KCError, Deadline, ProvenanceGraph, TseitinEncoding

VERSION = 'satprice-1.0'
RC2_OPTIONS = dict(solver='g3', adapt=False, exhaust=False, incr=False,
                   minz=False, trim=0, verbose=0)
# Do not enable a separate solver preprocessor. Backend-native SAT propagation
# and MaxSAT optimization are still intrinsic to solving, in BOTH variants.


@dataclass
class ReducedInstance:
    hard: List[Tuple[int, ...]]
    weights: Dict[int, int]
    offset: int = 0
    trace: List[dict] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    unsat: bool = False

    def restore(self, model: Dict[int, bool], nvars: int) -> Dict[int, bool]:
        values = {v: bool(model.get(v, False)) for v in range(1, nvars+1)}
        for event in reversed(self.trace):
            v = event['variable']
            if event['operation'] == 'assign':
                values[v] = bool(event['value'])
            elif event['operation'] == 'complement':
                values[v] = not values[event['other']]
            else:
                raise KCError('Unknown reconstruction event')
        return values


def stats_zero():
    return dict(indispensable_tuples=0, strong_dominated_tuples=0,
                binary_weak_substitutions=0, forced_false_tuples=0,
                auxiliary_units=0, dominance_candidates=0,
                signed_guard_rejections=0, tautologies_removed=0,
                duplicate_clauses_removed=0, unit_assignments=0,
                preprocessing_passes=0)


def untouched(hard, weights):
    """No application-level reductions, bounds, preprocessing or unit extraction."""
    return ReducedInstance(list(hard), dict(weights), stats=stats_zero())


class Reducer:
    """Mutable CNF indexed by positive/negative occurrences; one rewrite at a time.

    Base cost is a separate offset equivalent to unavoidable empty soft clauses.
    Elimination records are replayed backwards to recover ORIGINAL tuple IDs.
    """
    def __init__(self, hard, weights, deadline: Optional[Deadline]=None):
        if any(not isinstance(v, int) or v <= 0 or not isinstance(w, int) or w < 0
               for v,w in weights.items()):
            raise KCError('Reducer expects positive variable IDs and nonnegative integer prices')
        self.weights = dict(weights)
        self.deadline = deadline
        self.clauses = {}
        self.unique = {}
        self.occ = defaultdict(set)
        self.units = deque()
        self.next_id = 0
        self.offset = 0
        self.trace = []
        self.stats = stats_zero()
        self.fixed = {}
        self.unsat = False
        self.ticks = 0
        for clause in hard:
            self._insert(clause)

    def _check(self):
        self.ticks += 1
        if self.deadline and self.ticks % 1024 == 0:
            self.deadline.check()

    def _insert(self, clause):
        self._check()
        c = frozenset(clause)
        if any(not isinstance(l,int) or l == 0 for l in c):
            raise KCError('CNF literal must be a nonzero integer')
        if any(-l in c for l in c):
            self.stats['tautologies_removed'] += 1
            return
        if c in self.unique:
            self.stats['duplicate_clauses_removed'] += 1
            return
        cid = self.next_id; self.next_id += 1
        self.clauses[cid] = c; self.unique[c] = cid
        for lit in c:
            self.occ[lit].add(cid)
        if not c:
            self.unsat = True
        elif len(c) == 1:
            self.units.append(cid)

    def _erase(self, cid):
        c = self.clauses.pop(cid)
        del self.unique[c]
        for lit in c:
            self.occ[lit].remove(cid)
            if not self.occ[lit]:
                del self.occ[lit]
        return c

    def _assign(self, variable, value, rule, dominator=None):
        if variable in self.fixed:
            if self.fixed[variable] != value:
                self.unsat = True
            return
        self.fixed[variable] = value
        original_weight = self.weights.pop(variable, None)
        if original_weight is not None and value:
            self.offset += original_weight
        event = dict(operation='assign', variable=variable, value=value, rule=rule,
                     charged_weight=original_weight if value and original_weight is not None else 0,
                     offset_after=self.offset)
        if dominator is not None:
            event['dominator'] = dominator
        self.trace.append(event)
        satisfied_lit = variable if value else -variable
        for cid in tuple(self.occ.get(satisfied_lit, ())):
            self._check(); self._erase(cid)
        for cid in tuple(self.occ.get(-satisfied_lit, ())):
            self._check()
            c = self._erase(cid)
            self._insert(c - {-satisfied_lit})

    def _units(self):
        while self.units and not self.unsat:
            self._check()
            cid = self.units.popleft()
            c = self.clauses.get(cid)
            if c is None or len(c) != 1:
                continue
            lit = next(iter(c)); v = abs(lit)
            if v in self.weights:
                rule = 'indispensable' if lit > 0 else 'negative_unit'
                self.stats['indispensable_tuples' if lit > 0 else 'forced_false_tuples'] += 1
            else:
                rule = 'auxiliary_unit'
                self.stats['auxiliary_units'] += 1
            self.stats['unit_assignments'] += 1
            self._assign(v, lit > 0, rule)

    def _dominators(self, v):
        """Def.2: every clause with +v also contains +u."""
        positive = self.occ.get(v)
        if not positive:
            return ()
        rare = min(positive, key=lambda cid: len(self.clauses[cid]))
        candidates = sorted(u for u in self.clauses[rare] if u > 0 and u != v and u in self.weights)
        out = []
        for u in candidates:
            self._check()
            self.stats['dominance_candidates'] += 1
            if positive.issubset(self.occ.get(u, set())):
                out.append(u)
        return out

    def _strong(self):
        for v in sorted(self.weights):
            self._check()
            for u in self._dominators(v):
                if self.weights[u] > self.weights[v]:
                    continue
                # t'=u may change 0 -> 1 in the dominance proof. Its negative
                # occurrences must remain satisfied by setting t=v to 0.
                # This check is vacuous on a purely positive hard CNF.
                if not self.occ.get(-u, set()).issubset(self.occ.get(-v, set())):
                    self.stats['signed_guard_rejections'] += 1
                    continue
                self.stats['strong_dominated_tuples'] += 1
                self._assign(v, False, 'strong_domination', dominator=u)
                return True
        return False

    def _binary(self):
        for cid in sorted(self.clauses):
            self._check()
            c = self.clauses[cid]
            if len(c) != 2 or any(l <= 0 or l not in self.weights for l in c):
                continue
            a,b = sorted(c)
            for v,u in ((a,b),(b,a)):
                if self.weights[v] >= self.weights[u]:
                    continue  # positive residual difference as in paper
                if not self.occ[v].issubset(self.occ[u]):
                    continue
                old_v, old_u = self.weights.pop(v), self.weights[u]
                self.offset += old_v
                self.weights[u] = old_u - old_v
                self.trace.append(dict(operation='complement', variable=v, other=u,
                    rule='binary_weak_domination', removed_weight=old_v,
                    other_weight_before=old_u, other_weight_after=self.weights[u], offset_after=self.offset))
                self.stats['binary_weak_substitutions'] += 1
                affected = sorted(self.occ.get(v,set()) | self.occ.get(-v,set()))
                replaced = [self._erase(k) for k in affected]
                for clause in replaced:
                    self._insert(-u if l == v else u if l == -v else l for l in clause)
                return True
        return False

    def run(self):
        if self.deadline: self.deadline.check()
        while not self.unsat:
            self.stats['preprocessing_passes'] += 1
            self._units()
            if self.unsat: break
            if self._strong(): continue
            if self._binary(): continue
            break
        if self.deadline: self.deadline.check()
        hard = [tuple(sorted(self.clauses[cid],key=lambda x:(abs(x),x))) for cid in sorted(self.clauses)]
        return ReducedInstance(hard, dict(self.weights), self.offset, list(self.trace),
                               dict(self.stats), self.unsat)


def encode(graph: ProvenanceGraph, mode='tseitin', deadline=None):
    if mode == 'tseitin':
        encoding = TseitinEncoding(graph)
        clauses = []
        for i,c in enumerate(encoding.clauses()):
            if deadline and i % 1024 == 0: deadline.check()
            clauses.append(tuple(c))
        return clauses, encoding.nvars, dict(encoding.ids)
    if mode != 'preserve-cnf':
        raise KCError('Unknown encoding: '+mode)
    # This optional input-preserving mode performs NO distribution or clause
    # minimization. It accepts only an already-conjunctive positive formula.
    ids = {token:i+1 for i,token in enumerate(graph.inputs)}
    clauses = []
    pending = list(reversed(graph.roots))
    while pending:
        if deadline: deadline.check()
        node = pending.pop(); gate = graph.nodes[node]
        if gate.kind in ('and','alias'):
            pending.extend(reversed(gate.children)); continue
        if gate.kind == 'true': continue
        if gate.kind == 'false': clauses.append(()); continue
        stack=[node]; literals=[]; tautology=False
        while stack:
            current=stack.pop(); child=graph.nodes[current]
            if child.kind=='input': literals.append(ids[current])
            elif child.kind in ('or','alias'): stack.extend(reversed(child.children))
            elif child.kind=='false': continue
            elif child.kind=='true': tautology=True; break
            else: raise KCError('preserve-cnf accepts only a positive CNF; use --encoding tseitin')
        if not tautology: clauses.append(tuple(dict.fromkeys(literals)))
    return clauses,len(graph.inputs),ids


def satisfied_cnf(hard, assignment):
    return all(any(bool(assignment.get(abs(l),False)) == (l>0) for l in c) for c in hard)


def instance_digest(hard, weights, nvars):
    payload=json.dumps({'nvars':nvars,'hard':[list(c) for c in hard],
                        'weights':[[v,w] for v,w in sorted(weights.items())]},separators=(',',':')).encode()
    return hashlib.sha256(payload).hexdigest()


def write_wcnf(path, hard, weights, nvars, offset=0):
    # Zero weights must NOT become hard clauses; omit them from soft list.
    soft=[(v,w) for v,w in sorted(weights.items()) if w>0]
    top=sum(w for _,w in soft)+1
    with Path(path).open('w',encoding='ascii') as out:
        out.write('c objective_offset %d\n'%offset)
        out.write('p wcnf %d %d %d\n'%(nvars,len(hard)+len(soft),top))
        for c in hard:
            out.write('%d %s0\n'%(top,''.join('%d '%l for l in c)))
        for v,w in soft:
            out.write('%d -%d 0\n'%(w,v))


def solve_rc2(hard, weights, nvars):
    """Real PySAT backend; no silent reference-solver fallback."""
    try:
        import pysat
        from pysat.formula import WCNF
        from pysat.examples.rc2 import RC2
    except ImportError as exc:
        raise KCError('PySAT is missing in this Python interpreter. Install: '
                      'python -m pip install -r utilityTools/pricing/requirements.txt') from exc
    wcnf=WCNF()
    for c in hard: wcnf.append(list(c))
    for v,w in sorted(weights.items()):
        if w>0: wcnf.append([-v],weight=int(w))
    wcnf.nv=max(wcnf.nv,nvars)
    # These settings match the no-reduction ablation. RC2's ordinary SAT
    # propagation and its core-guided search remain essential in both runs.
    with RC2(wcnf, **RC2_OPTIONS) as engine:
        model=engine.compute()
        cost=None if model is None else int(engine.cost)
    meta={'maxsat_backend':'PySAT-RC2','python_sat_version':pysat.__version__,
          'solver_options':dict(RC2_OPTIONS)}
    return (None if model is None else {abs(l):l>0 for l in model}),cost,meta


def run_satprice(graph, prices, folder: Path, deadline: Deadline,
                 reductions=True, encoding='tseitin', backend=solve_rc2):
    """Return selected ORIGINAL input IDs and fully audited result metadata."""
    t=time.perf_counter()
    original,nvars,ids=encode(graph,encoding,deadline)
    info={'encoding':encoding,'n_cnf_vars':nvars,'n_cnf_clauses':len(original),
          'n_aux_vars':nvars-len(graph.inputs),
          'cnf_seconds':time.perf_counter()-t,'rules_enabled':reductions,
          'raw_instance_sha256':instance_digest(original,prices.units,nvars),
          'strong_domination_scope':'positive inclusion plus negative-occurrence safety guard'}
    write_wcnf(folder/'original.wcnf',original,prices.units,nvars)
    if reductions:
        t=time.perf_counter()
        reduced=Reducer(original,prices.units,deadline).run()
        info['reduction_seconds']=time.perf_counter()-t
    else:
        reduced=untouched(original,prices.units)
        info['reduction_seconds']=0.0
    info.update(reduced.stats)
    info.update(n_hard_after=len(reduced.hard),n_soft_after=sum(w>0 for w in reduced.weights.values()),
                n_hard_before=len(original),n_soft_before=sum(w>0 for w in prices.units.values()),
                objective_offset_units=reduced.offset, objective_offset=prices.format(reduced.offset))
    from pricing.common import write_json
    write_json(folder/'reduction_trace.json',dict(stats=reduced.stats,offset_units=reduced.offset,
        steps=reduced.trace,rules_enabled=reductions,source_variable_ids=list(range(1,len(graph.inputs)+1))))
    write_wcnf(folder/'solver_input.wcnf',reduced.hard,reduced.weights,nvars,reduced.offset)
    info['solver_input_sha256']=instance_digest(reduced.hard,reduced.weights,nvars)
    info['same_instance_as_raw']=info['solver_input_sha256']==info['raw_instance_sha256']
    deadline.check()
    t=time.perf_counter()
    if reduced.unsat:
        model,cost,metadata=None,None,{'maxsat_backend':'not-invoked-preprocessing-unsat'}
    else:
        model,cost,metadata=backend(reduced.hard,reduced.weights,nvars)
    info['solver_seconds']=time.perf_counter()-t
    info.update(metadata)
    deadline.check()
    if model is None:
        return None,None,dict(info,status='UNSAT')
    # Check reduced cost as well as the model. Backends never get to assert
    # optimality while returning a model of a different formula.
    if not satisfied_cnf(reduced.hard,model):
        raise KCError('MaxSAT returned a model that violates reduced hard clauses')
    residual=sum(w for v,w in reduced.weights.items() if model.get(v,False))
    if residual != cost:
        raise KCError('MaxSAT cost does not match the reduced tuple-price objective')
    restored=reduced.restore(model,nvars)
    if not satisfied_cnf(original,restored):
        raise KCError('Reconstructed model violates original hard clauses')
    selected={v for v in prices.units if restored[v]}
    value=sum(prices.units[v] for v in selected)
    if value != reduced.offset+cost:
        raise KCError('Restored purchase cost differs from offset + residual objective')
    if not graph.satisfied({graph.inputs[v-1] for v in selected}):
        raise KCError('Purchase set does not preserve the full original query answer set')
    info.update(residual_price_units=cost,residual_price=prices.format(cost),
                n_reconstruction_steps=len(reduced.trace),status='EXACT',support_verified=True)
    return value,selected,info
