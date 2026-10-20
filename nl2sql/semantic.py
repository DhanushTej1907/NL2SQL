"""Semantic analysis: reference resolution, join paths and type checks."""

from __future__ import annotations

import difflib
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from .parser import AggregateSpec, ColumnRef, Condition, OrderSpec, Query
from .schema import Column, Database, ForeignKey, SymbolTable

MAX_JOIN_HOPS = 3

NUMERIC_OPERATORS = {">", "<", ">=", "<="}


@dataclass
class Diagnostic:
    level: str              # error | warning | info
    code: str
    message: str
    span: tuple[int, int] | None = None
    suggestions: list[str] = field(default_factory=list)

    def render(self) -> str:
        out = f"[{self.level}] {self.code}: {self.message}"
        if self.suggestions:
            out += "  did you mean: " + ", ".join(self.suggestions)
        return out


@dataclass
class JoinPath:
    edges: list[ForeignKey] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)

    def trusted(self) -> bool:
        return all(e.trusted for e in self.edges)


@dataclass
class SemanticResult:
    query: Query
    join_path: JoinPath
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(d.level == "error" for d in self.diagnostics)

    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "error"]


class SemanticError(Exception):
    def __init__(self, diagnostics: list[Diagnostic]):
        super().__init__("; ".join(d.message for d in diagnostics))
        self.diagnostics = diagnostics


def _undirected_graph(symbols: SymbolTable) -> dict[str, list[tuple[str, ForeignKey]]]:
    graph: dict[str, list[tuple[str, ForeignKey]]] = {t: [] for t in symbols.tables}
    for fk in symbols.edges():
        if fk.from_table not in graph or fk.to_table not in graph:
            continue
        graph[fk.from_table].append((fk.to_table, fk))
        graph[fk.to_table].append((fk.from_table, fk))
    return graph


def shortest_path(symbols: SymbolTable, start: str, goal: str) -> list[ForeignKey] | None:
    """BFS over the FK graph, preferring declared edges on ties."""
    if start == goal:
        return []
    graph = _undirected_graph(symbols)
    queue: deque[tuple[str, list[ForeignKey]]] = deque([(start, [])])
    seen = {start}
    best: list[ForeignKey] | None = None
    while queue:
        node, path = queue.popleft()
        if len(path) >= MAX_JOIN_HOPS:
            continue
        neighbours = sorted(
            graph.get(node, []), key=lambda ne: (0 if ne[1].trusted else 1, ne[0])
        )
        for nxt, edge in neighbours:
            if nxt in seen:
                continue
            candidate = path + [edge]
            if nxt == goal:
                if best is None or len(candidate) < len(best):
                    best = candidate
                elif len(candidate) == len(best) and _score(candidate) < _score(best):
                    best = candidate
                continue
            seen.add(nxt)
            queue.append((nxt, candidate))
        if best is not None:
            break
    return best


def _score(edges: list[ForeignKey]) -> int:
    return sum(0 if e.trusted else 1 for e in edges)


def build_join_path(symbols: SymbolTable, target: str,
                    needed: Iterable[str]) -> tuple[JoinPath, list[Diagnostic]]:
    """Connect every needed table to the target with the fewest edges."""
    diagnostics: list[Diagnostic] = []
    path = JoinPath(tables=[target])
    for table in needed:
        if table in path.tables:
            continue
        best: list[ForeignKey] | None = None
        for anchor in list(path.tables):
            edges = shortest_path(symbols, anchor, table)
            if edges is None:
                continue
            if best is None or len(edges) < len(best):
                best = edges
        if best is None:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "no-join-path",
                    f"no foreign-key path connects {target!r} and {table!r} "
                    f"within {MAX_JOIN_HOPS} hops",
                )
            )
            continue
        for edge in best:
            if edge not in path.edges:
                path.edges.append(edge)
            for name in (edge.from_table, edge.to_table):
                if name not in path.tables:
                    path.tables.append(name)
    inferred = [e for e in path.edges if e.status == "inferred"]
    violated = [e for e in path.edges if e.status == "declared_violated"]
    if inferred:
        diagnostics.append(
            Diagnostic(
                "info", "inferred-join",
                "joined on inferred keys: "
                + "; ".join(e.describe() for e in inferred),
            )
        )
    if violated:
        diagnostics.append(
            Diagnostic(
                "warning", "violated-fk",
                "declared foreign keys with dangling rows are treated as inferred: "
                + "; ".join(e.describe() for e in violated),
            )
        )
    if path.edges:
        diagnostics.append(
            Diagnostic(
                "info", "join-path",
                "join path: " + " , ".join(e.describe() for e in path.edges),
            )
        )
    return path, diagnostics


class Analyzer:
    def __init__(self, db: Database):
        self.db = db
        self.symbols = db.symbols

    def analyze(self, query: Query) -> SemanticResult:
        diagnostics: list[Diagnostic] = []
        target = query.target
        if target is None or self.symbols.table(target) is None:
            raise SemanticError(
                [Diagnostic("error", "unknown-table",
                            f"unknown table {target!r}",
                            suggestions=self._suggest(target or ""))]
            )
        query.target = self.symbols.table(target).name

        # Pass 1: resolve references that name exactly one column.
        refs = list(self._all_refs(query))
        for ref in refs:
            self._resolve(ref, query, diagnostics, prefer_only_certain=True)

        known = self._known_tables(query)
        for ref in refs:
            if not ref.resolved():
                self._resolve(ref, query, diagnostics, known=known)
                known = self._known_tables(query)

        needed = [t for t in self._known_tables(query) if t != query.target]
        path, path_diags = build_join_path(self.symbols, query.target, needed)
        diagnostics.extend(path_diags)

        diagnostics.extend(self._type_checks(query))
        result = SemanticResult(query=query, join_path=path, diagnostics=diagnostics)
        if not result.ok:
            raise SemanticError(result.errors())
        return result

    # -- reference resolution ------------------------------------------------
    @staticmethod
    def _all_refs(query: Query) -> Iterable[ColumnRef]:
        yield from query.projections
        for cond in query.conditions:
            yield cond.column
        for agg in query.aggregates:
            if agg.column:
                yield agg.column
        for spec in query.order_by:
            if spec.column:
                yield spec.column
        yield from query.group_by

    def _known_tables(self, query: Query) -> list[str]:
        out: list[str] = []
        if query.target:
            out.append(query.target)
        for name in query.tables:
            if name and name not in out and self.symbols.table(name):
                out.append(name)
        for ref in self._all_refs(query):
            if ref.resolved() and ref.table not in out:
                out.append(ref.table)
        return out

    def _resolve(self, ref: ColumnRef, query: Query, diagnostics: list[Diagnostic],
                 known: list[str] | None = None, prefer_only_certain: bool = False) -> None:
        if ref.resolved() or not ref.candidates:
            return
        options = {(c.table, c.column) for c in ref.candidates}
        if len(options) == 1:
            ref.table, ref.column = next(iter(options))
            return
        if prefer_only_certain:
            return

        known = known or [query.target or ""]
        # 1. the target table wins outright
        for table, column in options:
            if table == query.target:
                ref.table, ref.column = table, column
                return
        # 2. a table already pulled in by another reference
        for name in known:
            for table, column in options:
                if table == name:
                    ref.table, ref.column = table, column
                    return
        # 3. otherwise the shortest join path from the target
        best: tuple[int, str, str] | None = None
        for table, column in sorted(options):
            edges = shortest_path(self.symbols, query.target or "", table)
            if edges is None:
                continue
            cost = (len(edges), table, column)
            if best is None or cost < best:
                best = cost
        if best is None:
            diagnostics.append(
                Diagnostic(
                    "error", "ambiguous-column",
                    f"{ref.text!r} could be " + " or ".join(
                        f"{t}.{c}" for t, c in sorted(options)
                    ),
                )
            )
            return
        ref.table, ref.column = best[1], best[2]
        diagnostics.append(
            Diagnostic(
                "info", "resolved-column",
                f"{ref.text!r} resolved to {ref.table}.{ref.column}",
            )
        )

    # -- type checks -----------------------------------------------------------
    def _type_checks(self, query: Query) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        for cond in query.conditions:
            col = self._column(cond.column)
            if col is None:
                continue
            numeric_value = isinstance(cond.value, (int, float)) and not isinstance(
                cond.value, bool
            )
            if cond.operator in NUMERIC_OPERATORS and col.type_class == "text":
                out.append(
                    Diagnostic(
                        "error", "type-mismatch",
                        f"{cond.operator!r} needs an ordered column, but "
                        f"{col.qname} holds text",
                    )
                )
            elif col.type_class == "numeric" and not numeric_value:
                out.append(
                    Diagnostic(
                        "error", "type-mismatch",
                        f"{col.qname} is numeric, so it cannot be compared with "
                        f"{cond.value!r}",
                        suggestions=self._suggest(str(cond.value)),
                    )
                )
        for agg in query.aggregates:
            if agg.func == "COUNT" or agg.column is None:
                continue
            col = self._column(agg.column)
            if col is None:
                continue
            if col.type_class not in ("numeric", "date"):
                out.append(
                    Diagnostic(
                        "error", "aggregate-type",
                        f"{agg.func} needs a numeric column, but {col.qname} "
                        f"holds {col.type_class}",
                    )
                )
        return out

    def _column(self, ref: ColumnRef) -> Column | None:
        if not ref.resolved():
            return None
        tbl = self.symbols.table(ref.table or "")
        if tbl is None:
            return None
        return tbl.columns.get(ref.column or "")

    # -- suggestions ------------------------------------------------------------
    def _suggest(self, word: str) -> list[str]:
        if not word:
            return []
        pool: list[str] = list(self.symbols.tables)
        for col in self.symbols.all_columns():
            pool.append(col.name)
            pool.extend(str(s) for s in col.samples[:20])
        return difflib.get_close_matches(str(word), list(dict.fromkeys(pool)), n=3, cutoff=0.7)


def suggest_for(db: Database, word: str) -> list[str]:
    return Analyzer(db)._suggest(word)
