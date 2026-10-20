"""Run two queries and compare their results.

Rows are compared as a multiset, unless the statement is ordered, in which
case they are compared as a list. Codegen appends a primary-key tiebreaker
to ordered queries, so the list comparison is deterministic.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from collections import Counter
from typing import Any, Sequence

import sqlglot
from sqlglot import exp

ROUND_DIGITS = 9


@dataclass
class EquivalenceResult:
    passed: bool
    mode: str                       # multiset | ordered | n/a
    n_rows_original: int = 0
    n_rows_optimized: int = 0
    first_difference: str = ""
    error: str = ""

    def badge(self) -> str:
        if self.mode == "n/a":
            return f"n/a - {self.first_difference or self.error or 'not compared'}"
        return "equivalent" if self.passed else "NOT equivalent"

    def render(self) -> str:
        if self.error:
            return f"equivalence check failed to run: {self.error}"
        if self.mode == "n/a":
            return self.badge()
        detail = (
            f"{self.n_rows_original} vs {self.n_rows_optimized} rows, "
            f"{self.mode} comparison"
        )
        if self.passed:
            return f"equivalent ({detail})"
        return f"NOT equivalent ({detail}): {self.first_difference}"


def _is_ordered(sql: str) -> bool:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return " order by " in sql.lower()
    return tree.find(exp.Order) is not None


def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if value == 0:
            return 0.0
        digits = ROUND_DIGITS - 1 - int(math.floor(math.log10(abs(value))))
        return round(value, max(digits, 0))
    if isinstance(value, bytes):
        return value.hex()
    return value


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    cur = conn.execute(sql)
    return [tuple(_normalize(v) for v in row) for row in cur.fetchall()]


def run_query(conn: sqlite3.Connection, sql: str,
              limit: int | None = None) -> tuple[list[str], list[tuple]]:
    """Execute a query, returning its column names and rows."""
    cur = conn.execute(sql)
    columns = [d[0] for d in (cur.description or [])]
    rows = cur.fetchall() if limit is None else cur.fetchmany(limit)
    return columns, [tuple(r) for r in rows]


def check(conn: sqlite3.Connection, original_sql: str,
          optimized_sql: str) -> EquivalenceResult:
    """Compare the results of two queries on the same connection."""
    if original_sql.strip() == optimized_sql.strip():
        return EquivalenceResult(
            True, "multiset", first_difference="", error="",
        )
    try:
        left = _rows(conn, original_sql)
        right = _rows(conn, optimized_sql)
    except sqlite3.Error as exc:
        return EquivalenceResult(False, "n/a", error=str(exc))

    ordered = _is_ordered(original_sql) and _is_ordered(optimized_sql)
    mode = "ordered" if ordered else "multiset"

    if ordered:
        passed = left == right
        difference = ""
        if not passed:
            for i, (a, b) in enumerate(zip(left, right)):
                if a != b:
                    difference = f"row {i}: {a!r} vs {b!r}"
                    break
            else:
                difference = f"row counts differ: {len(left)} vs {len(right)}"
    else:
        counter_left = Counter(left)
        counter_right = Counter(right)
        passed = counter_left == counter_right
        difference = ""
        if not passed:
            missing = counter_left - counter_right
            extra = counter_right - counter_left
            bits = []
            if missing:
                bits.append(f"only in the original: {list(missing)[:3]}")
            if extra:
                bits.append(f"only in the optimized: {list(extra)[:3]}")
            difference = "; ".join(bits)

    return EquivalenceResult(
        passed=passed,
        mode=mode,
        n_rows_original=len(left),
        n_rows_optimized=len(right),
        first_difference=difference,
    )


def not_applicable(reason: str) -> EquivalenceResult:
    return EquivalenceResult(True, "n/a", first_difference=reason)
