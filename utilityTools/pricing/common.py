"""Algorithm-neutral provenance, Tseitin, prices, deadlines and JSON helpers.

Extracted without changes to computational behaviour from the previously
provided core. Does not import KCPrice, c2d, ProvSQL or any SAT solver.
The original KCPrice files stay unchanged for backwards compatibility.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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
            raise DeadlineExceeded("Pricing time budget exhausted")
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
        # Stream the body, so the CNF is not duplicated in Python memory.
        body = path.with_suffix(path.suffix + ".body")
        count = 0
        try:
            with body.open("w", encoding="ascii") as stream:
                for count, clause in enumerate(self.clauses(), 1):
                    if deadline and count % 8192 == 0:
                        deadline.check()
                    stream.write(" ".join(map(str, clause)) + " 0\n")
            with path.open("w", encoding="ascii") as dest:
                dest.write("c Pricing: original tuple IDs are 1..%d\n" % self.ninputs)
                dest.write("p cnf %d %d\n" % (self.nvars, count))
                with body.open("r", encoding="ascii") as source:
                    shutil.copyfileobj(source, dest)
        finally:
            if body.exists():
                body.unlink()
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
