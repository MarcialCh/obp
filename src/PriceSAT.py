from BandPrice import Literal
from pysat.formula import WCNF
from pysat.examples.rc2 import RC2

from dnnf import dDNNF

def satsovler2price(cnf_fname, dnnf, tuple2price=None):
    if tuple2price is None:
            tuple2price = {v: (1 if v not in dnnf._forget_vars else 0) for v in dnnf._variables}
    with open(cnf_fname, 'r') as f:
        c = f.readlines()
    wcnf = WCNF()
    for clause in c:
        clause = clause.split()
        if clause[0] == 'p':
            var_num, clause_num = int(clause[2]), int(clause[3])
        else:
            current_var_num = len(clause) - 1
            clause_lit = []
            for i in range(current_var_num):
                clause_lit.append(int(clause[i]))
            wcnf.append(clause_lit)
    for v in dnnf._var2idx:
        wcnf.append([-v], weight = tuple2price[v])
    
    solver = RC2(wcnf)
    solution = solver.compute()

    return sum(tuple2price.get(v, 0) for v in solution if v > 0)
