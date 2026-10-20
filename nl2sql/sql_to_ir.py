"""Convert a sqlglot SELECT into the IR, for the subset the optimizer knows.

Anything outside that subset raises :class:`Unsupported`; the caller then
keeps the SQL verbatim and marks the result unoptimized.
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

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
    Scan,
    Select,
    SemiJoin,
    Sort,
    Star,
    conjoin,
)
from .schema import ForeignKey, SymbolTable

AGGREGATE_NAMES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}

_COMPARISONS = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
}


class Unsupported(Exception):
    """The SQL uses a construct the IR does not model."""


class Converter:
    def __init__(self, symbols: SymbolTable):
        self.symbols = symbols
        self.aliases: dict[str, str] = {}

    # -- names ----------------------------------------------------------
    def _table_name(self, name: str) -> str:
        table = self.symbols.table(name)
        if table is None:
            raise Unsupported(f"unknown table {name!r}")
        return table.name

    def _resolve_column(self, node: exp.Column) -> ColRef:
        column = node.name
        qualifier = node.table
        if qualifier:
            table = self.aliases.get(qualifier.lower(), qualifier)
            resolved = self.symbols.table(table)
            if resolved is None:
                raise Unsupported(f"unknown table {qualifier!r}")
            if column not in resolved.columns:
                match = next(
                    (c for c in resolved.columns if c.lower() == column.lower()), None
                )
                if match is None:
                    raise Unsupported(f"unknown column {qualifier}.{column}")
                column = match
            return ColRef(resolved.name, column)
        hits = self.symbols.find_columns(column)
        if len(hits) != 1:
            raise Unsupported(f"column {column!r} is not unambiguous")
        return ColRef(hits[0].table, hits[0].name)

    # -- expressions -----------------------------------------------------
    def expr(self, node: exp.Expression) -> Expr:
        if isinstance(node, exp.Paren):
            return self.expr(node.this)
        if isinstance(node, exp.Column):
            return self._resolve_column(node)
        if isinstance(node, exp.Star):
            return Star()
        if isinstance(node, exp.Literal):
            if node.is_string:
                return Literal(node.this)
            text = node.this
            try:
                return Literal(int(text))
            except ValueError:
                return Literal(float(text))
        if isinstance(node, exp.Boolean):
            return Literal(bool(node.this))
        if isinstance(node, exp.Null):
            return Literal(None)
        if isinstance(node, exp.Neg):
            inner = self.expr(node.this)
            if isinstance(inner, Literal) and isinstance(inner.value, (int, float)):
                return Literal(-inner.value)
            raise Unsupported("negation of a non-literal")
        for cls, op in _COMPARISONS.items():
            if isinstance(node, cls):
                return Cmp(op, self.expr(node.this), self.expr(node.expression))
        if isinstance(node, exp.And):
            return And((self.expr(node.this), self.expr(node.expression)))
        if isinstance(node, exp.Or):
            return Or((self.expr(node.this), self.expr(node.expression)))
        if isinstance(node, exp.Not):
            return Not(self.expr(node.this))
        if isinstance(node, exp.Is):
            right = node.expression
            op = "IS NOT" if isinstance(node.parent, exp.Not) else "IS"
            return Cmp(op, self.expr(node.this), self.expr(right))
        if isinstance(node, exp.Between):
            target = self.expr(node.this)
            return And(
                (
                    Cmp(">=", target, self.expr(node.args["low"])),
                    Cmp("<=", target, self.expr(node.args["high"])),
                )
            )
        if isinstance(node, exp.In):
            target = self.expr(node.this)
            options = node.args.get("expressions") or []
            if not options or node.args.get("query"):
                raise Unsupported("IN with a subquery")
            return Or(tuple(Cmp("=", target, self.expr(o)) for o in options))
        if isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            func = type(node).__name__.upper()
            arg = node.this
            if arg is None or isinstance(arg, exp.Star):
                return AggCall(func, None)
            distinct = isinstance(arg, exp.Distinct)
            if distinct:
                inner = arg.expressions[0] if arg.expressions else arg.this
                return AggCall(func, self.expr(inner), distinct=True)
            return AggCall(func, self.expr(arg))
        if isinstance(node, exp.Anonymous) and str(node.this).upper() in AGGREGATE_NAMES:
            args = node.expressions
            return AggCall(
                str(node.this).upper(), self.expr(args[0]) if args else None
            )
        if isinstance(node, exp.Exists):
            inner = node.this
            return ExistsExpr(self.select(self._as_select(inner)))
        if isinstance(node, exp.Alias):
            return self.expr(node.this)
        raise Unsupported(f"expression {type(node).__name__}")

    @staticmethod
    def _as_select(node: exp.Expression) -> exp.Select:
        if isinstance(node, exp.Subquery):
            node = node.this
        if not isinstance(node, exp.Select):
            raise Unsupported("expected a subquery")
        return node

    # -- edges ------------------------------------------------------------
    def _edge_for(self, predicate: Expr) -> ForeignKey | None:
        """Recognise a join predicate as a declared foreign key, if it is one."""
        if not isinstance(predicate, Cmp) or predicate.op != "=":
            return None
        left, right = predicate.left, predicate.right
        if not isinstance(left, ColRef) or not isinstance(right, ColRef):
            return None
        for fk in self.symbols.edges():
            pairs = list(zip(fk.from_columns, fk.to_columns))
            if len(pairs) != 1:
                continue
            a, b = pairs[0]
            forward = (
                left.table == fk.from_table and left.column == a
                and right.table == fk.to_table and right.column == b
            )
            backward = (
                right.table == fk.from_table and right.column == a
                and left.table == fk.to_table and left.column == b
            )
            if forward or backward:
                return fk
        return None

    # -- statements ---------------------------------------------------------
    def select(self, node: exp.Select) -> Node:
        for key in ("with", "distribute", "qualify", "window", "laterals", "pivots"):
            if node.args.get(key):
                raise Unsupported(key)

        from_clause = node.args.get("from")
        if from_clause is None:
            raise Unsupported("SELECT without FROM")
        source = self._source(from_clause.this)

        for join in node.args.get("joins") or []:
            source = self._join(source, join)

        where = node.args.get("where")
        if where is not None:
            source = Select(source, self.expr(where.this))

        group = node.args.get("group")
        having = node.args.get("having")
        items: list[tuple[Expr, str | None]] = []
        aggs: list[tuple[AggCall, str | None]] = []
        distinct = node.args.get("distinct") is not None

        for projection in node.expressions:
            alias = projection.alias_or_name if isinstance(projection, exp.Alias) else None
            converted = self.expr(projection)
            items.append((converted, alias))
            if isinstance(converted, AggCall):
                aggs.append((converted, alias))

        if group is not None or aggs:
            keys = tuple(self.expr(e) for e in (group.expressions if group else []))
            source = Aggregate(
                source,
                keys,
                tuple(aggs),
                self.expr(having.this) if having is not None else None,
            )
        elif having is not None:
            raise Unsupported("HAVING without GROUP BY")

        root: Node = Project(source, tuple(items), distinct=distinct)

        order = node.args.get("order")
        if order is not None:
            keys = []
            for ordered in order.expressions:
                direction = "DESC" if ordered.args.get("desc") else "ASC"
                keys.append((self.expr(ordered.this), direction))
            root = Sort(root, tuple(keys))

        limit = node.args.get("limit")
        if limit is not None:
            value = limit.expression
            if not isinstance(value, exp.Literal):
                raise Unsupported("LIMIT with a non-literal")
            root = Limit(root, int(value.this))
        if node.args.get("offset"):
            raise Unsupported("OFFSET")
        return root

    def _source(self, node: exp.Expression) -> Node:
        if isinstance(node, exp.Table):
            name = self._table_name(node.name)
            if node.alias:
                self.aliases[node.alias.lower()] = name
            self.aliases[name.lower()] = name
            return Scan(name)
        raise Unsupported(f"FROM {type(node).__name__}")

    def _join(self, left: Node, join: exp.Join) -> Node:
        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        if side in ("LEFT", "RIGHT", "FULL") or kind == "CROSS":
            raise Unsupported(f"{side or kind} join")
        right = self._source(join.this)
        on = join.args.get("on")
        if on is None:
            raise Unsupported("join without ON")
        predicate = self.expr(on)
        edge = self._edge_for(predicate)
        return Join(left, right, predicate, "INNER", edge)


def convert(sql: str, symbols: SymbolTable, intents: set[str] | None = None) -> Plan:
    """Parse ``sql`` and convert it into a :class:`~nl2sql.ir.Plan`."""
    statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    if len(statements) != 1:
        raise Unsupported(f"expected one statement, found {len(statements)}")
    statement = statements[0]
    if isinstance(statement, exp.Subquery):
        statement = statement.this
    if not isinstance(statement, exp.Select):
        raise Unsupported(f"{type(statement).__name__} is not a SELECT")
    converter = Converter(symbols)
    root = converter.select(statement)
    tags = set(intents or ())
    if isinstance(root, Project) and root.distinct:
        tags.add("distinct")
    if isinstance(root, Limit):
        tags.add("top_k")
    return Plan(root, tags)
