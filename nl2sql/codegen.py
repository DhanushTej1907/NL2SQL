"""IR -> SQL.

The IR is rendered into canonical SQLite text, parsed into a sqlglot
expression (which validates it and gives a real AST), and emitted again in
the requested dialect. Identifiers are always quoted, so reserved words,
spaces and mixed-case names all survive.
"""

from __future__ import annotations

from typing import Any, Iterable

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
    Raw,
    Scan,
    Select,
    SemiJoin,
    Sort,
    Star,
    Unit,
)
from .schema import quote_ident

DEFAULT_DIALECT = "sqlite"

_OPERATOR_SQL = {"!=": "<>", "==": "="}


class CodegenError(Exception):
    pass


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def render_expr(expr: Expr) -> str:
    if isinstance(expr, ColRef):
        return f"{quote_ident(expr.table)}.{quote_ident(expr.column)}"
    if isinstance(expr, Star):
        return "*"
    if isinstance(expr, Literal):
        return sql_literal(expr.value)
    if isinstance(expr, Cmp):
        op = _OPERATOR_SQL.get(expr.op, expr.op)
        return f"({render_expr(expr.left)} {op} {render_expr(expr.right)})"
    if isinstance(expr, And):
        return "(" + " AND ".join(render_expr(i) for i in expr.items) + ")"
    if isinstance(expr, Or):
        return "(" + " OR ".join(render_expr(i) for i in expr.items) + ")"
    if isinstance(expr, Not):
        return f"(NOT {render_expr(expr.item)})"
    if isinstance(expr, AggCall):
        inner = render_expr(expr.arg) if expr.arg is not None else "*"
        prefix = "DISTINCT " if expr.distinct else ""
        return f"{expr.func}({prefix}{inner})"
    if isinstance(expr, ExistsExpr):
        word = "NOT EXISTS" if expr.negated else "EXISTS"
        return f"{word} ({render_select(expr.source)})"
    raise CodegenError(f"cannot render expression {type(expr).__name__}")


def _operand(node: Node) -> str:
    """Render something that can sit in a FROM or JOIN position."""
    if isinstance(node, Scan):
        return quote_ident(node.table)
    if isinstance(node, Raw):
        return f"({node.sql})"
    tables = node.output_tables()
    alias = tables[0] if tables else "t"
    return f"({render_select(node)}) AS {quote_ident(alias)}"


def _collect_from(node: Node, where: list[str]) -> str:
    """Walk the relational chain, building FROM and collecting WHERE terms."""
    if isinstance(node, Unit):
        return ""
    if isinstance(node, Scan):
        return quote_ident(node.table)
    if isinstance(node, Select):
        source = _collect_from(node.source, where)
        where.append(render_expr(node.predicate))
        return source
    if isinstance(node, Join):
        left = _collect_from(node.left, where)
        right = _operand(node.right)
        return f"{left} {node.kind} JOIN {right} ON {render_expr(node.on)}"
    if isinstance(node, (SemiJoin, AntiJoin)):
        left = _collect_from(node.left, where)
        terms = [node.on] + ([node.predicate] if node.predicate is not None else [])
        from .ir import conjoin

        body_filter = conjoin(terms)
        word = "NOT EXISTS" if isinstance(node, AntiJoin) else "EXISTS"
        where.append(f"{word} ({render_select(node.right, extra_where=body_filter)})")
        return left
    if isinstance(node, (Project, Aggregate, Sort, Limit)):
        return _operand(node)
    if isinstance(node, Raw):
        return f"({node.sql})"
    raise CodegenError(f"cannot render node {type(node).__name__} in FROM position")


def render_select(node: Node, extra_where: Expr | None = None) -> str:
    """Render any IR subtree as a single SELECT statement."""
    if isinstance(node, Raw):
        return node.sql

    limit: int | None = None
    order: list[tuple[Expr, str]] = []
    while True:
        if isinstance(node, Limit):
            limit = node.count
            node = node.source
        elif isinstance(node, Sort):
            order = list(node.keys)
            node = node.source
        else:
            break

    distinct = False
    items: list[tuple[Expr, str | None]] = []
    if isinstance(node, Project):
        distinct = node.distinct
        items = list(node.items)
        node = node.source
    else:
        items = [(Star(), None)]

    group: list[Expr] = []
    having: Expr | None = None
    if isinstance(node, Aggregate):
        group = list(node.group_by)
        having = node.having
        node = node.source

    where: list[str] = []
    from_sql = _collect_from(node, where)
    if extra_where is not None:
        where.append(render_expr(extra_where))

    select_parts = []
    for expr, alias in items:
        text = render_expr(expr)
        if alias:
            text += f" AS {quote_ident(alias)}"
        select_parts.append(text)

    sql = "SELECT "
    if distinct:
        sql += "DISTINCT "
    sql += ", ".join(select_parts)
    if from_sql:
        sql += f" FROM {from_sql}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += " GROUP BY " + ", ".join(render_expr(g) for g in group)
    if having is not None:
        sql += " HAVING " + render_expr(having)
    if order:
        sql += " ORDER BY " + ", ".join(
            f"{render_expr(e)} {d}" for e, d in order
        )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql


def generate(plan: Plan | Node, dialect: str = DEFAULT_DIALECT,
             pretty: bool = False) -> str:
    """Render a plan, then re-emit it through sqlglot in ``dialect``."""
    node = plan.root if isinstance(plan, Plan) else plan
    sql = render_select(node)
    return to_dialect(sql, dialect, pretty=pretty)


def to_dialect(sql: str, dialect: str = DEFAULT_DIALECT, pretty: bool = False) -> str:
    try:
        tree = sqlglot.parse_one(sql, read=DEFAULT_DIALECT)
    except Exception as exc:
        raise CodegenError(f"generated SQL did not parse: {exc}\n{sql}") from exc
    return tree.sql(dialect=dialect, pretty=pretty)


def parse_expression(sql: str) -> exp.Expression:
    """Parse generated SQL into a sqlglot expression (used by the tests)."""
    return sqlglot.parse_one(sql, read=DEFAULT_DIALECT)


def is_single_select(sql: str, dialect: str = DEFAULT_DIALECT) -> tuple[bool, str]:
    """Guard: only one statement, and it must read rather than write."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as exc:
        return False, f"could not parse SQL: {exc}"
    if len(statements) != 1:
        return False, f"expected exactly one statement, found {len(statements)}"
    stmt = statements[0]
    if isinstance(stmt, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return True, ""
    if isinstance(stmt, exp.Subquery) and isinstance(stmt.this, exp.Select):
        return True, ""
    return False, f"only SELECT statements may run, got {type(stmt).__name__.upper()}"


def explain_plan(conn, sql: str) -> list[str]:
    """``EXPLAIN QUERY PLAN`` output as readable lines."""
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    except Exception as exc:
        return [f"plan unavailable: {exc}"]
    out: list[str] = []
    for row in rows:
        try:
            out.append(str(row["detail"]))
        except (IndexError, KeyError, TypeError):
            out.append(" ".join(str(v) for v in tuple(row)))
    return out
