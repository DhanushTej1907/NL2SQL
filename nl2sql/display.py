"""How SQL is shown to a person. Display only.

The SQL that runs and the equivalence check never pass through here; this
only produces an easier-to-read rendering of the same query.

Compact form:
- a select list naming every column of one table, in schema order, becomes
  ``*`` (single source) or ``t.*`` (in a join);
- identifiers lose their quotes unless they need them (reserved words,
  spaces or symbols, upper or mixed case);
- parentheses around a whole WHERE/HAVING condition, and around its AND-ed
  parts, are dropped.
"""

from __future__ import annotations

import functools
import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from . import codegen
from .schema import SymbolTable

_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_]*")


def format_sql(sql: str, dialect: str, symbols: SymbolTable, raw: bool = False) -> str:
    """The SQL as shown to the user: compact by default, the full form if ``raw``."""
    return pretty_sql(sql, dialect) if raw else compact_sql(sql, dialect, symbols)


def pretty_sql(sql: str, dialect: str) -> str:
    """The full form: every column listed, every identifier quoted."""
    try:
        return codegen.to_dialect(sql, dialect, pretty=True)
    except Exception:
        return sql


def compact_sql(sql: str, dialect: str, symbols: SymbolTable) -> str:
    """A shorter rendering of the same query; falls back to the full form."""
    full = pretty_sql(sql, dialect)
    try:
        target = Dialect.get_or_raise(dialect)
        tree = sqlglot.parse_one(sql, read=codegen.DEFAULT_DIALECT)
        for select in list(tree.find_all(exp.Select)):  # outermost first
            _collapse_star(select, symbols)
        for node in list(tree.find_all(exp.Where, exp.Having)):
            _strip_condition_parens(node)
        for ident in tree.find_all(exp.Identifier):
            ident.set("quoted", _needs_quotes(ident.name, target))
        text = tree.sql(dialect=dialect, pretty=True)
        sqlglot.parse_one(text, read=dialect)  # must still parse, or show the full form
        return text
    except Exception:
        return full


@functools.lru_cache(maxsize=None)
def _reserved_words(dialect: Dialect) -> frozenset[str]:
    """Keywords the parser would not read as a plain name (``ORDER BY`` -> order)."""
    plain_ok = dialect.parser_class.ID_VAR_TOKENS
    words = set()
    for text, token in dialect.tokenizer_class.KEYWORDS.items():
        parts = text.split()
        if len(parts) > 1 or token not in plain_ok:
            words.add(parts[0].lower())
    return frozenset(words)


def _needs_quotes(name: str, dialect: Dialect) -> bool:
    if not _PLAIN_NAME.fullmatch(name):  # spaces, symbols, upper or mixed case
        return True
    return name in _reserved_words(dialect)


def _source_columns(source: exp.Expression, symbols: SymbolTable) -> list[str] | None:
    if isinstance(source, exp.Table):
        table = symbols.table(source.name)
        return table.column_names() if table else None
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
        return list(source.this.named_selects)
    return None


def _collapse_star(select: exp.Select, symbols: SymbolTable) -> None:
    """``SELECT t.a, t.b, ...`` listing every column of t in order -> ``*`` / ``t.*``."""
    projections = select.expressions
    if not projections or not all(type(p) is exp.Column for p in projections):
        return
    qualifiers = {p.table for p in projections}
    if len(qualifiers) != 1:
        return
    qualifier = qualifiers.pop()

    from_ = select.args.get("from") or select.args.get("from_")
    if from_ is None:
        return
    sources = [from_.this] + [j.this for j in select.args.get("joins") or []]
    if qualifier:
        matches = [s for s in sources if s.alias_or_name == qualifier]
    else:
        matches = sources if len(sources) == 1 else []
    if len(matches) != 1:
        return
    columns = _source_columns(matches[0], symbols)
    names = [p.name for p in projections]
    if not columns or [c.lower() for c in columns] != [n.lower() for n in names]:
        return

    if len(sources) == 1:
        select.set("expressions", [exp.Star()])
    else:
        select.set("expressions",
                   [exp.Column(this=exp.Star(), table=exp.to_identifier(qualifier))])


def _strip_condition_parens(node: exp.Expression) -> None:
    """``WHERE (a > 1)`` -> ``WHERE a > 1``; ``(a) AND (b)`` -> ``a AND b``."""
    condition = node.this
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        parts = []
        for part in condition.flatten():
            inner = part
            while isinstance(inner, exp.Paren):
                inner = inner.this
            # OR binds looser than AND, so its parentheses must stay.
            parts.append(part if isinstance(inner, exp.Or) else inner)
        condition = exp.and_(*parts, copy=False)
    node.set("this", condition)
