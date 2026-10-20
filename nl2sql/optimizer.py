"""Intent-aware, sound-by-construction optimizer.

Every rule states its preconditions explicitly and fires only when all of
them hold on **declared** constraints. An inferred join edge, or a declared
one the data violates, never satisfies a precondition. Each firing — and
each near miss — is recorded in the trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Iterator

from . import codegen
from .ir import (
    AggCall,
    Aggregate,
    And,
    AntiJoin,
    ColRef,
    Cmp,
    Expr,
    ExistsExpr,
    Join,
    Limit,
    Literal,
    Node,
    Not,
    Or,
    Plan,
    Project,
    Raw,
    Scan,
    Select,
    SemiJoin,
    Sort,
    Star,
    Unit,
    conjoin,
    conjuncts,
    walk_nodes,
)
from .schema import SymbolTable

MAX_PASSES = 10
MAX_FIRINGS = 20


@dataclass
class Check:
    description: str
    passed: bool
    evidence: str = ""

    def render(self) -> str:
        mark = "OK" if self.passed else "NO"
        tail = f" ({self.evidence})" if self.evidence else ""
        return f"[{mark}] {self.description}{tail}"


@dataclass
class TraceEntry:
    rule: str
    fired: bool
    checks: list[Check] = field(default_factory=list)
    before_sql: str = ""
    after_sql: str = ""
    note: str = ""

    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def reason(self) -> str:
        """Why the rule did not fire, phrased as the failure it was."""
        failed = self.failed_checks()
        if not failed:
            return self.note
        return "; ".join(
            f"{c.description} - not so: {c.evidence}" if c.evidence
            else f"{c.description} - not so"
            for c in failed
        )


@dataclass
class OptimizeResult:
    plan: Plan
    trace: list[TraceEntry] = field(default_factory=list)
    skipped: list[TraceEntry] = field(default_factory=list)

    def fired_rules(self) -> list[str]:
        return [t.rule for t in self.trace]


@dataclass
class Context:
    symbols: SymbolTable
    intents: set[str] = field(default_factory=set)


# ----------------------------------------------------------------------
# Tree utilities
# ----------------------------------------------------------------------
def replace_node(root: Node, target: Node, new: Node) -> Node:
    """Rebuild ``root`` with ``target`` (matched by identity) swapped out."""

    def walk(node: Node) -> Node:
        if node is target:
            return new
        children = node.children()
        if children:
            rebuilt = [walk(c) for c in children]
            if any(a is not b for a, b in zip(children, rebuilt)):
                node = node.with_children(rebuilt)
        if isinstance(node, Project):
            items = []
            changed = False
            for expr, alias in node.items:
                if isinstance(expr, ExistsExpr):
                    inner = walk(expr.source)
                    if inner is not expr.source:
                        expr = replace(expr, source=inner)
                        changed = True
                items.append((expr, alias))
            if changed:
                node = replace(node, items=tuple(items))
        return node

    return walk(root)


def parents_of(root: Node) -> dict[int, Node]:
    out: dict[int, Node] = {}
    for node in walk_nodes(root):
        for child in node.children():
            out[id(child)] = node
        if isinstance(node, Project):
            for expr, _ in node.items:
                if isinstance(expr, ExistsExpr):
                    out[id(expr.source)] = node
    return out


def map_expr(expr: Expr, fn) -> Expr:
    """Rebuild an expression, applying ``fn`` to every sub-expression."""
    if isinstance(expr, Cmp):
        expr = replace(expr, left=map_expr(expr.left, fn), right=map_expr(expr.right, fn))
    elif isinstance(expr, (And, Or)):
        expr = replace(expr, items=tuple(map_expr(i, fn) for i in expr.items))
    elif isinstance(expr, Not):
        expr = replace(expr, item=map_expr(expr.item, fn))
    elif isinstance(expr, AggCall) and expr.arg is not None:
        expr = replace(expr, arg=map_expr(expr.arg, fn))
    return fn(expr)


def map_node_exprs(node: Node, fn) -> Node:
    """Apply ``fn`` to every expression in the whole tree below ``node``."""
    children = node.children()
    if children:
        rebuilt = [map_node_exprs(c, fn) for c in children]
        if any(a is not b for a, b in zip(children, rebuilt)):
            node = node.with_children(rebuilt)
    if isinstance(node, Project):
        items = []
        for expr, alias in node.items:
            if isinstance(expr, ExistsExpr):
                expr = replace(expr, source=map_node_exprs(expr.source, fn))
            else:
                expr = map_expr(expr, fn)
            items.append((expr, alias))
        node = replace(node, items=tuple(items))
    elif isinstance(node, Select):
        node = replace(node, predicate=map_expr(node.predicate, fn))
    elif isinstance(node, Join):
        node = replace(node, on=map_expr(node.on, fn))
    elif isinstance(node, Aggregate):
        node = replace(
            node,
            group_by=tuple(map_expr(e, fn) for e in node.group_by),
            aggs=tuple((map_expr(a, fn), al) for a, al in node.aggs),
            having=map_expr(node.having, fn) if node.having is not None else None,
        )
    elif isinstance(node, Sort):
        node = replace(node, keys=tuple((map_expr(e, fn), d) for e, d in node.keys))
    elif isinstance(node, (SemiJoin, AntiJoin)):
        node = replace(
            node,
            on=map_expr(node.on, fn),
            predicate=map_expr(node.predicate, fn) if node.predicate is not None else None,
        )
    return node


def columns_outside(root: Node, subtree: Node) -> set[tuple[str, str]]:
    """Every column mentioned outside ``subtree``."""
    from .ir import node_columns

    inside = {id(n) for n in walk_nodes(subtree)}
    used: set[tuple[str, str]] = set()
    for node in walk_nodes(root):
        if id(node) in inside:
            continue
        for col in node_columns(node):
            used.add(col.key)
    return used


def tables_of(node: Node) -> set[str]:
    return set(node.output_tables())


# ----------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------
class Rule:
    name = "rule"
    description = ""

    def sites(self, root: Node) -> Iterator[Node]:
        return iter(())

    def check(self, root: Node, node: Node, ctx: Context) -> list[Check]:
        return []

    def apply(self, root: Node, node: Node, ctx: Context) -> Node:
        return root


class ExistenceRule(Rule):
    """R1 — ``SELECT COUNT(*) > 0`` becomes ``SELECT EXISTS (...)``."""

    name = "R1 existence"
    description = "COUNT(*) > 0 -> EXISTS"

    _OPS = {">": 0, ">=": 1, "<>": 0, "!=": 0}

    def sites(self, root: Node) -> Iterator[Node]:
        for node in walk_nodes(root):
            if isinstance(node, Project) and len(node.items) == 1:
                expr = node.items[0][0]
                if isinstance(expr, Cmp) and isinstance(expr.left, AggCall):
                    if expr.left.func == "COUNT":
                        yield node

    def check(self, root: Node, node: Node, ctx: Context) -> list[Check]:
        expr = node.items[0][0]
        count: AggCall = expr.left
        literal = expr.right
        checks = [
            Check(
                "the comparison is a non-emptiness test",
                isinstance(literal, Literal)
                and expr.op in self._OPS
                and literal.value == self._OPS.get(expr.op, -1),
                f"{count.render()} {expr.op} {getattr(literal, 'value', '?')}",
            ),
            Check(
                "no GROUP BY or HAVING under the aggregate",
                not isinstance(node.source, Aggregate)
                or (not node.source.group_by and node.source.having is None),
                "the count covers the whole result",
            ),
        ]
        if count.arg is not None:
            checks.append(
                Check(
                    "COUNT over a column keeps its NOT NULL guard",
                    True,
                    f"adds {count.arg.render()} IS NOT NULL to the EXISTS body",
                )
            )
        return checks

    def apply(self, root: Node, node: Node, ctx: Context) -> Node:
        expr = node.items[0][0]
        count: AggCall = expr.left
        source = node.source
        if isinstance(source, Aggregate):
            source = source.source
        if count.arg is not None:
            guard = Cmp("IS NOT", count.arg, Literal(None))
            source = Select(source, guard)
        body = Project(source, ((Literal(1), None),), distinct=False)
        alias = node.items[0][1] or "answer"
        new = Project(Unit(), ((ExistsExpr(body), alias),), distinct=False)
        return replace_node(root, node, new)


class SemiJoinRule(Rule):
    """R2 — ``DISTINCT outer FROM outer JOIN inner`` becomes ``WHERE EXISTS``."""

    name = "R2 semi-join"
    description = "JOIN + DISTINCT -> WHERE EXISTS"

    def sites(self, root: Node) -> Iterator[Node]:
        for node in walk_nodes(root):
            if not isinstance(node, Project) or not node.distinct:
                continue
            inner = node.source
            if isinstance(inner, Select):
                inner = inner.source
            if isinstance(inner, Join):
                yield node

    @staticmethod
    def _parts(node: Project) -> tuple[Join, Expr | None]:
        source = node.source
        outer_filter = None
        if isinstance(source, Select):
            outer_filter = source.predicate
            source = source.source
        return source, outer_filter

    def check(self, root: Node, node: Node, ctx: Context) -> list[Check]:
        join, _ = self._parts(node)
        outer_tables = tables_of(join.left)
        inner_tables = tables_of(join.right)
        projected = {c.key for e, _ in node.items for c in e.columns()}
        projected_tables = {t for t, _ in projected}

        above = columns_outside(root, node.source)
        above_inner = {c for c in above if c[0] in inner_tables}

        edge = join.edge
        outer_name = next(iter(outer_tables)) if len(outer_tables) == 1 else None
        outer_tbl = ctx.symbols.table(outer_name) if outer_name else None

        checks = [
            Check(
                "only outer-table columns are projected",
                projected_tables.issubset(outer_tables),
                f"projected from {sorted(projected_tables)}, outer is {sorted(outer_tables)}",
            ),
            Check(
                "no inner-table column is used above the join",
                not above_inner,
                f"inner columns above: {sorted(above_inner) or 'none'}",
            ),
            Check(
                "the join edge is a declared, unviolated foreign key",
                bool(edge) and edge.status == "declared",
                edge.describe() if edge else "no foreign key behind this join",
            ),
            Check(
                "the outer table has a declared primary key",
                bool(outer_tbl) and bool(outer_tbl.primary_key),
                f"{outer_name}: {outer_tbl.primary_key if outer_tbl else 'unknown'}",
            ),
            Check(
                "the join is an inner join",
                join.kind == "INNER",
                join.kind,
            ),
        ]
        return checks

    @staticmethod
    def drops_distinct(node: Project, ctx: Context) -> tuple[bool, str]:
        """DISTINCT may only go when a non-null primary key is projected."""
        projected = {c.key for e, _ in node.items for c in e.columns()}
        tables = {t for t, _ in projected}
        if len(tables) != 1:
            return False, "projection spans several tables"
        tbl = ctx.symbols.table(next(iter(tables)))
        if tbl is None or not tbl.primary_key:
            return False, "outer table has no declared primary key"
        pk_keys = {(tbl.name, c) for c in tbl.primary_key}
        if not pk_keys.issubset(projected):
            return False, f"primary key {sorted(tbl.primary_key)} is not projected"
        if not all(tbl.columns[c].never_null() for c in tbl.primary_key):
            return False, "primary-key columns are not guaranteed NOT NULL"
        return True, f"primary key {sorted(tbl.primary_key)} is projected and NOT NULL"

    def apply(self, root: Node, node: Node, ctx: Context) -> Node:
        join, outer_filter = self._parts(node)
        inner_tables = tables_of(join.right)

        outer_terms: list[Expr] = []
        inner_terms: list[Expr] = []
        for term in conjuncts(outer_filter):
            term_tables = {c.table for c in term.columns()}
            if term_tables and term_tables.issubset(inner_tables):
                inner_terms.append(term)
            else:
                outer_terms.append(term)

        left: Node = join.left
        outer_pred = conjoin(outer_terms)
        if outer_pred is not None:
            left = Select(left, outer_pred)

        semi = SemiJoin(
            left=left,
            right=join.right,
            on=join.on,
            predicate=conjoin(inner_terms),
            edge=join.edge,
        )
        can_drop, _ = self.drops_distinct(node, ctx)
        new = replace(node, source=semi, distinct=not can_drop)
        return replace_node(root, node, new)


class ColumnPruningRule(Rule):
    """R3 — narrow inner projections to the columns actually used above."""

    name = "R3 column pruning"
    description = "drop columns no one above reads"

    def sites(self, root: Node) -> Iterator[Node]:
        """Only projections in a subquery position; never the user's own."""
        parents = parents_of(root)
        for node in walk_nodes(root):
            if node is root or not isinstance(node, Project) or node.distinct:
                continue
            parent = parents.get(id(node))
            if parent is None:
                continue
            if isinstance(parent, Join) and parent.right is node:
                yield node
            elif isinstance(parent, (SemiJoin, AntiJoin)) and parent.right is node:
                yield node
            elif isinstance(parent, Project) and any(
                isinstance(e, ExistsExpr) and e.source is node for e, _ in parent.items
            ):
                yield node

    @staticmethod
    def _wanted(root: Node, node: Project) -> tuple[list[tuple[Expr, str | None]], str]:
        parents = parents_of(root)
        parent = parents.get(id(node))
        exists_body = isinstance(parent, (SemiJoin, AntiJoin)) and parent.right is node
        if isinstance(parent, Project):
            for expr, _ in parent.items:
                if isinstance(expr, ExistsExpr) and expr.source is node:
                    exists_body = True
        if exists_body:
            return [(Literal(1), None)], "EXISTS ignores its select list"

        used = columns_outside(root, node)
        kept = [
            (expr, alias)
            for expr, alias in node.items
            if not isinstance(expr, ColRef) or expr.key in used
        ]
        return kept, f"{len(kept)} of {len(node.items)} columns are read above"

    def check(self, root: Node, node: Node, ctx: Context) -> list[Check]:
        kept, why = self._wanted(root, node)
        return [
            Check("the projection is not DISTINCT", not node.distinct,
                  "pruning under DISTINCT would change cardinality"),
            Check("at least one column is unused above", len(kept) < len(node.items), why),
            Check("something is still projected", bool(kept), why),
        ]

    def apply(self, root: Node, node: Node, ctx: Context) -> Node:
        kept, _ = self._wanted(root, node)
        return replace_node(root, node, replace(node, items=tuple(kept)))


class RedundantJoinRule(Rule):
    """R4 — drop a foreign-key join whose table nobody reads."""

    name = "R4 redundant join"
    description = "remove a join that cannot filter or duplicate rows"

    def sites(self, root: Node) -> Iterator[Node]:
        for node in walk_nodes(root):
            if isinstance(node, Join):
                yield node

    @staticmethod
    def _key_map(node: Join) -> dict[tuple[str, str], ColRef]:
        """Map each referenced key column onto the foreign-key column."""
        edge = node.edge
        if edge is None:
            return {}
        inner = tables_of(node.right)
        mapping: dict[tuple[str, str], ColRef] = {}
        for from_col, to_col in zip(edge.from_columns, edge.to_columns):
            if edge.to_table in inner:
                mapping[(edge.to_table, to_col)] = ColRef(edge.from_table, from_col)
            elif edge.from_table in inner:
                mapping[(edge.from_table, from_col)] = ColRef(edge.to_table, to_col)
        return mapping

    def check(self, root: Node, node: Node, ctx: Context) -> list[Check]:
        edge = node.edge
        inner = tables_of(node.right)
        mapping = self._key_map(node)
        used = columns_outside(root, node.right) - {
            c.key for c in node.on.columns()
        }
        # Key references survive the rewrite, so they do not block the rule.
        leftover = {c for c in used if c[0] in inner and c not in mapping}

        filtered = any(isinstance(n, (Select, SemiJoin, AntiJoin))
                       for n in walk_nodes(node.right))
        checks = [
            Check(
                "the joined table is otherwise unused above",
                not leftover,
                f"still needed: {sorted(leftover) or 'nothing'}",
            ),
            Check(
                "the joined table carries no filter of its own",
                not filtered,
                "a filtered inner side can remove rows, so the join is not redundant",
            ),
            Check(
                "the join edge is a declared, unviolated foreign key",
                bool(edge) and edge.status == "declared",
                edge.describe() if edge else "no foreign key behind this join",
            ),
            Check("the join is an inner join", node.kind == "INNER", node.kind),
        ]
        if not edge:
            return checks

        # Which side holds the foreign key, and which the referenced key.
        fk_table, fk_cols = edge.from_table, edge.from_columns
        pk_table, pk_cols = edge.to_table, edge.to_columns
        if pk_table not in inner:
            fk_table, fk_cols, pk_table, pk_cols = pk_table, pk_cols, fk_table, fk_cols

        fk_tbl = ctx.symbols.table(fk_table)
        pk_tbl = ctx.symbols.table(pk_table)
        not_null = bool(fk_tbl) and all(
            fk_tbl.columns[c].never_null() for c in fk_cols if c in fk_tbl.columns
        )
        checks.append(
            Check(
                "the foreign-key columns are NOT NULL",
                not_null,
                ", ".join(
                    f"{fk_table}.{c}: "
                    + ("NOT NULL" if fk_tbl and c in fk_tbl.columns
                       and fk_tbl.columns[c].never_null() else "nullable")
                    for c in fk_cols
                ),
            )
        )
        unique = False
        evidence = "referenced columns are not a declared key"
        if pk_tbl:
            if list(pk_tbl.primary_key) == list(pk_cols) and pk_tbl.has_trusted_pk():
                unique, evidence = True, f"{pk_table} primary key {list(pk_cols)}"
            elif len(pk_cols) == 1 and pk_cols[0] in pk_tbl.columns:
                col = pk_tbl.columns[pk_cols[0]]
                if col.unique and col.never_null():
                    unique, evidence = True, f"{pk_table}.{col.name} is UNIQUE NOT NULL"
        checks.append(
            Check("the referenced columns are a unique, non-null key", unique, evidence)
        )
        checks.append(
            Check(
                "the joined table is reachable only through this edge",
                len(inner) == 1,
                f"inner side covers {sorted(inner)}",
            )
        )
        return checks

    def apply(self, root: Node, node: Node, ctx: Context) -> Node:
        mapping = self._key_map(node)
        new_root = replace_node(root, node, node.left)

        if mapping:
            def rewrite(expr: Expr) -> Expr:
                if isinstance(expr, ColRef) and expr.key in mapping:
                    return mapping[expr.key]
                return expr

            new_root = map_node_exprs(new_root, rewrite)
        return new_root


RULES: list[Rule] = [
    ExistenceRule(),
    SemiJoinRule(),
    RedundantJoinRule(),
    ColumnPruningRule(),
]


def optimize(plan: Plan, symbols: SymbolTable,
             enabled: Iterable[str] | None = None) -> OptimizeResult:
    """Run the rules to a fixpoint, recording every firing and near miss."""
    ctx = Context(symbols=symbols, intents=set(plan.intents))
    root = plan.root
    trace: list[TraceEntry] = []
    skipped: list[TraceEntry] = []
    seen_skips: set[tuple[str, str]] = set()
    allowed = set(enabled) if enabled is not None else None

    firings = 0
    for _ in range(MAX_PASSES):
        fired = False
        for rule in RULES:
            if allowed is not None and rule.name not in allowed:
                continue
            for node in list(rule.sites(root)):
                try:
                    checks = rule.check(root, node, ctx)
                except Exception as exc:  # a rule must never break the compile
                    skipped.append(
                        TraceEntry(rule.name, False, note=f"rule error: {exc}")
                    )
                    continue
                if not checks:
                    continue
                if all(c.passed for c in checks):
                    before = _safe_sql(root)
                    try:
                        new_root = rule.apply(root, node, ctx)
                    except Exception as exc:
                        skipped.append(
                            TraceEntry(rule.name, False, checks,
                                       note=f"rewrite failed: {exc}")
                        )
                        continue
                    after = _safe_sql(new_root)
                    if after == before:
                        continue
                    trace.append(
                        TraceEntry(rule.name, True, checks, before, after,
                                   note=rule.description)
                    )
                    root = new_root
                    fired = True
                    firings += 1
                    break
                else:
                    key = (rule.name, node.label())
                    if key not in seen_skips:
                        seen_skips.add(key)
                        skipped.append(
                            TraceEntry(rule.name, False, checks,
                                       note=f"matched {node.label()}")
                        )
            if fired:
                break
        if not fired or firings >= MAX_FIRINGS:
            break

    # A rule that fired somewhere will also have been re-examined and
    # declined afterwards, which is just the fixpoint stopping. Only rules
    # that never fired belong in the skipped list.
    fired_names = {entry.rule for entry in trace}
    skipped = [entry for entry in skipped if entry.rule not in fired_names]

    return OptimizeResult(Plan(root, set(plan.intents)), trace, skipped)


def _safe_sql(node: Node) -> str:
    try:
        return codegen.render_select(node)
    except Exception as exc:
        return f"<unrenderable: {exc}>"
