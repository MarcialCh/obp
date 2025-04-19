import math
from scipy.special import comb

from dnnf import dDNNF

def approximation(cnf_fname, dnnf, tuple2price=None):
    p = 0
    if tuple2price is None:
        tuple2price = {v: (1 if v not in dnnf._forget_vars else 0) for v in dnnf._var2idx}
    with open(cnf_fname, 'r') as f:
        c = f.readlines()
    clauses = []
    for clause in c:
        if clause[0] != 'p':
            clause = clause.split()
            clause = [int(v) for v in clause]
            clauses.append(clause)
    while len(clauses) != 0:
        vs = {}
        for clause in clauses:       
            n_pos = 0
            n_neg = 0
            for v in clause:
                if v > 0:
                    n_pos += 1
                else:
                    n_neg += 1
            for v in clause:
                is_pos = v > 0
                v = abs(v)
                if v not in vs:
                    vs[v] = 0
                vs[v] += (1 if is_pos else -1)/(len(clause) * comb(len(clause)-1, n_neg if is_pos else n_neg-1))

        v_sel = max(vs, key=lambda v: vs[v])
        p += tuple2price.get(v_sel, 0)
        clauses = [clause for clause in clauses if v_sel not in clause]
    return p
