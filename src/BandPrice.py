from dataclasses import dataclass
from math import log, sqrt
from random import randint
from dnnf import dDNNF
from time import time
from decimation import Decimation

MAX_INT = 10000000
MY_RAND_MAX_FLOAT = 10000000.0
MY_RAND_MAX_INT = 10000000
BASIC_SCALE = 0.0000001

cutoff_time = 1
backward_step = 21
gamma = 0.9
lambda_constant = 1.0
arm_num = 20
max_variable_score = 1000

max_tries = 100000000
max_flips = 200000000
non_improve_flip = 10000000

large_clause_count_threshold = 0
soft_large_variable_count_threshold = 0

rdprob = 0.01
hd_count_threshold = 15
rwprob = 0.1
smooth_probability = 0.01

h_inc = 300
soft_variable_weight_threshold = 500


@dataclass
class Literal:
    clause: int
    variable: int
    sense: int

class BandPrice:

    def __init__(self, cnf_fname, dnnf, tuple2price = None):
        if tuple2price is None:
            tuple2price = {v: (1 if v not in dnnf._forget_vars else 0) for v in dnnf._var2idx}
        with open(cnf_fname, 'r') as f:
            c = f.readlines()
        current_clause_num = 0
        for clause in c:
            clause = clause.split()
            if clause[0] == 'p':
                self.var_num, self.clause_num = int(clause[2]), int(clause[3])
                self.clause_lit = [[] for _ in range(self.clause_num)]
                self.clause_lit_count = [0 for _ in range(self.clause_num)]
                self.var_lit_count = [0 for v in range(self.var_num + 1)]
                self.var_lit = [[] for v in range(self.var_num + 1)]
                self.unit_clause_count = 0
                self.unit_clause = []
                self.binary_clause_count = 0
                self.binary_clause = []
                continue
            else:
                current_var_num = len(clause) - 1
                self.clause_lit_count[current_clause_num] = current_var_num
                for i in range(current_var_num):
                    v = int(clause[i])
                    if v != 0:
                        sense = 1 if v > 0 else 0
                        lit = Literal(current_clause_num, abs(v), sense)
                        self.clause_lit[current_clause_num].append(lit) 
                        self.var_lit[abs(v)].append(lit)
                        self.var_lit_count[abs(v)] += 1
                if current_var_num == 1:
                    self.unit_clause.append(self.clause_lit[current_clause_num][0])
                    self.unit_clause_count += 1
                elif current_var_num == 2:
                    self.binary_clause.append(current_clause_num)
                    self.binary_clause_count += 1
                current_clause_num += 1
        neighbor_flag = [0] * (self.var_num + 1)
        temp_neighbor = [0] * (self.var_num + 1)
        self.var_neighbor = [[] for v in range(self.var_num + 1)]
        self.var_neighbor_count = [0] * (self.var_num + 1)
        for v in range(1, self.var_num + 1):
            neighbor_flag[v] = 1
            temp_neighbor_count = 0
            for i in range(self.var_lit_count[v]):
                c = self.var_lit[v][i].clause
                for j in range(self.clause_lit_count[c]):
                    n = self.clause_lit[c][j].variable
                    if neighbor_flag[n] != 1:
                        neighbor_flag[n] = 1
                        temp_neighbor[temp_neighbor_count] = n
                        temp_neighbor_count += 1
            neighbor_flag[v] = 0
            self.var_neighbor_count[v] = temp_neighbor_count
            for i in range(temp_neighbor_count):
                self.var_neighbor[v].append(temp_neighbor[i])
                neighbor_flag[temp_neighbor[i]] = 0

        self.negative_variable_weight = [tuple2price[v] if v in tuple2price else 0 for v in range(self.var_num + 1)]
        

        self.goodvar_stack = []
        self.goodvar_stack_fill_pointer = 0
        self.already_in_goodvar_stack = [-1 for _ in range(self.var_num + 1)]

        self.cur_solution, self.best_solution, self.local_opt_solution = [], [-1 for _ in range(self.var_num + 1)], []
        self.best_solution_feasible, self.local_solution_feasible, self.hard_unsat_nb = 0, 0, 0
        self.soft_unsat_weight, self.opt_unsat_weight, self.local_opt_unsat_weight = 0, MAX_INT, MAX_INT

        self.soft_large_weight_variables = []
        self.soft_large_weight_variable_count, self.soft_large_variable_count_threshold = 0, 0

        self.best_array, self.temp_lit = [], []
        self.best_count = 0

        self.unassigned_hard_only_var, self.index_in_unassigned_hard_only_var = [], []
        self.unassigned_hard_only_var_num = 0

        self.index_in_hardunsat_stack, self.index_in_softunsat_stack  = [-1 for _ in range(self.clause_num)], [-1 for _ in range(self.var_num + 1)]
        self.hardunsat_stack, self.softunsat_stack = [-1 for _ in range(self.clause_num)], [-1 for _ in range(self.var_num + 1)]
        self.hardunsat_stack_fill_pointer, self.softunsat_stack_fill_pointer = 0, 0

        self.large_weight_clauses = []
        self.large_weight_clause_count = 0

        self.selected_variables = [-1 for _ in range(backward_step)]

        self.start_time = time()


    def init_from_init_solution(self, init_solution):
        self.local_times = 0
        self.if_exceed = 0
        self.soft_large_weight_variable_count = 0

        
        self.selected_times = [0 for _ in range(self.var_num + 1)]
        self.variable_score = [1 for _ in range(self.var_num + 1)]
        self.already_in_soft_large_weight_stack = [0 for _ in range(self.var_num + 1)]

        self.clause_weight = [1 for _ in range(self.clause_num)]

        self.variable_weight = [0 for _ in range(self.var_num + 1)]

        self.time_stamp = [0 for _ in range(self.var_num + 1)]

        self.cur_solution = [-1 for _ in range(self.var_num + 1)]

        
        
        for v in range(1, self.var_num + 1):
            if self.best_solution_feasible == 0:
                self.variable_weight[v] = 0
            else:
                self.variable_weight[v] = self.negative_variable_weight[v]
                if self.variable_weight[v] > 1 and self.already_in_soft_large_weight_stack[v] == 0:
                    self.already_in_soft_large_weight_stack[v] = 1
                    self.soft_large_weight_variables.append(v)
                    self.soft_large_weight_variable_count += 1

        if self.best_solution_feasible == 1:
            self.best_solution_feasible = 2
            for v in range(1, self.var_num + 1):
                self.time_stamp[v] = 0

        elif len(init_solution) == 0:
            for v in range(1, self.var_num + 1):
                self.cur_solution[v] = randint(0, 1) % 2
                self.time_stamp[v] = 0

        else:
            for v in range(1, self.var_num + 1):
                self.cur_solution[v] = init_solution[v]
                if self.cur_solution[v] != 0 and self.cur_solution[v] != 1:
                    self.cur_solution[v] = randint(0, 1) % 2
                self.time_stamp[v] = 0

        self.hard_unsat_nb = 0
        self.soft_unsat_weight = 0
        self.large_weight_clause_count = 0

        self.sat_count, self.sat_var = [0 for _ in range(self.clause_num)], [-1 for _ in range(self.clause_num)]
        self.score = [0 for _ in range(0, self.var_num + 1)]
        for c in range(0, self.clause_num):
            self.sat_count[c] = 0
            for j in range(0, self.clause_lit_count[c]):
                if self.cur_solution[self.clause_lit[c][j].variable] == self.clause_lit[c][j].sense:
                    self.sat_count[c] += 1
                    self.sat_var[c] = self.clause_lit[c][j].variable
            if self.sat_count[c] == 0:
                self.index_in_hardunsat_stack[c] = self.hardunsat_stack_fill_pointer
                self.hardunsat_stack[self.hardunsat_stack_fill_pointer] = c
                self.hardunsat_stack_fill_pointer += 1
                self.hard_unsat_nb += 1
        for v in range(self.var_num + 1):
            # is selected and not a forget variable
            if self.cur_solution[v] == 1 and self.negative_variable_weight[v] != 0:
                self.index_in_softunsat_stack[v] = self.softunsat_stack_fill_pointer
                self.softunsat_stack[self.softunsat_stack_fill_pointer] = v
                self.softunsat_stack_fill_pointer += 1
                self.soft_unsat_weight += self.negative_variable_weight[v]


        
            
        for v in range(1, self.var_num + 1):
            self.score[v] = 0
            for i in range(0, self.var_lit_count[v]):
                c = self.var_lit[v][i].clause
                if self.sat_count[c] == 0:
                    self.score[v] += self.clause_weight[c]
                elif self.sat_count[c] == 1 and self.var_lit[v][i].sense == self.cur_solution[v]:
                    self.score[v] -= self.clause_weight[c]
            if self.cur_solution[v] == 0:
                self.score[v] -= self.variable_weight[v]


        self.goodvar_stack_fill_pointer = 0
        for v in range(1, self.var_num + 1):
            if self.score[v] > 0:
                self.already_in_goodvar_stack[v] = self.goodvar_stack_fill_pointer
                self.goodvar_stack.append(v)
                self.goodvar_stack_fill_pointer += 1
            else:
                self.already_in_goodvar_stack[v] = -1
    
    def smooth_weights(self):
        for i in range(0, self.large_weight_clause_count):
            clause = self.large_weight_clauses[i]
            if self.sat_count[clause] > 0:
                self.clause_weight[clause] -= h_inc
                if self.clause_weight[clause] == 1:
                    self.large_weight_clauses[i] = self.large_weight_clauses[self.large_weight_clause_count - 1]
                    self.large_weight_clause_count -= 1
                    i -= 1
                if self.sat_count[clause] == 1:
                    v = self.sat_var[clause]
                    self.score[v] += h_inc
                    if self.score[v] > 0 and self.already_in_goodvar_stack[v] == -1:
                        self.already_in_goodvar_stack[v] = self.goodvar_stack_fill_pointer
                        self.goodvar_stack.append(v)
                        self.goodvar_stack_fill_pointer += 1
        if self.best_solution_feasible > 0:
            for i in range(self.soft_large_weight_variable_count):
                v = self.soft_large_weight_variables[i]
                if self.cur_solution[v] == 0 and self.negative_variable_weight[v] > 0:
                    self.variable_weight[v] -= 1
                    if self.variable_weight[v] == 1 and self.already_in_soft_large_weight_stack[v] == 1:
                        self.already_in_soft_large_weight_stack[v] = 0
                        self.soft_large_weight_variables[i] = self.soft_large_weight_variables[self.soft_large_weight_variable_count - 1]
                        self.soft_large_weight_variable_count -= 1
                        i -= 1
                    if self.cur_solution[v] == 0 and self.negative_variable_weight[v] > 0:
                        self.score[v] += 1
                        if self.score[v] > 0 and self.already_in_goodvar_stack[v] != -1:
                            self.already_in_goodvar_stack[v] = self.goodvar_stack_fill_pointer
                            self.goodvar_stack.append(v)
                            self.goodvar_stack_fill_pointer += 1
                        
                        
    def increase_weights(self):
        for i in range(0, self.hardunsat_stack_fill_pointer):
            c = self.hardunsat_stack[i]
            self.clause_weight[c] += h_inc
            
            if self.clause_weight[c] == h_inc + 1:
                self.large_weight_clauses.append(c)
                self.large_weight_clause_count += 1
            
            for p in self.clause_lit[c]:
                v = p.variable
                self.score[v] += h_inc
                if self.score[v] > 0 and self.already_in_goodvar_stack[v] == -1:
                    self.already_in_goodvar_stack[v] = self.goodvar_stack_fill_pointer
                    self.goodvar_stack.append(v)
                    self.goodvar_stack_fill_pointer += 1
        if self.best_solution_feasible > 0:
            for i in range(0, self.softunsat_stack_fill_pointer):
                v = self.softunsat_stack[i]
                if self.variable_weight[v] > soft_variable_weight_threshold:
                    continue
                else:
                    self.variable_weight[v] += 1
                if self.variable_weight[v] > 1 and self.already_in_soft_large_weight_stack[v] == 0:
                    self.already_in_soft_large_weight_stack[v] = 1
                    self.soft_large_weight_variables.append(v)
                    self.soft_large_weight_variable_count += 1
                self.score[v] += 1
                if self.score[v] > 0 and self.already_in_goodvar_stack[v] == -1:
                    self.already_in_goodvar_stack[v] = self.goodvar_stack_fill_pointer
                    self.goodvar_stack.append(v)
                    self.goodvar_stack_fill_pointer += 1



    def update_clause_weights(self):
        if (randint(0, MY_RAND_MAX_INT - 1) % MY_RAND_MAX_INT) * BASIC_SCALE < smooth_probability and self.large_weight_clause_count > large_clause_count_threshold:
            self.smooth_weights()
        else:
            self.increase_weights()



    def pick_var(self):
        if self.goodvar_stack_fill_pointer > 0:
            if (randint(0, MY_RAND_MAX_INT - 1) % MY_RAND_MAX_INT) * BASIC_SCALE < rdprob:
                return self.goodvar_stack[randint(0, self.goodvar_stack_fill_pointer - 1) % self.goodvar_stack_fill_pointer]
            else:
                tabu_step = randint(0, 2) % 3
                best_score = -1
                if self.goodvar_stack_fill_pointer < hd_count_threshold:
                    for i in range(0, self.goodvar_stack_fill_pointer):
                        v = self.goodvar_stack[i]
                        if self.time_stamp[v] == 0 and self.step - self.time_stamp[v] > tabu_step:
                            if self.score[v] > best_score:
                                best_var = v
                                best_score = self.score[v]
                            elif self.time_stamp[v] < self.time_stamp[best_var]:
                                best_var = v
                        if best_score > 0:
                            return best_var
                else:
                    for i in range(0, hd_count_threshold):
                        v = self.goodvar_stack[randint(0, self.goodvar_stack_fill_pointer - 1) % self.goodvar_stack_fill_pointer]
                        if self.time_stamp[v] == 0 and self.step - self.time_stamp[v] > tabu_step:
                            if self.score[v] > best_score:
                                best_var = v
                                best_score = self.score[v]
                            elif self.time_stamp[v] < self.time_stamp[best_var]:
                                best_var = v
                        if best_score > 0:
                            return best_var

        self.update_clause_weights()

        if self.hardunsat_stack_fill_pointer > 0:
            sel_c = self.hardunsat_stack[randint(0, self.hardunsat_stack_fill_pointer - 1) % self.hardunsat_stack_fill_pointer]
            if randint(0, MY_RAND_MAX_INT - 1) % BASIC_SCALE < rwprob:
                return self.clause_lit[sel_c][randint(0, self.clause_lit_count[sel_c] - 1) % self.clause_lit_count[sel_c]].variable
            else:
                best_var = self.clause_lit[sel_c][0].variable
                for p in self.clause_lit[sel_c]:
                    v = p.variable
                    if self.score[v] > self.score[best_var]:
                        best_var = v
                    elif self.score[v] == self.score[best_var]:
                        if self.time_stamp[v] < self.time_stamp[best_var]:
                            best_var = v
                return best_var
        else:
            sampled_variables = []
            sampled_variables.append(self.softunsat_stack[randint(0, self.softunsat_stack_fill_pointer - 1) % self.softunsat_stack_fill_pointer])
            min_score, max_score = self.score[sampled_variables[0]], self.score[sampled_variables[0]]
            for i in range(1, arm_num):
                sampled_variables.append(self.softunsat_stack[randint(0, self.softunsat_stack_fill_pointer - 1) % self.softunsat_stack_fill_pointer])
                if self.score[sampled_variables[i]] < min_score:
                    min_score = self.score[sampled_variables[i]]
                if self.score[sampled_variables[i]] > max_score:
                    max_score = self.score[sampled_variables[i]]
            if max_score == min_score:
                best_var = sampled_variables[0]
                for i in range(1, arm_num):
                    if self.selected_times[sampled_variables[i]] < self.selected_times[best_var]:
                        best_var = sampled_variables[i]
                    elif self.selected_times[sampled_variables[i]] == self.selected_times[best_var]:
                        if self.variable_weight[sampled_variables[i]] > self.variable_weight[best_var]:
                            best_var = sampled_variables[i]
            else:
                max_value = self.variable_score[sampled_variables[0]]/self.negative_variable_weight[sampled_variables[0]] + lambda_constant * sqrt(log(self.local_times + 1)/(self.selected_times[sampled_variables[0]] + 1))
                best_var = sampled_variables[0]
                for i in range(1, arm_num):
                    dtemp = self.variable_score[sampled_variables[i]]/self.negative_variable_weight[sampled_variables[i]] + lambda_constant * sqrt(log(self.local_times + 1)/(self.selected_times[sampled_variables[i]] + 1))
                    if dtemp > max_value:
                        max_value = dtemp
                        best_var = sampled_variables[i]
            self.selected_times[best_var] += 1
            self.selected_variables[self.local_times % backward_step] = best_var
            if self.local_times > 0:
                s = self.pre_unsat_weight - self.soft_unsat_weight
                self.update_variable_scores(s)
            self.pre_unsat_weight = self.soft_unsat_weight
            self.local_times += 1
            return best_var

    def update_variable_scores(self, s):
        opt = self.opt_unsat_weight
        if self.soft_unsat_weight < opt:
            opt = self.soft_unsat_weight
        
        stemp = s / (self.pre_unsat_weight - opt + 1)

        if self.local_times < backward_step:
            for i in range(self.local_times):
                discount = pow(gamma, self.local_times - 1)
                self.variable_score[self.selected_variables[i]] += discount * stemp
                if abs(self.variable_score[self.selected_variables[i]]) > max_variable_score:
                    self.if_exceed = 1
        
        else:
            for i in range(0, backward_step):
                if i == self.local_times % backward_step:
                    continue
                if i < self.local_times % backward_step:
                    discount = pow(gamma, self.local_times % backward_step - 1 - i)
                else:
                    discount = pow(gamma, self.local_times % backward_step + backward_step - 1 - i)
                if abs(self.variable_score[self.selected_variables[i]]) > max_variable_score:
                    self.if_exceed = 1
            if self.if_exceed:
                for i in range(0, self.clause_num):
                    self.variable_score[i] = self.variable_score[i] / 2.0
                self.if_exceed = 0

    def update_goodvarstack(self, flipvar):
        for idx in range(self.goodvar_stack_fill_pointer - 1, -1, -1):
            v = self.goodvar_stack[idx]
            if self.score[v] <= 0:
                self.goodvar_stack.pop()
                self.goodvar_stack_fill_pointer -= 1
                self.already_in_goodvar_stack[v] = -1
        
        for i in range(0, self.var_neighbor_count[flipvar]):
            v = self.var_neighbor[flipvar][i]
            if self.score[v] > 0:
                if self.already_in_goodvar_stack[v] == -1:
                    self.already_in_goodvar_stack[v] = self.goodvar_stack_fill_pointer
                    self.goodvar_stack.append(v)
                    self.goodvar_stack_fill_pointer += 1
                


    def flip(self, flipvar):
        org_flipvar_score = self.score[flipvar]
        self.cur_solution[flipvar] = 1 - self.cur_solution[flipvar]

        for i in range(0, self.var_lit_count[flipvar]):
            c = self.var_lit[flipvar][i].clause
            if self.cur_solution[flipvar] == self.var_lit[flipvar][i].sense:
                self.sat_count[c] += 1
                if self.sat_count[c] == 2:
                    self.score[self.sat_var[c]] += self.clause_weight[c]
                elif self.sat_count[c] == 1:
                    self.sat_var[c] = flipvar
                    for p in self.clause_lit[c]:
                        v = p.variable
                        self.score[v] -= self.clause_weight[c]
                    last_unsat_clause = self.hardunsat_stack[self.hardunsat_stack_fill_pointer - 1]
                    self.hardunsat_stack_fill_pointer -= 1
                    idx = self.index_in_hardunsat_stack[c]
                    self.hardunsat_stack[idx] = last_unsat_clause
                    self.index_in_hardunsat_stack[last_unsat_clause] = idx
                    self.hard_unsat_nb -= 1
            else:
                self.sat_count[c] -= 1
                if self.sat_count[c] == 1:
                    for p in self.clause_lit[c]:
                        v = p.variable
                        if p.sense == self.cur_solution[v]:
                            self.score[v] -= self.clause_weight[c]
                            self.sat_var[c] = v
                            break
                elif self.sat_count[c] == 0:
                    for p in self.clause_lit[c]:
                        v = p.variable
                        self.score[v] += self.clause_weight[c]
                    self.index_in_hardunsat_stack[c] = self.hardunsat_stack_fill_pointer
                    self.hardunsat_stack[self.hardunsat_stack_fill_pointer] = c
                    self.hardunsat_stack_fill_pointer += 1
                    self.hard_unsat_nb += 1

        if self.cur_solution[flipvar] == 0:
            last_unsat_var = self.softunsat_stack[self.softunsat_stack_fill_pointer - 1]
            self.softunsat_stack_fill_pointer -= 1
            idx = self.index_in_softunsat_stack[last_unsat_var]
            self.softunsat_stack[idx] = last_unsat_var
            self.index_in_softunsat_stack[last_unsat_var] = idx
            self.soft_unsat_weight -= self.negative_variable_weight[flipvar]
        
        elif self.cur_solution[flipvar] == 1:
            self.index_in_softunsat_stack[flipvar] = self.softunsat_stack_fill_pointer
            self.softunsat_stack[self.softunsat_stack_fill_pointer] = flipvar
            self.softunsat_stack_fill_pointer += 1
            self.soft_unsat_weight += self.negative_variable_weight[flipvar] 



        self.score[flipvar] = - org_flipvar_score
        self.update_goodvarstack(flipvar)

    def get_runtime(self):
        return time() - self.start_time

    def local_search(self):
        init_solution = []
        for tries in range(max_tries):
            self.init_from_init_solution(init_solution)
            for step in range(1, max_flips):
                self.step = step
                if self.hard_unsat_nb == 0 and (self.soft_unsat_weight < self.opt_unsat_weight or self.best_solution_feasible == 0):
                    if self.soft_unsat_weight < self.opt_unsat_weight:
                        self.best_solution_feasible = 1
                        self.opt_unsat_weight = self.soft_unsat_weight
                        opt_time = self.get_runtime()
                        for v in range(1, self.var_num + 1):
                            self.best_solution[v] = self.cur_solution[v]
                if step % 1000 == 0:
                    elapse_time = self.get_runtime()
                    if elapse_time >= cutoff_time:
                        return
                flipvar = self.pick_var()
                self.flip(flipvar)
                self.time_stamp[flipvar] = step
    
    def local_search_with_decimation(self):
        deci = Decimation(self.var_lit, self.var_lit_count, self.clause_lit,self.clause_lit_count, self.unit_clause, self.unit_clause_count, self.binary_clause, self.binary_clause_count, self.negative_variable_weight)
        opt_unsat_weight = MAX_INT
        for tries in range(max_tries):
            if self.best_solution_feasible != 1:
                deci.unit_process()
                self.init_from_init_solution(deci.fix)
            else:
                self.init_from_init_solution(deci.fix)
            
            local_opt = MAX_INT
            for step in range(1, max_flips):
                self.step = step
                if self.hard_unsat_nb == 0:
                    if local_opt > self.soft_unsat_weight:
                        local_opt = self.soft_unsat_weight
                    if self.soft_unsat_weight < self.opt_unsat_weight:
                        self.opt_unsat_weight = self.soft_unsat_weight
                        opt_time = self.get_runtime()
                        for v in range(1, self.var_num + 1):
                            self.best_solution[v] = self.cur_solution[v]
                    if self.best_solution_feasible == 0:
                        self.best_solution_feasible = 1
                if step % 1000 == 0:
                    elapse_time = self.get_runtime()
                    if elapse_time >= cutoff_time:
                        return
                flipvar = self.pick_var()
                self.flip(flipvar)
                self.time_stamp[flipvar] = step
