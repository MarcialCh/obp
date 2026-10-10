#!/usr/bin/env python3
"""KCPrice for a joint, monotone query-answer provenance circuit.

Based on Section III, Eq. (5), and Section IV-A / Algorithm 1 of the
the accompanying manuscript.
No Shapley computation, SAT-price optimizer, or tropical evaluation of an
uncompiled provenance graph is substituted for knowledge compilation.

Python >= 3.8; the core uses only the standard library. c2d is external.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

INF = math.inf
VERSION = "1.1.0"


class KCError(RuntimeError):
    pass


class DeadlineExceeded(KCError):
    pass


class Deadline:
    def __init__(self, seconds: float):
        if not math.isfinite(seconds) or seconds <= 0:
            raise KCError("Timeout must be a finite positive number of seconds")
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        value = self.end - time.monotonic()
        if value <= 0:
            raise DeadlineExceeded("KCPrice time budget exhausted")
        return value

    def check(self) -> None:
        self.remaining()


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    os.replace(str(temp), str(path))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1048576), b""):
            h.update(part)
    return h.hexdigest()


@dataclass(frozen=True)
class Gate:
    kind: str
    children: Tuple[str, ...] = ()


class ProvenanceGraph:
    """Canonical Boolean view of the positive provenance fragment.

    A repeated source UUID is ONE variable across every answer and occurrence.
    Unary project/eq/delta gates are Boolean-transparent in the supported
    positive relational fragment. Unknown and nonmonotone gates fail closed.
    """
    ALIASES = {
        "input": "input", "plus": "or", "or": "or",
        "times": "and", "and": "and", "one": "true", "true": "true",
        "zero": "false", "false": "false", "project": "alias",
        "eq": "alias", "delta": "alias", "annotation": "alias",
        "alias": "alias",
    }

    def __init__(self, nodes: Dict[str, dict], roots: List[str]):
        self.roots = tuple(dict.fromkeys(str(r) for r in roots))
        self.nodes = {}  # type: Dict[str, Gate]
        for token, record in nodes.items():
            kind = str(record.get("type", record.get("kind", ""))).lower()
            # ProvSQL metadata wrappers are accepted only for Boolean semantics.
            if kind == "assumed":
                if record.get("extra") not in (None, "", "boolean"):
                    raise KCError("Non-Boolean assumed gate is not supported: %s" % token)
                kind = "alias"
            if kind not in self.ALIASES:
                raise KCError("Unsupported provenance gate %r at %s" % (kind, token))
            canonical = self.ALIASES[kind]
            children = tuple(dict.fromkeys(str(c) for c in record.get("children", []) or []))
            if canonical in ("input", "true", "false") and children:
                raise KCError("Leaf %s unexpectedly has children" % token)
            if canonical == "alias" and len(children) != 1:
                raise KCError("Transparent gate %s requires exactly one child" % token)
            self.nodes[str(token)] = Gate(canonical, children)
        self.order = self._postorder()
        self.nodes = {t: self.nodes[t] for t in self.order}
        self.inputs = tuple(sorted(t for t in self.order if self.nodes[t].kind == "input"))

    def _postorder(self) -> List[str]:
        state = {}  # type: Dict[str, int]
        order = []
        for root in self.roots:
            if state.get(root) == 2:
                continue
            stack = [(root, False)]
            while stack:
                token, done = stack.pop()
                if token not in self.nodes:
                    raise KCError("Missing provenance gate: %s" % token)
                if done:
                    state[token] = 2
                    order.append(token)
                    continue
                mark = state.get(token, 0)
                if mark == 2:
                    continue
                if mark == 1:
                    raise KCError("Cyclic provenance is outside this implementation's scope")
                state[token] = 1
                stack.append((token, True))
                for child in reversed(self.nodes[token].children):
                    if state.get(child, 0) == 1:
                        raise KCError("Cycle in provenance at %s" % child)
                    if state.get(child, 0) != 2:
                        stack.append((child, False))
        return order

    def values(self, selected: Set[str]) -> Dict[str, bool]:
        result = {}
        for token in self.order:
            gate = self.nodes[token]
            if gate.kind == "input":
                value = token in selected
            elif gate.kind == "true":
                value = True
            elif gate.kind == "false":
                value = False
            elif gate.kind == "or":
                value = any(result[c] for c in gate.children)
            else:  # and / unary alias
                value = all(result[c] for c in gate.children)
            result[token] = value
        return result

    def satisfied(self, selected: Set[str]) -> bool:
        values = self.values(selected)
        return all(values[r] for r in self.roots)


class TseitinEncoding:
    """Full biconditional encoding, with all answer roots asserted TRUE."""
    def __init__(self, graph: ProvenanceGraph):
        self.graph = graph
        self.ids = {token: i + 1 for i, token in enumerate(graph.inputs)}
        self.ninputs = len(graph.inputs)
        for token in graph.order:
            if token not in self.ids:
                self.ids[token] = len(self.ids) + 1
        self.nvars = len(self.ids)

    def clauses(self):
        for token in self.graph.order:
            gate = self.graph.nodes[token]
            v = self.ids[token]
            kids = [self.ids[c] for c in gate.children]
            if gate.kind == "input":
                continue
            if gate.kind == "true":
                yield (v,)
            elif gate.kind == "false":
                yield (-v,)
            elif gate.kind == "or":
                yield tuple([-v] + kids)
                for x in kids:
                    yield (v, -x)
            else:  # AND or identity
                yield tuple([v] + [-x for x in kids])
                for x in kids:
                    yield (-v, x)
        # This is the conjunction over ALL answer provenances, Eq. (5).
        for root in self.graph.roots:
            yield (self.ids[root],)

    def write(self, path: Path, deadline: Optional[Deadline] = None) -> int:
        # Each non-input gate emits len(children)+1 clauses. Count these
        # without serializing, then write once instead of copying a body file.
        if deadline:
            deadline.check()
        count = len(self.graph.roots)
        for i, token in enumerate(self.graph.order):
            if deadline and i % 8192 == 0:
                deadline.check()
            gate = self.graph.nodes[token]
            if gate.kind != "input":
                count += len(gate.children) + 1
        with Path(path).open("w", encoding="ascii", buffering=1048576) as stream:
            stream.write("c KCPrice: original tuple IDs are 1..%d\n" % self.ninputs)
            stream.write("p cnf %d %d\n" % (self.nvars, count))
            lines = []
            units = set()
            for emitted, clause in enumerate(self.clauses(), 1):
                if units is not None:
                    if len(clause) == 1:
                        units.add(clause[0])
                    else:
                        units = None
                lines.append(" ".join(map(str, clause)) + " 0\n")
                if emitted % 4096 == 0:
                    if deadline:
                        deadline.check()
                    stream.write("".join(lines))
                    lines.clear()
            stream.write("".join(lines))
        self.unit_clauses = units
        if deadline:
            deadline.check()
        return count


class Prices:
    """Exact, nonnegative finite decimal prices, converted to integer units."""
    def __init__(self, inputs: Tuple[str, ...], prices: Dict[str, object]):
        amounts = []
        digits = 0
        for token in inputs:
            if token not in prices:
                raise KCError("No tuple price for %s" % token)
            try:
                value = Decimal(str(prices[token]))
            except (InvalidOperation, ValueError) as exc:
                raise KCError("Invalid price for %s" % token) from exc
            if not value.is_finite() or value < 0:
                raise KCError("Prices must be finite and nonnegative: %s" % token)
            exponent = value.as_tuple().exponent
            if abs(exponent) > 12 or len(value.as_tuple().digits) > 30:
                raise KCError("Price needs <=12 decimal places and <=30 significant digits")
            digits = max(digits, -exponent)
            amounts.append(value)
        self.digits = digits
        self.scale = 10 ** digits
        self.units = {}  # type: Dict[int, int]
        self.text = {}
        for i, (token, value) in enumerate(zip(inputs, amounts), 1):
            parts = value.as_tuple()
            coefficient = int("".join(map(str, parts.digits)) or "0")
            units = coefficient * (10 ** (parts.exponent + digits))
            self.units[i] = units
            self.text[token] = self.format(units)

    def format(self, value: int) -> str:
        value = int(value)
        if self.digits == 0:
            return str(value)
        return "%d.%0*d" % (value // self.scale, self.digits, value % self.scale)


class NNFNode(NamedTuple):
    """Immutable tuple record avoids a per-node instance dictionary."""
    kind: str
    literal: int = 0
    children: Tuple[int, ...] = ()


class NNF:
    def __init__(self, nodes: List[NNFNode], variables: int, edges: Optional[int] = None):
        if not nodes:
            raise KCError("An NNF circuit must have a root node")
        self.nodes = nodes
        self.root = len(nodes) - 1
        self.variables = variables
        self.edges = sum(len(n.children) for n in nodes) if edges is None else edges

    @classmethod
    def read(cls, path: Path, ninputs: int, nvars: int,
             deadline: Optional[Deadline] = None):
        nodes = []
        header = None
        edges = 0
        forgotten = 0
        true_node = NNFNode("T")
        false_node = NNFNode("F")
        if deadline:
            deadline.check()
        with Path(path).open("r", encoding="ascii", buffering=1048576) as stream:
            for lineno, line in enumerate(stream, 1):
                if deadline and lineno % 8192 == 0:
                    deadline.check()
                fields = line.split()
                if not fields or fields[0] in ("c", "#"):
                    continue
                if header is None:
                    if fields[0] != "nnf" or len(fields) != 4:
                        raise KCError("Expected c2d NNF header 'nnf nodes edges variables'")
                    header = tuple(map(int, fields[1:]))
                    if header[2] > nvars:
                        raise KCError("Compiler output has unexpected variable IDs")
                    continue
                kind = fields[0]
                try:
                    if kind == "L" and len(fields) == 2:
                        lit = int(fields[1])
                        if not 1 <= abs(lit) <= nvars:
                            raise ValueError("literal out of range")
                        if abs(lit) > ninputs:
                            # Algorithm 1, line 1: existential forgetting on DNNF.
                            node = true_node
                            forgotten += 1
                        else:
                            node = NNFNode("L", literal=lit)
                    elif kind == "A" and len(fields) >= 2:
                        kids = tuple(map(int, fields[2:]))
                        if len(kids) != int(fields[1]):
                            raise ValueError("AND arity mismatch")
                        node = NNFNode("A", children=kids) if kids else true_node
                    elif kind == "O" and len(fields) >= 3:
                        # fields[1] is c2d's decision-variable annotation.
                        kids = tuple(map(int, fields[3:]))
                        if len(kids) != int(fields[2]):
                            raise ValueError("OR arity mismatch")
                        node = NNFNode("O", children=kids) if kids else false_node
                    else:
                        raise ValueError("unsupported NNF record")
                except ValueError as exc:
                    raise KCError("Invalid NNF line %d: %s" % (lineno, line.rstrip())) from exc
                if node.children and (min(node.children) < 0 or max(node.children) >= len(nodes)):
                    raise KCError("NNF is not children-before-parent at line %d" % lineno)
                edges += len(node.children)
                nodes.append(node)
        if header is None or len(nodes) != header[0] or edges != header[1]:
            raise KCError("Truncated NNF or inconsistent node/edge counts")
        if deadline:
            deadline.check()
        result = cls(nodes, nvars, edges=edges)
        result.forgotten_occurrences = forgotten
        return result

    def check_decomposable(self) -> None:
        """Optional expensive diagnostic; bitset memory can be large."""
        scopes = []
        for i, node in enumerate(self.nodes):
            scope = 0
            if node.kind == "L":
                scope = 1 << (abs(node.literal) - 1)
            else:
                for child in node.children:
                    if node.kind == "A" and scope & scopes[child]:
                        raise KCError("Nondecomposable AND at NNF node %d" % i)
                    scope |= scopes[child]
            scopes.append(scope)


class PriceEvaluator:
    """Iterative Algorithm 1 with context-safe upper-bound pruning.

    Only internal gates allocate traversal frames. Literal/cache returns go
    straight to their parent, avoiding a generator and StopIteration per edge.
    Exact prices and interrupted lower bounds remain in separate memo tables.
    """
    def __init__(self, circuit: NNF, costs: Dict[int, int],
                 deadline: Optional[Deadline] = None, prune: bool = True):
        self.circuit = circuit
        self.costs = costs
        self.deadline = deadline
        self.prune = prune
        self.memo = {}
        self.lower = {}
        self.choice = {}
        self.stats = {"node_calls": 0, "cache_hits": 0, "bound_cuts": 0,
                      "and_early_stops": 0, "or_zero_stops": 0}

    def run(self):
        nodes = self.circuit.nodes
        costs = self.costs
        memo = self.memo
        lower = self.lower
        choice = self.choice
        memo_get = memo.get
        lower_get = lower.get
        prune = self.prune
        deadline = self.deadline
        stats = self.stats
        calls = stats["node_calls"]
        hits = stats["cache_hits"]
        cuts = stats["bound_cuts"]
        and_stops = stats["and_early_stops"]
        zero_stops = stats["or_zero_stops"]
        missing = object()
        root = self.circuit.root
        index, bound = root, INF
        # [index, bound, children, next_position, value, chosen_child, is_and]
        stack = []
        if deadline:
            deadline.check()
        try:
            while True:
                calls += 1
                if deadline and calls % 4096 == 0:
                    deadline.check()
                value = memo_get(index, missing)
                exact = True
                if value is not missing:
                    hits += 1
                elif prune and bound != INF and lower_get(index, 0) >= bound:
                    value = lower_get(index, 0)
                    cuts += 1
                    exact = False
                else:
                    node = nodes[index]
                    kind = node.kind
                    if kind == "L":
                        literal = node.literal
                        value = costs[literal] if literal > 0 else 0
                    elif kind == "T":
                        value = 0
                    elif kind == "F":
                        value = INF
                    else:
                        children = node.children
                        is_and = kind == "A"
                        value = 0 if is_and else (bound if prune else INF)
                        if children:
                            stack.append([index, bound, children, 1, value, None, is_and])
                            index = children[0]
                            bound = bound if prune else INF
                            continue
                        if not is_and and prune and bound != INF:
                            lower[index] = max(lower_get(index, 0), bound)
                            exact = False
                    if exact:
                        memo[index] = value

                # Return this child result directly through completed parents.
                while stack:
                    frame = stack[-1]
                    parent, parent_bound, children, pos, total, chosen, is_and = frame
                    if is_and:
                        total += value
                        if exact and value == INF:
                            value, exact = INF, True
                            memo[parent] = INF
                        elif not exact or (prune and total >= parent_bound and pos < len(children)):
                            and_stops += 1
                            lower[parent] = max(lower_get(parent, 0), total)
                            value, exact = total, False
                        elif pos < len(children):
                            frame[3] = pos + 1
                            frame[4] = total
                            index = children[pos]
                            bound = parent_bound - total if prune and parent_bound != INF else INF
                            break
                        else:
                            value, exact = total, True
                            memo[parent] = total
                    else:
                        if value < total:
                            if not exact:
                                raise KCError("Internal error: a pruned bound improved an incumbent")
                            total, chosen = value, children[pos - 1]
                        if prune and chosen is not None and total == 0:
                            zero_stops += 1
                        elif pos < len(children):
                            frame[3] = pos + 1
                            frame[4] = total
                            frame[5] = chosen
                            index = children[pos]
                            bound = total if prune else INF
                            break
                        if chosen is None and prune and parent_bound != INF:
                            lower[parent] = max(lower_get(parent, 0), parent_bound)
                            value, exact = parent_bound, False
                        else:
                            value, exact = total, True
                            memo[parent] = total
                            if chosen is not None:
                                choice[parent] = chosen
                    stack.pop()
                else:
                    break
        finally:
            stats.update(node_calls=calls, cache_hits=hits, bound_cuts=cuts,
                         and_early_stops=and_stops, or_zero_stops=zero_stops)
        if not exact:
            raise KCError("Root price was not computed exactly")
        if deadline:
            deadline.check()
        if value == INF:
            return INF, set()
        selected = set()
        seen = set()
        todo = [root]
        visited = 0
        while todo:
            index = todo.pop()
            if index in seen:
                continue
            seen.add(index)
            visited += 1
            if deadline and visited % 4096 == 0:
                deadline.check()
            node = nodes[index]
            if node.kind == "L" and node.literal > 0:
                selected.add(node.literal)
            elif node.kind == "A":
                todo.extend(node.children)
            elif node.kind == "O":
                if index not in choice:
                    raise KCError("Missing optimal OR choice")
                todo.append(choice[index])
        if deadline:
            deadline.check()
        return value, selected


def executable(name: str) -> str:
    location = shutil.which(os.path.expanduser(name))
    if not location:
        raise KCError("Executable not found: %s. Set C2D_BIN to your c2d executable." % name)
    return str(Path(location).resolve())


def run_process(command: List[str], log: Path, seconds: float, cwd: Optional[Path] = None) -> None:
    """Run trusted local executables; never invoke a shell or leave timed-out children."""
    with Path(log).open("wb") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                   cwd=str(cwd) if cwd else None,
                                   start_new_session=os.name != "nt",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            status = process.wait(timeout=seconds)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            if os.name == "nt":
                # c2d is a native process; taskkill also covers any child it
                # may have launched. Never use the POSIX-only os.killpg here.
                if process.poll() is None:
                    try:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    pass
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise DeadlineExceeded("Process timed out; details in %s" % log)
    if status != 0:
        raise KCError("%s exited with code %d; see %s" % (command[0], status, log))


def _write_unit_nnf(path, units, nvars):
    """Compile a unit-only CNF exactly into a decomposable conjunction.

    This also avoids a c2d 2.20 Windows hang on a one-clause input. No prices
    enter this compilation; the ordinary NNF evaluator still computes them.
    """
    with Path(path).open("w", encoding="ascii") as stream:
        if any(-literal in units for literal in units):
            stream.write("nnf 1 0 %d\nO 0 0\n" % nvars)
        elif not units:
            stream.write("nnf 1 0 %d\nA 0\n" % nvars)
        else:
            literals = sorted(units, key=lambda lit: (abs(lit), lit))
            size = len(literals)
            stream.write("nnf %d %d %d\n" % (size + (size > 1), size if size > 1 else 0, nvars))
            for literal in literals:
                stream.write("L %d\n" % literal)
            if size > 1:
                stream.write("A %d %s\n" % (size, " ".join(map(str, range(size)))))


def _atomic_copy(source, destination, deadline):
    destination = Path(destination)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(destination.parent), delete=False) as output:
            temp = Path(output.name)
            with Path(source).open("rb") as stream:
                while True:
                    deadline.check()
                    block = stream.read(1048576)
                    if not block:
                        break
                    output.write(block)
        os.replace(str(temp), str(destination))
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def _cache_restore(directory, key, destination, deadline):
    """A miss or damaged cache never supplies an unverified artifact."""
    try:
        metadata = json.loads((directory / (key + ".json")).read_text(encoding="utf-8"))
        if metadata.get("key") != key or not isinstance(metadata.get("nnf_sha256"), str):
            return False
        _atomic_copy(directory / (key + ".nnf"), destination, deadline)
        valid = sha256_file(destination) == metadata["nnf_sha256"]
        if not valid:
            destination.unlink()
        deadline.check()
        return valid
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _cache_store(directory, key, source, deadline):
    directory.mkdir(parents=True, exist_ok=True)
    digest = sha256_file(source)
    _atomic_copy(source, directory / (key + ".nnf"), deadline)
    # A unique metadata temporary file avoids collisions between worker runs.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(directory), delete=False) as stream:
            temp = Path(stream.name)
            json.dump({"key": key, "nnf_sha256": digest}, stream)
        os.replace(str(temp), str(directory / (key + ".json")))
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def solve(graph: ProvenanceGraph, raw_prices: Dict[str, object], out: Path,
          c2d: str, timeout: float = 100, prune: bool = True,
          check_decomposable: bool = False, metadata: Optional[dict] = None,
          dt_count: Optional[int] = 1, dt_method: Optional[int] = None,
          project_aux: bool = True, cache_dir: Optional[Path] = None) -> dict:
    """Compile and price the joint formula; all previous positional arguments remain valid.

    dt_count=1 avoids c2d's default 25 randomized dtree constructions. Hard
    instances can benefit from increasing it. Optional cache_dir reuses only
    compiled NNFs with identical CNF, compiler binary and compiler settings;
    prices and selected supports are always recomputed.
    """
    if dt_count is not None and (isinstance(dt_count, bool) or not isinstance(dt_count, int) or dt_count < 1):
        raise KCError("dt_count must be a positive integer or None")
    if dt_method is not None and (isinstance(dt_method, bool) or not isinstance(dt_method, int) or dt_method not in range(5)):
        raise KCError("dt_method must be 0, 1, 2, 3, 4 or None")
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any((out / f).exists() for f in ("formula.cnf", "formula.cnf.nnf", "result.json")):
        raise KCError("Refusing to reuse a previous solver directory: %s" % out)
    started = time.perf_counter()
    deadline = Deadline(timeout)
    result = dict(metadata or {})
    result.update(algorithm="KCPrice", implementation_version=VERSION,
                  status="STARTED", price=None, timeout_scope="CNF+compile+read+price+verify+cache",
                  n_answer_roots=len(graph.roots), n_tuples=len(graph.inputs),
                  n_provenance_gates=len(graph.nodes), pruning=prune,
                  compilation_cache="disabled", compiler_invoked=False,
                  compiler_options=dict(dt_count=dt_count, dt_method=dt_method, project_aux=bool(project_aux)))
    cache_key = None
    cache_miss = False
    cache_folder = Path(cache_dir).resolve() if cache_dir is not None else None
    try:
        prices = Prices(graph.inputs, raw_prices)
        encoding = TseitinEncoding(graph)
        result["n_aux_vars"] = encoding.nvars - encoding.ninputs
        result["n_cnf_vars"] = encoding.nvars
        t = time.perf_counter()
        result["n_cnf_clauses"] = encoding.write(out / "formula.cnf", deadline)
        result["cnf_seconds"] = time.perf_counter() - t
        write_json(out / "variables.json", {
            "tuple_variables": {str(encoding.ids[k]): k for k in graph.inputs},
            "auxiliary_variables": {str(encoding.ids[k]): k for k in graph.order if encoding.ids[k] > encoding.ninputs},
        })
        with (out / "fact_prices.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["variable", "provenance", "price"])
            for token in graph.inputs:
                writer.writerow([encoding.ids[token], token, prices.text[token]])
        nnf = out / "formula.cnf.nnf"
        if encoding.nvars == 0:
            # Empty certain-answer set: the conjunction is TRUE and costs zero.
            circuit = NNF([NNFNode("T")], 0)
            result.update(compile_seconds=0.0, nnf_read_seconds=0.0,
                          compiler="not-needed-empty-answer", compiler_sha256=None)
        elif encoding.unit_clauses is not None:
            t = time.perf_counter()
            _write_unit_nnf(nnf, encoding.unit_clauses, encoding.nvars)
            result.update(compile_seconds=time.perf_counter() - t,
                          compiler="builtin-unit-cnf", compiler_sha256=None)
            t = time.perf_counter()
            circuit = NNF.read(nnf, encoding.ninputs, encoding.nvars, deadline)
            result["nnf_read_seconds"] = time.perf_counter() - t
        else:
            compiler = executable(c2d)
            result.update(compiler=compiler, compiler_sha256=sha256_file(Path(compiler)))
            command = [compiler, "-in", str(out / "formula.cnf")]
            if dt_method is not None:
                command.extend(["-dt_method", str(dt_method)])
            if dt_count is not None and dt_method in (None, 0, 1):
                command.extend(["-dt_count", str(dt_count)])
            if project_aux and encoding.nvars > encoding.ninputs:
                variables = range(encoding.ninputs + 1, encoding.nvars + 1)
                with (out / "auxiliary.vars").open("w", encoding="ascii") as stream:
                    stream.write("%d %s\n" % (len(variables), " ".join(map(str, variables))))
                command.extend(["-exist", str(out / "auxiliary.vars")])
            result["aux_projection"] = "compiler" if project_aux else "python"
            write_json(out / "compiler_command.json", command)
            cache_hit = False
            if cache_folder is not None:
                t = time.perf_counter()
                payload = dict(format="kcprice-nnf-cache-v1", ninputs=encoding.ninputs,
                               cnf_sha256=sha256_file(out / "formula.cnf"),
                               compiler_sha256=result["compiler_sha256"],
                               options=result["compiler_options"])
                cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("ascii")).hexdigest()
                cache_hit = _cache_restore(cache_folder, cache_key, nnf, deadline)
                cache_miss = not cache_hit
                result.update(compilation_cache="hit" if cache_hit else "miss",
                              compilation_cache_key=cache_key,
                              cache_read_seconds=time.perf_counter() - t)
            if cache_hit:
                result["compile_seconds"] = 0.0
                (out / "c2d.log").write_text("Verified compiled NNF cache hit; c2d was not invoked.\n", encoding="utf-8")
            else:
                t = time.perf_counter()
                result["compiler_invoked"] = True
                run_process(command, out / "c2d.log", deadline.remaining(), cwd=out)
                result["compile_seconds"] = time.perf_counter() - t
            if not nnf.is_file() or nnf.stat().st_size == 0:
                raise KCError("c2d produced no formula.cnf.nnf; inspect c2d.log")
            t = time.perf_counter()
            circuit = NNF.read(nnf, encoding.ninputs, encoding.nvars, deadline)
            result["nnf_read_seconds"] = time.perf_counter() - t
        result["n_nnf_nodes"] = len(circuit.nodes)
        result["n_nnf_edges"] = circuit.edges
        result["forgotten_aux_literal_occurrences"] = getattr(circuit, "forgotten_occurrences", 0)
        if check_decomposable:
            t = time.perf_counter()
            circuit.check_decomposable()
            result["decomposability_check_seconds"] = time.perf_counter() - t
            deadline.check()
        t = time.perf_counter()
        evaluator = PriceEvaluator(circuit, prices.units, deadline, prune)
        value, selected_ids = evaluator.run()
        result["price_seconds"] = time.perf_counter() - t
        result.update(evaluator.stats)
        t = time.perf_counter()
        if value == INF:
            result["status"] = "UNSAT"
            result["selected_count"] = 0
        else:
            selected = {graph.inputs[i - 1] for i in selected_ids}
            if not graph.satisfied(selected):
                raise KCError("Selected support does not satisfy the ORIGINAL joint provenance")
            if sum(prices.units[i] for i in selected_ids) != value:
                raise KCError("Support price differs from the circuit price (possible nondecomposable NNF)")
            with (out / "selected_tokens.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["variable", "provenance", "price"])
                for i in sorted(selected_ids):
                    token = graph.inputs[i - 1]
                    writer.writerow([i, token, prices.text[token]])
            result.update(status="EXACT", price=prices.format(value), selected_count=len(selected),
                          support_verified=True, price_integer_units=int(value), price_scale=prices.scale)
        result["verify_seconds"] = time.perf_counter() - t
        deadline.check()
        # Publish only a parsed circuit whose pricing result passed validation.
        if cache_miss:
            t = time.perf_counter()
            try:
                _cache_store(cache_folder, cache_key, nnf, deadline)
            except OSError as exc:
                result["cache_write_error"] = str(exc)
            result["cache_write_seconds"] = time.perf_counter() - t
            deadline.check()
    except DeadlineExceeded as exc:
        result.update(status="TIMEOUT", error=str(exc), price=None, support_verified=False)
        raise
    except Exception as exc:
        result.update(status="ERROR", error=str(exc), price=None, support_verified=False)
        raise
    finally:
        result["kc_total_seconds"] = time.perf_counter() - started
        write_json(out / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="JSON with nodes, roots, prices")
    parser.add_argument("--out", required=True, type=Path, help="New output directory")
    parser.add_argument("--c2d", default=os.environ.get("C2D_BIN", "c2d"))
    parser.add_argument("--timeout", type=float, default=100)
    parser.add_argument("--no-prune", action="store_true")
    parser.add_argument("--check-decomposable", action="store_true")
    parser.add_argument("--dt-count", type=int, default=1,
                        help="Randomized dtree candidates (default 1; c2d original default 25)")
    parser.add_argument("--dt-method", type=int, choices=range(5),
                        help="c2d dtree method: 0/1 hypergraph, 2/3 variable order, 4 min-fill")
    parser.add_argument("--no-project-aux", action="store_true",
                        help="Forget auxiliary literals in Python instead of inside c2d")
    parser.add_argument("--cache-dir", type=Path,
                        help="Optional price-independent compiled-NNF cache, disabled by default")
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        graph = ProvenanceGraph(data["nodes"], data["roots"])
        result = solve(graph, data["prices"], args.out, args.c2d, args.timeout,
                       not args.no_prune, args.check_decomposable,
                       {"input_sha256": sha256_file(args.input)},
                       dt_count=args.dt_count, dt_method=args.dt_method,
                       project_aux=not args.no_project_aux, cache_dir=args.cache_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "EXACT" else 2
    except DeadlineExceeded as exc:
        print("TIMEOUT: %s" % exc, file=sys.stderr)
        return 124
    except (KCError, OSError, ValueError, KeyError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
