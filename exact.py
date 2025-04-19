#!/usr/bin/env python3

import numpy as np
import pandas as pd
from collections import defaultdict, deque

VARGATE = 1
NEGGATE = 2
ORGATE = 3
ANDGATE = 4
TRUECONST = 5
FALSECONST = 6

class dDNNF:

    def __init__(self, circuit_filepath, forget_filepath=None):

        self._forget_vars = set()
        if forget_filepath is not None:
            forget_df = pd.read_csv(forget_filepath, delimiter=' ', header=None)
            self._forget_vars = set(forget_df[0].unique())

        self.__read_nnf__(circuit_filepath)
        vars = list(set([self._variables[gate] for gate in self._variables if self._gate_types[gate]==VARGATE]))
        self._var2idx = {v: i for i, v in enumerate(vars)}

    def __read_nnf__(self, filepath):
        file = open(filepath)
        self._children = {}  # children[i] is a list containing the children of gate i
        self._gate_types = {}  # gateType[i] is the type of gate i
        self._variables = {}  # when gateType[i] is VARGATE, variables[i] is the variable that this gate holds
        self._output_gate = -1

        current_gate = 0
        for line in file:
            parsed = [x.strip() for x in line.split(' ')]
            if parsed[0] == 'nnf':
                nbgates = int(parsed[1])
                self._output_gate = nbgates - 1
            elif parsed[0] == 'L':
                if abs(int(parsed[1])) in self._forget_vars:  # a literal we wish to forget
                    self._gate_types[current_gate] = TRUECONST
                    self._children[current_gate] = []
                else:
                    if int(parsed[1]) > 0:  # a positive literal
                        self._gate_types[current_gate] = VARGATE
                        self._variables[current_gate] = int(parsed[1])
                        self._children[current_gate] = []
                    elif int(parsed[1]) < 0:  # a negative literal. 
                        self._gate_types[current_gate] = NEGGATE
                        self._variables[current_gate] = int(parsed[1])
                        self._children[current_gate] = []
                current_gate += 1
            elif parsed[0] == 'A':
                if int(parsed[1]) == 0:  # this is a constant 1-gate
                    self._gate_types[current_gate] = TRUECONST
                    self._children[current_gate] = []
                else:  
                    self._gate_types[current_gate] = ANDGATE
                    self._children[current_gate] = []
                    for i in range(int(parsed[1])):
                        self._children[current_gate].append(int(parsed[i + 2]))
                current_gate += 1
            elif parsed[0] == 'O':
                if int(parsed[2]) == 0:  # this is a constant 0-gate
                    self._gate_types[current_gate] = FALSECONST
                    self._children[current_gate] = []
                else: 
                    self._gate_types[current_gate] = ORGATE
                    self._children[current_gate] = []
                    for i in range(int(parsed[2])):
                        self._children[current_gate].append(int(parsed[i + 3]))
                current_gate += 1

    def n_vars(self):
        return len(self._var2idx)

    def circuit2price(self, tuple2price=None):
        if tuple2price is None:
            tuple2price = {v: (1 if v not in self._forget_vars else 0) for v in self._var2idx}
        root_gate = self._output_gate
        gate2Price = dict.fromkeys(range(root_gate + 1), np.inf)
        upper_gate2Price = dict.fromkeys(range(root_gate + 1), np.inf)
        def sub_circuit2price(gate):
            if not self._children[gate]:
                if self._gate_types[gate] == VARGATE:
                    gate2Price[gate] = tuple2price[self._variables[gate]]
                    return gate2Price[gate]
                else:
                    gate2Price[gate] = 0
                    return 0
            if self._gate_types[gate] == ORGATE:
                for children_gate in self._children[gate]:
                    upper_gate2Price[children_gate] = gate2Price[gate]
                    gate2Price[children_gate] = sub_circuit2price(gate = children_gate)
                    gate2Price[gate] = min(gate2Price[children_gate], gate2Price[children_gate])
            elif self._gate_types[gate] == ANDGATE:
                currentPrice = 0
                for children_gate in self._children[gate]:
                    currentPrice += currentPrice + sub_circuit2price(gate = children_gate)
                    if currentPrice >= upper_gate2Price[gate]:
                        break
                gate2Price[gate] = currentPrice
            return gate2Price[gate]
        return sub_circuit2price(root_gate)
