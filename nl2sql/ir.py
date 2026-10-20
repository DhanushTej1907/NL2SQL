"""Relational-algebra IR with intent tags.

Nodes are plain dataclasses treated as immutable: rewrites build new nodes
with :func:`dataclasses.replace` rather than mutating in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .schema import ForeignKey

# ----------------------------------------------------------------------
# Expressions
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Expr:
    def columns(self) -> list["ColRef"]:
        return []

    def render(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class ColRef(Expr):
    table: str
    column: str

    def columns(self) -> list["ColRef"]:
        return [self]

    def render(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def key(self) -> tuple[str, str]:
        return (self.table, self.column)


@dataclass(frozen=True)
class Star(Expr):
    def render(self) -> str:
        return "*"


@dataclass(frozen=True)
class Literal(Expr):
    value: Any

    def render(self) -> str:
        return repr(self.value)


@dataclass(frozen=True)
class Cmp(Expr):
    op: str
    left: Expr
    right: Expr

    def columns(self) -> list[ColRef]:
        return self.left.columns() + self.right.columns()

    def render(self) -> str:
        return f"({self.left.render()} {self.op} {self.right.render()})"


@dataclass(frozen=True)
class And(Expr):
    items: tuple[Expr, ...]

    def columns(self) -> list[ColRef]:
        return [c for i in self.items for c in i.columns()]

    def render(self) -> str:
        return " AND ".join(i.render() for i in self.items)


@dataclass(frozen=True)
class Or(Expr):
    items: tuple[Expr, ...]

    def columns(self) -> list[ColRef]:
        return [c for i in self.items for c in i.columns()]

    def render(self) -> str:
        return " OR ".join(i.render() for i in self.items)


@dataclass(frozen=True)
class Not(Expr):
    item: Expr

    def columns(self) -> list[ColRef]:
        return self.item.columns()

    def render(self) -> str:
        return f"NOT ({self.item.render()})"


@dataclass(frozen=True)
class AggCall(Expr):
    func: str
    arg: Expr | None = None     # None means COUNT(*)
    distinct: bool = False

    def columns(self) -> list[ColRef]:
        return self.arg.columns() if self.arg else []

    def render(self) -> str:
        inner = self.arg.render() if self.arg else "*"
        prefix = "DISTINCT " if self.distinct else ""
        return f"{self.func}({prefix}{inner})"


@dataclass(frozen=True)
class ExistsExpr(Expr):
    """``EXISTS (<subquery>)`` as a scalar expression."""

    source: "Node"
    negated: bool = False

    def columns(self) -> list[ColRef]:
        return []

    def render(self) -> str:
        word = "NOT EXISTS" if self.negated else "EXISTS"
        return f"{word}(...)"


# ----------------------------------------------------------------------
# Relational nodes
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    def children(self) -> list["Node"]:
        return []

    def with_children(self, children: list["Node"]) -> "Node":
        return self

    def output_tables(self) -> list[str]:
        out: list[str] = []
        for child in self.children():
            for name in child.output_tables():
                if name not in out:
                    out.append(name)
        return out

    def label(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class Scan(Node):
    table: str

    def output_tables(self) -> list[str]:
        return [self.table]

    def label(self) -> str:
        return f"Scan({self.table})"


@dataclass(frozen=True)
class Join(Node):
    left: Node
    right: Node
    on: Expr
    kind: str = "INNER"
    edge: ForeignKey | None = None

    def children(self) -> list[Node]:
        return [self.left, self.right]

    def with_children(self, children: list[Node]) -> Node:
        return replace(self, left=children[0], right=children[1])

    def label(self) -> str:
        provenance = self.edge.status if self.edge else "ad hoc"
        return f"Join[{self.kind}, {provenance}] on {self.on.render()}"


@dataclass(frozen=True)
class Select(Node):
    source: Node
    predicate: Expr

    def children(self) -> list[Node]:
        return [self.source]

    def with_children(self, children: list[Node]) -> Node:
        return replace(self, source=children[0])

    def label(self) -> str:
        return f"Select[{self.predicate.render()}]"


@dataclass(frozen=True)
class Project(Node):
    source: Node
    items: tuple[tuple[Expr, str | None], ...]
    distinct: bool = False

    def children(self) -> list[Node]:
        return [self.source]

    def with_children(self, children: list[Node]) -> Node:
        return replace(self, source=children[0])

    def exprs(self) -> list[Expr]:
        return [e for e, _ in self.items]

    def label(self) -> str:
        cols = ", ".join(e.render() for e, _ in self.items)
        return f"Project[{'DISTINCT ' if self.distinct else ''}{cols}]"


@dataclass(frozen=True)
class Aggregate(Node):
    source: Node
    group_by: tuple[Expr, ...] = ()
    aggs: tuple[tuple[AggCall, str | None], ...] = ()
    having: Expr | None = None

    def children(self) -> list[Node]:
        return [self.source]

    def with_children(self, children: list[Node]) -> Node:
        return replace(self, source=children[0])

    def label(self) -> str:
        keys = ", ".join(e.render() for e in self.group_by)
        calls = ", ".join(a.render() for a, _ in self.aggs)
        return f"Aggregate[group: {keys or 'none'}; {calls}]"


@dataclass(frozen=True)
class Sort(Node):
    source: Node
    keys: tuple[tuple[Expr, str], ...]

    def children(self) -> list[Node]:
        return [self.source]

    def with_children(self, children: list[Node]) -> Node:
        return replace(self, source=children[0])

    def label(self) -> str:
        return "Sort[" + ", ".join(f"{e.render()} {d}" for e, d in self.keys) + "]"


@dataclass(frozen=True)
class Limit(Node):
    source: Node
    count: int

    def children(self) -> list[Node]:
        return [self.source]

    def with_children(self, children: list[Node]) -> Node:
        return replace(self, source=children[0])

    def label(self) -> str:
        return f"Limit[{self.count}]"


@dataclass(frozen=True)
class SemiJoin(Node):
    """``left WHERE EXISTS (right WHERE on AND predicate)``."""

    left: Node
    right: Node
    on: Expr
    predicate: Expr | None = None
    edge: ForeignKey | None = None

    def children(self) -> list[Node]:
        return [self.left, self.right]

    def with_children(self, children: list[Node]) -> Node:
        return replace(self, left=children[0], right=children[1])

    def output_tables(self) -> list[str]:
        return self.left.output_tables()

    def label(self) -> str:
        return f"SemiJoin on {self.on.render()}"


@dataclass(frozen=True)
class AntiJoin(SemiJoin):
    def label(self) -> str:
        return f"AntiJoin on {self.on.render()}"


@dataclass(frozen=True)
class Unit(Node):
    """A single-row source: a SELECT with no FROM clause."""

    def label(self) -> str:
        return "Unit"


@dataclass(frozen=True)
class Raw(Node):
    """SQL that could not be lowered into the IR; carried through verbatim."""

    sql: str

    def label(self) -> str:
        return "Raw[unsupported constructs kept as written]"


@dataclass
class Plan:
    root: Node
    intents: set[str] = field(default_factory=set)

    def pretty(self) -> str:
        return pretty(self.root, self.intents)


def pretty(node: Node, intents: Iterable[str] = ()) -> str:
    """An indented tree rendering, used by the CLI and the UI."""
    lines: list[str] = []
    tags = sorted(intents)
    if tags:
        lines.append(f"intents: {', '.join(tags)}")

    def walk(n: Node, depth: int) -> None:
        lines.append("  " * depth + n.label())
        if isinstance(n, Project):
            for e, _ in n.items:
                if isinstance(e, ExistsExpr):
                    lines.append("  " * (depth + 1) + "EXISTS:")
                    walk(e.source, depth + 2)
        if isinstance(n, (SemiJoin, AntiJoin)):
            lines.append("  " * (depth + 1) + "outer:")
            walk(n.left, depth + 2)
            lines.append("  " * (depth + 1) + "inner:")
            walk(n.right, depth + 2)
            return
        for child in n.children():
            walk(child, depth + 1)

    walk(node, 0)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Traversal helpers used by the optimizer
# ----------------------------------------------------------------------
def walk_nodes(node: Node) -> Iterable[Node]:
    yield node
    for child in node.children():
        yield from walk_nodes(child)
    if isinstance(node, Project):
        for expr, _ in node.items:
            if isinstance(expr, ExistsExpr):
                yield from walk_nodes(expr.source)


def node_columns(node: Node) -> list[ColRef]:
    """Columns a single node mentions, ignoring its children."""
    out: list[ColRef] = []
    if isinstance(node, Project):
        for expr, _ in node.items:
            out.extend(expr.columns())
    elif isinstance(node, Select):
        out.extend(node.predicate.columns())
    elif isinstance(node, Join):
        out.extend(node.on.columns())
    elif isinstance(node, Aggregate):
        for expr in node.group_by:
            out.extend(expr.columns())
        for agg, _ in node.aggs:
            out.extend(agg.columns())
        if node.having is not None:
            out.extend(node.having.columns())
    elif isinstance(node, Sort):
        for expr, _ in node.keys:
            out.extend(expr.columns())
    elif isinstance(node, (SemiJoin, AntiJoin)):
        out.extend(node.on.columns())
        if node.predicate is not None:
            out.extend(node.predicate.columns())
    return out


def conjuncts(expr: Expr | None) -> list[Expr]:
    if expr is None:
        return []
    if isinstance(expr, And):
        out: list[Expr] = []
        for item in expr.items:
            out.extend(conjuncts(item))
        return out
    return [expr]


def conjoin(items: list[Expr]) -> Expr | None:
    items = [i for i in items if i is not None]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return And(tuple(items))


# ----------------------------------------------------------------------
# Lowering: AST -> naive IR
# ----------------------------------------------------------------------
def _all_columns(symbols, table: str) -> list[ColRef]:
    tbl = symbols.table(table)
    if tbl is None:
        return []
    return [ColRef(table, c) for c in tbl.column_names()]


def _edge_predicate(edge) -> Expr:
    parts = [
        Cmp("=", ColRef(edge.from_table, a), ColRef(edge.to_table, b))
        for a, b in zip(edge.from_columns, edge.to_columns)
    ]
    return conjoin(parts) or Literal(1)


def lower(query, analysis, symbols) -> Plan:
    """Translate the parsed query literally, so the optimizer has real work.

    The translation follows the wording rather than the best plan: a joined
    table becomes a derived table projecting all of its columns, and an
    existence question becomes a counting query.
    """
    from .parser import AggregateSpec, ColumnRef

    target = query.target
    intents = set(query.intents)

    def ref(cr) -> ColRef:
        return ColRef(cr.table, cr.column)

    # Predicates, split by the table they constrain.
    per_table: dict[str, list[Expr]] = {}
    shared: list[Expr] = []
    for cond in query.conditions:
        expr = Cmp(cond.operator, ref(cond.column), Literal(cond.value))
        tables = {c.table for c in expr.columns()}
        if len(tables) == 1:
            per_table.setdefault(tables.pop(), []).append(expr)
        else:
            shared.append(expr)

    # FROM clause: the target, then one derived table per joined table.
    node: Node = Scan(target)
    present = {target}
    for pred in per_table.pop(target, []):
        node = Select(node, pred)
    for edge in analysis.join_path.edges:
        new_table = (
            edge.to_table if edge.from_table in present else edge.from_table
        )
        if new_table in present:
            continue
        inner: Node = Scan(new_table)
        preds = per_table.pop(new_table, [])
        combined = conjoin(preds)
        if combined is not None:
            inner = Select(inner, combined)
        right = Project(
            inner,
            tuple((c, None) for c in _all_columns(symbols, new_table)),
            distinct=False,
        )
        node = Join(node, right, _edge_predicate(edge), "INNER", edge)
        present.add(new_table)

    leftovers = [p for preds in per_table.values() for p in preds] + shared
    combined = conjoin(leftovers)
    if combined is not None:
        node = Select(node, combined)

    # Grouping keys, including a readable label when grouping by a table.
    group_keys: list[Expr] = [ref(g) for g in query.group_by]
    for table_name in query.group_tables:
        tbl = symbols.table(table_name)
        if tbl is None:
            continue
        label = tbl.label_column()
        for name in tbl.primary_key:
            group_keys.append(ColRef(tbl.name, name))
        if label is not None:
            group_keys.append(ColRef(tbl.name, label.name))

    agg_calls: list[tuple[AggCall, str | None]] = []
    for agg in query.aggregates:
        arg = ref(agg.column) if agg.column else None
        alias = f"{agg.func.lower()}_{agg.column.column}" if agg.column else "n"
        agg_calls.append((AggCall(agg.func, arg), alias))

    if "existence" in intents:
        body = node
        root: Node = Project(
            body,
            ((Cmp(">", AggCall("COUNT", None), Literal(0)), "answer"),),
            distinct=False,
        )
        return Plan(root, intents)

    if agg_calls:
        node = Aggregate(node, tuple(group_keys), tuple(agg_calls))
        items: list[tuple[Expr, str | None]] = [(k, None) for k in group_keys]
        items.extend((call, alias) for call, alias in agg_calls)
        root = Project(node, tuple(items), distinct=False)
    else:
        projected: list[ColRef] = [ref(p) for p in query.projections if p.resolved()]
        if not projected:
            projected = _all_columns(symbols, target)
        root = Project(
            node,
            tuple((c, None) for c in projected),
            distinct=query.distinct,
        )

    sort_keys: list[tuple[Expr, str]] = []
    for spec in query.order_by:
        if spec.column is not None and spec.column.resolved():
            sort_keys.append((ref(spec.column), spec.direction))
        elif spec.aggregate is not None and agg_calls:
            sort_keys.append((agg_calls[0][0], spec.direction))
    if not sort_keys and query.order_by and agg_calls:
        sort_keys.append((agg_calls[0][0], query.order_by[0].direction))

    if sort_keys:
        # A deterministic tiebreaker keeps LIMIT results and the ordered
        # equivalence comparison stable.
        tbl = symbols.table(target)
        existing = {e.render() for e, _ in sort_keys}
        if tbl is not None and not agg_calls:
            for name in tbl.primary_key:
                key = ColRef(target, name)
                if key.render() not in existing:
                    sort_keys.append((key, "ASC"))
        root = Sort(root, tuple(sort_keys))

    if query.limit is not None:
        root = Limit(root, query.limit)

    return Plan(root, intents)
