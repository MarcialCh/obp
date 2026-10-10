#!/usr/bin/env python3
"""Baseline based on MINIMAL why-provenance, not a no-reduction SAT solver.

The supplied paper Section V describes [15]'s Baseline but gives no enumeration
pseudocode. This is an explicit reimplementation of that stated strategy:
construct inclusion-minimal global supports by antichain union/product, then
scan their actual purchase costs. Not the original [15] source implementation.
"""
from __future__ import annotations
import time
from typing import Optional
from pricing.common import KCError, Deadline, ProvenanceGraph


class SupportLimitExceeded(KCError):
    pass


class MinimalWhy:
    def __init__(self, graph: ProvenanceGraph, deadline: Optional[Deadline]=None,
                 max_supports=200000, max_candidates=5000000):
        self.graph,self.deadline=graph,deadline
        self.max_supports,self.max_candidates=max_supports,max_candidates
        self.generated=0;self.subset_tests=0;self.max_family=0
        self.dominated=0;self.candidate_steps=0

    def check(self):
        self.candidate_steps+=1
        if self.deadline and self.candidate_steps%1024==0:self.deadline.check()

    def insert(self,family,candidate):
        self.generated+=1
        if self.max_candidates and self.generated>self.max_candidates:
            raise SupportLimitExceeded('Baseline candidate budget exceeded; no partial price is exact')
        self.check()
        to_remove=[]
        for existing in family:
            self.subset_tests+=1;self.check()
            if existing & candidate == existing:
                self.dominated+=1;return
            if candidate & existing == candidate:to_remove.append(existing)
        for old in to_remove:family.remove(old)
        family.append(candidate)
        self.max_family=max(self.max_family,len(family))
        if self.max_supports and len(family)>self.max_supports:
            raise SupportLimitExceeded('Baseline antichain size limit exceeded; no truncation is performed')

    def union(self,families):
        result=[]
        for family in families:
            for support in family:self.insert(result,support)
        return result

    def product(self,a,b):
        result=[]
        for x in a:
            for y in b:self.insert(result,x|y)
        return result

    def enumerate(self):
        if self.deadline:self.deadline.check()
        graph=self.graph
        bits={t:1<<i for i,t in enumerate(graph.inputs)}
        # Reclaim families when their last DAG parent/root consumer has used
        # them. No price, upper-bound or cheapest-per-answer heuristic is used.
        uses={t:0 for t in graph.nodes}
        for gate in graph.nodes.values():
            for child in gate.children:uses[child]+=1
        for root in graph.roots:uses[root]+=1
        families={}
        for token in graph.order:
            self.check();g=graph.nodes[token]
            if g.kind=='input':result=[bits[token]]
            elif g.kind=='true':result=[0]
            elif g.kind=='false':result=[]
            elif g.kind=='alias':result=families[g.children[0]]
            elif g.kind=='or':result=self.union(families[c] for c in g.children)
            else:
                result=[0]
                for child in g.children:result=self.product(result,families[child])
            families[token]=result
            for child in g.children:
                uses[child]-=1
                if uses[child]==0:del families[child]
        all_answers=[0]
        for root in graph.roots:
            all_answers=self.product(all_answers,families[root])
            uses[root]-=1
            if uses[root]==0:del families[root]
        if self.deadline:self.deadline.check()
        return all_answers


def support_ids(bits):
    while bits:
        low=bits & -bits
        yield low.bit_length()
        bits-=low


def run_baseline(graph,prices,deadline,max_supports=200000,max_candidates=5000000):
    engine=MinimalWhy(graph,deadline,max_supports,max_candidates)
    t=time.perf_counter();supports=engine.enumerate()
    info={'why_provenance_seconds':time.perf_counter()-t,'n_minimal_supports':len(supports),
          'baseline_candidates':engine.generated,'baseline_subset_tests':engine.subset_tests,
          'baseline_max_family':engine.max_family,'baseline_dominated_candidates':engine.dominated,
          'baseline_strategy':'global minimal-why antichain enumeration then minimum price',
          'price_pruning':False,'solver_backend':'not-used'}
    t=time.perf_counter();best=None;best_support=None
    for candidate in supports:
        deadline.check()
        value=sum(prices.units[v] for v in support_ids(candidate))
        if best is None or value<best:best,best_support=value,candidate
    info['min_support_price_seconds']=time.perf_counter()-t
    if best is None:return None,None,dict(info,status='UNSAT')
    selected=set(support_ids(best_support))
    if not graph.satisfied({graph.inputs[i-1] for i in selected}):
        raise KCError('Baseline selected support fails the original global provenance')
    return best,selected,dict(info,status='EXACT',support_verified=True)
