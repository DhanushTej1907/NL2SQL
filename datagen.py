"""Schema-driven data generator.

Scales every table of any SQLite database to N rows while respecting
primary keys, foreign keys, NOT NULL and UNIQUE. Values are driven by the
data already present, never by column names, so the generator is as
schema-agnostic as the compiler.

    python datagen.py in.db out.db --rows 100000 [--seed 0]
"""

from __future__ import annotations

import argparse
import random
import shutil
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nl2sql.schema import Column, SymbolTable, Table, introspect, quote_ident

BATCH = 10_000
TEXT_POOL_LIMIT = 500


class GenerationError(Exception):
    pass


@dataclass
class ColumnStats:
    minimum: Any = None
    maximum: Any = None
    is_integer: bool = True
    values: list[Any] = None
    distinct: int = 0

    def __post_init__(self):
        if self.values is None:
            self.values = []


def topological_order(symbols: SymbolTable) -> tuple[list[str], list[str]]:
    """Order tables parents-first. Cycles are reported, not followed."""
    pending = {name: set() for name in symbols.tables}
    for table in symbols.tables.values():
        for fk in table.foreign_keys:
            if fk.to_table != table.name and fk.to_table in pending:
                pending[table.name].add(fk.to_table)

    ordered: list[str] = []
    queue = deque(sorted(n for n, deps in pending.items() if not deps))
    done: set[str] = set()
    while queue:
        name = queue.popleft()
        if name in done:
            continue
        ordered.append(name)
        done.add(name)
        for other, deps in pending.items():
            if other in done:
                continue
            deps.discard(name)
            if not deps and other not in queue:
                queue.append(other)
    cycles = [n for n in symbols.tables if n not in done]
    return ordered + cycles, cycles


def collect_stats(conn: sqlite3.Connection, table: Table) -> dict[str, ColumnStats]:
    """Learn each column's shape from the rows already there."""
    stats: dict[str, ColumnStats] = {}
    for col in table.columns.values():
        entry = ColumnStats()
        quoted = quote_ident(col.name)
        tname = quote_ident(table.name)
        if col.type_class == "numeric":
            row = conn.execute(
                f"SELECT MIN({quoted}), MAX({quoted}), "
                f"MAX(CAST({quoted} AS TEXT) LIKE '%.%') FROM {tname}"
            ).fetchone()
            entry.minimum, entry.maximum = row[0], row[1]
            entry.is_integer = not bool(row[2])
        else:
            rows = conn.execute(
                f"SELECT DISTINCT {quoted} FROM {tname} "
                f"WHERE {quoted} IS NOT NULL LIMIT {TEXT_POOL_LIMIT}"
            ).fetchall()
            entry.values = [r[0] for r in rows]
            entry.distinct = len(entry.values)
            if col.type_class == "date" and entry.values:
                entry.minimum = min(entry.values)
                entry.maximum = max(entry.values)
        stats[col.name] = entry
    return stats


class Generator:
    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)
        try:
            from faker import Faker

            self.faker = Faker()
            self.faker.seed_instance(seed)
        except Exception:
            self.faker = None

    # -- values ----------------------------------------------------------
    def numeric(self, col: Column, stats: ColumnStats, serial: int) -> Any:
        low, high = stats.minimum, stats.maximum
        if low is None or high is None:
            return serial
        if stats.is_integer:
            low, high = int(low), int(high)
            if low == high:
                high = low + 1
            return self.rng.randint(low, high)
        return round(self.rng.uniform(float(low), float(high)), 4)

    def text(self, col: Column, stats: ColumnStats, serial: int, unique: bool) -> Any:
        if stats.values and not unique and stats.distinct <= 100:
            return self.rng.choice(stats.values)
        if stats.values and not unique:
            base = self.rng.choice(stats.values)
            return base
        word = ""
        if self.faker is not None:
            word = self.faker.word() if stats.distinct <= 50 else self.faker.name()
        else:
            word = "value" + str(self.rng.randint(1, 9999))
        return f"{word} {serial}" if unique else word

    def date(self, stats: ColumnStats, serial: int) -> Any:
        if stats.values:
            return self.rng.choice(stats.values)
        return f"20{self.rng.randint(10, 29)}-{self.rng.randint(1, 12):02d}-" \
               f"{self.rng.randint(1, 28):02d}"

    def value(self, col: Column, stats: ColumnStats, serial: int,
              unique: bool) -> Any:
        if col.type_class == "numeric":
            return self.numeric(col, stats, serial)
        if col.type_class == "date":
            return self.date(stats, serial)
        if col.type_class == "text":
            return self.text(col, stats, serial, unique)
        return serial


def _fk_map(table: Table) -> dict[str, tuple[str, str]]:
    """column -> (parent table, parent column), for single-column FKs."""
    out: dict[str, tuple[str, str]] = {}
    for fk in table.foreign_keys:
        for a, b in zip(fk.from_columns, fk.to_columns):
            out[a] = (fk.to_table, b)
    return out


def generate(source: Path, target: Path, rows: int, seed: int = 0,
             verbose: bool = True) -> dict[str, int]:
    """Copy the schema of ``source`` and fill it with ``rows`` rows per table."""
    source, target = Path(source), Path(target)
    if target.exists():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)

    symbols = introspect(source)
    if not symbols.tables:
        raise GenerationError(f"{source} has no tables")

    src = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    ddl = [
        r[0]
        for r in src.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]

    out = sqlite3.connect(str(target))
    out.execute("PRAGMA foreign_keys=OFF")
    out.execute("PRAGMA synchronous=OFF")
    out.execute("PRAGMA journal_mode=OFF")
    for statement in ddl:
        out.execute(statement)
    out.commit()

    gen = Generator(seed)
    order, cycles = topological_order(symbols)
    if cycles and verbose:
        print(f"note: foreign-key cycle among {cycles}; "
              f"nullable keys there are left NULL")

    produced: dict[str, int] = {}
    parent_keys: dict[tuple[str, str], list[Any]] = {}

    try:
        for name in order:
            table = symbols.tables[name]
            stats = collect_stats(src, table)
            count = _fill_table(out, table, stats, gen, rows, parent_keys, cycles)
            produced[name] = count
            if verbose:
                print(f"  {name}: {count:,} rows")
        out.commit()
    finally:
        src.close()
        out.close()
    return produced


def _fill_table(conn: sqlite3.Connection, table: Table,
                stats: dict[str, ColumnStats], gen: Generator, rows: int,
                parent_keys: dict[tuple[str, str], list[Any]],
                cycles: Sequence[str]) -> int:
    columns = list(table.columns.values())
    names = [c.name for c in columns]
    pk = list(table.primary_key)
    fks = _fk_map(table)
    rng = gen.rng

    # How many rows can we actually make? A composite key drawn from parent
    # key spaces cannot exceed the product of those spaces.
    target_rows = rows
    composite_pk = len(pk) > 1
    if composite_pk:
        space = 1
        for key in pk:
            parent = fks.get(key)
            pool = parent_keys.get(parent, []) if parent else []
            space *= max(len(pool), 1)
        if space and space < target_rows:
            target_rows = space

    seen_pk: set[tuple] = set()
    unique_seen: dict[str, set] = {
        c.name: set() for c in columns if c.unique and not c.is_pk
    }

    single_int_pk = (
        len(pk) == 1
        and pk[0] in table.columns
        and table.columns[pk[0]].type_class == "numeric"
    )

    placeholders = ", ".join("?" for _ in names)
    insert = (
        f"INSERT INTO {quote_ident(table.name)} "
        f"({', '.join(quote_ident(n) for n in names)}) VALUES ({placeholders})"
    )

    batch: list[tuple] = []
    made = 0
    attempts = 0
    max_attempts = target_rows * 10 + 1000

    while made < target_rows and attempts < max_attempts:
        attempts += 1
        row: list[Any] = []
        serial = made + 1
        for col in columns:
            parent = fks.get(col.name)
            if parent is not None and parent in parent_keys and parent_keys[parent]:
                pool = parent_keys[parent]
                if col.nullable and col.name not in pk and rng.random() < 0.05:
                    row.append(None)
                else:
                    row.append(rng.choice(pool))
                continue
            if parent is not None and table.name in cycles and col.nullable:
                row.append(None)
                continue
            if single_int_pk and col.name == pk[0]:
                row.append(serial)
                continue
            unique = col.name in unique_seen or (col.is_pk and not single_int_pk)
            value = gen.value(col, stats[col.name], serial, unique)
            if col.name in unique_seen:
                guard = 0
                while value in unique_seen[col.name] and guard < 50:
                    guard += 1
                    value = gen.value(col, stats[col.name], serial * 1000 + guard, True)
                unique_seen[col.name].add(value)
            row.append(value)

        if pk:
            key = tuple(row[names.index(c)] for c in pk)
            if any(v is None for v in key) or key in seen_pk:
                continue
            seen_pk.add(key)

        batch.append(tuple(row))
        made += 1
        if len(batch) >= BATCH:
            conn.executemany(insert, batch)
            batch.clear()

    if batch:
        conn.executemany(insert, batch)
    conn.commit()

    for key in pk or names[:1]:
        values = [r[0] for r in conn.execute(
            f"SELECT {quote_ident(key)} FROM {quote_ident(table.name)}"
        ).fetchall()]
        parent_keys[(table.name, key)] = values
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="the database to copy the schema from")
    ap.add_argument("target", help="the database to write")
    ap.add_argument("--rows", type=int, default=100_000,
                    help="rows per table (default 100000)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    start = time.perf_counter()
    counts = generate(Path(args.source), Path(args.target), args.rows,
                      args.seed, verbose=not args.quiet)
    elapsed = time.perf_counter() - start
    total = sum(counts.values())
    if not args.quiet:
        print(f"wrote {total:,} rows across {len(counts)} tables "
              f"in {elapsed:.1f}s -> {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
