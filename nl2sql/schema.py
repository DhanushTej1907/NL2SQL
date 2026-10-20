"""Database loading and introspection.

Produces a :class:`SymbolTable` describing whatever database is handed to it.
Nothing here knows any particular table or column name; a database holding
``widget(zorb_id, ...)`` is treated exactly like any other.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import sqlglot
from sqlglot import exp

# Columns are classified by SQLite's type-affinity rules.
_NUMERIC_HINTS = ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC", "MONEY")
_TEXT_HINTS = ("CHAR", "CLOB", "TEXT", "STRING", "ENUM", "SET")
_DATE_HINTS = ("DATE", "TIME", "YEAR")

MAX_SAMPLES = 50

# Statement-level noise commonly found in engine-specific dumps.
_DUMP_NOISE = (
    re.compile(r"/\*!.*?\*/;?", re.S),
    re.compile(r"^\s*SET\s+[^;]*;", re.I | re.M),
    re.compile(r"^\s*(UN)?LOCK\s+TABLES[^;]*;", re.I | re.M),
    re.compile(r"^\s*DELIMITER\b.*$", re.I | re.M),
    re.compile(r"^\s*CREATE\s+DATABASE\b[^;]*;", re.I | re.M),
    re.compile(r"^\s*USE\s+[^;]*;", re.I | re.M),
    re.compile(r"^\s*--.*$", re.M),
)


def classify_type(raw: str | None) -> str:
    """Map a declared SQL type onto a coarse class used by the type checker."""
    if not raw:
        return "other"
    up = str(raw).upper()
    for hint in _DATE_HINTS:
        if hint in up:
            return "date"
    for hint in _NUMERIC_HINTS:
        if hint in up:
            return "numeric"
    for hint in _TEXT_HINTS:
        if hint in up:
            return "text"
    return "other"


@dataclass
class Column:
    table: str
    name: str
    type_raw: str
    type_class: str
    nullable: bool
    pk_pos: int = 0            # 1-based position in the PK, 0 if not part of it
    unique: bool = False       # single-column UNIQUE constraint or index
    rowid_alias: bool = False  # INTEGER PRIMARY KEY, so it can never be NULL
    samples: list[Any] = field(default_factory=list)

    @property
    def qname(self) -> str:
        return f"{self.table}.{self.name}"

    @property
    def is_pk(self) -> bool:
        return self.pk_pos > 0

    def never_null(self) -> bool:
        """True when SQLite guarantees this column cannot hold NULL."""
        return (not self.nullable) or self.rowid_alias


@dataclass
class ForeignKey:
    """A join edge, with the provenance the optimizer preconditions rely on."""

    from_table: str
    from_columns: list[str]
    to_table: str
    to_columns: list[str]
    status: str = "declared"  # declared | declared_violated | inferred

    @property
    def trusted(self) -> bool:
        return self.status == "declared"

    def describe(self) -> str:
        left = ", ".join(f"{self.from_table}.{c}" for c in self.from_columns)
        right = ", ".join(f"{self.to_table}.{c}" for c in self.to_columns)
        return f"{left} -> {right} ({self.status})"


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    row_count: int = 0

    def column_names(self) -> list[str]:
        return list(self.columns)

    def pk_columns(self) -> list[Column]:
        return [self.columns[c] for c in self.primary_key if c in self.columns]

    def has_trusted_pk(self) -> bool:
        """A declared PK whose columns can genuinely not be NULL."""
        cols = self.pk_columns()
        return bool(cols) and all(c.never_null() for c in cols)

    def label_column(self) -> Column | None:
        """Pick a human-readable column for grouped output, by shape only.

        The choice uses type and sampled distinctness, never the column's
        name, so it behaves the same on a schema full of ``zorb`` tables.
        """
        best, best_score = None, -1.0
        for col in self.columns.values():
            if col.is_pk or col.type_class != "text":
                continue
            score = len(set(map(str, col.samples))) / max(len(col.samples), 1)
            if col.samples and score > best_score:
                best, best_score = col, score
        return best


@dataclass
class SymbolTable:
    tables: dict[str, Table] = field(default_factory=dict)
    schema_hash: str = ""

    def table(self, name: str) -> Table | None:
        if name in self.tables:
            return self.tables[name]
        for key, tbl in self.tables.items():
            if key.lower() == name.lower():
                return tbl
        return None

    def all_columns(self) -> Iterable[Column]:
        for tbl in self.tables.values():
            yield from tbl.columns.values()

    def find_columns(self, name: str) -> list[Column]:
        """Every column with this name, across tables (ambiguity is kept)."""
        low = name.lower()
        return [c for c in self.all_columns() if c.name.lower() == low]

    def edges(self) -> list[ForeignKey]:
        out: list[ForeignKey] = []
        for tbl in self.tables.values():
            out.extend(tbl.foreign_keys)
        return out

    def compact_description(self, max_samples: int = 5) -> str:
        """A compact schema rendering, used in the LLM prompt and the UI."""
        lines: list[str] = []
        for tbl in self.tables.values():
            lines.append(f"TABLE {tbl.name} ({tbl.row_count} rows)")
            for col in tbl.columns.values():
                bits = [col.type_raw or col.type_class]
                if col.is_pk:
                    bits.append("PRIMARY KEY")
                if not col.nullable:
                    bits.append("NOT NULL")
                if col.unique:
                    bits.append("UNIQUE")
                samples = [str(s) for s in col.samples[:max_samples]]
                tail = f"  e.g. {', '.join(samples)}" if samples else ""
                lines.append(f"  {col.name}: {' '.join(bits)}{tail}")
            for fk in tbl.foreign_keys:
                if fk.status != "inferred":
                    lines.append(f"  FOREIGN KEY {fk.describe()}")
        return "\n".join(lines)


def _canonical_schema_json(tables: dict[str, Table]) -> str:
    payload = []
    for name in sorted(tables):
        tbl = tables[name]
        payload.append(
            {
                "table": name,
                "columns": [
                    {
                        "name": c.name,
                        "type": (c.type_raw or "").upper(),
                        "nullable": c.nullable,
                        "pk": c.pk_pos,
                    }
                    for c in tbl.columns.values()
                ],
                "pk": tbl.primary_key,
                "fks": [
                    {
                        "cols": fk.from_columns,
                        "to": fk.to_table,
                        "to_cols": fk.to_columns,
                    }
                    for fk in tbl.foreign_keys
                    if fk.status != "inferred"
                ],
            }
        )
    return json.dumps(payload, sort_keys=True)


def _install_timeout(conn: sqlite3.Connection, seconds: float) -> None:
    import time

    deadline = time.perf_counter() + seconds

    def guard() -> int:
        return 1 if time.perf_counter() > deadline else 0

    conn.set_progress_handler(guard, 10_000)


class Database:
    """A loaded database: a file on disk plus its :class:`SymbolTable`."""

    def __init__(self, path: Path, symbols: SymbolTable, origin: Path | None = None,
                 diagnostics: Sequence[str] = ()):
        self.path = Path(path)
        self.symbols = symbols
        self.origin = Path(origin) if origin else self.path
        self.load_diagnostics = list(diagnostics)
        self._temp_dir: str | None = None

    def connect(self, timeout_seconds: float = 15.0) -> sqlite3.Connection:
        """A read-only connection with a wall-clock guard on long queries."""
        uri = "file:" + self.path.resolve().as_posix().replace("?", "%3f")
        conn = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        _install_timeout(conn, timeout_seconds)
        return conn

    @property
    def name(self) -> str:
        return self.origin.name

    def cleanup(self) -> None:
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None


def quote_ident(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


_quote = quote_ident


def introspect(path: Path) -> SymbolTable:
    """Read a SQLite file into a SymbolTable (SQLAlchemy + PRAGMA cross-check)."""
    from sqlalchemy import create_engine, inspect as sa_inspect

    engine = create_engine(f"sqlite:///{Path(path).resolve().as_posix()}")
    tables: dict[str, Table] = {}
    try:
        insp = sa_inspect(engine)
        names = [n for n in insp.get_table_names() if not n.startswith("sqlite_")]
        for tname in names:
            tbl = Table(name=tname)
            pk = insp.get_pk_constraint(tname) or {}
            pk_cols = list(pk.get("constrained_columns") or [])
            uniques: set[str] = set()
            for uc in insp.get_unique_constraints(tname) or []:
                cols = uc.get("column_names") or []
                if len(cols) == 1:
                    uniques.add(cols[0])
            for idx in insp.get_indexes(tname) or []:
                cols = [c for c in (idx.get("column_names") or []) if c]
                if idx.get("unique") and len(cols) == 1:
                    uniques.add(cols[0])
            for col in insp.get_columns(tname):
                raw = str(col.get("type") or "")
                cname = col["name"]
                tbl.columns[cname] = Column(
                    table=tname,
                    name=cname,
                    type_raw=raw,
                    type_class=classify_type(raw),
                    nullable=bool(col.get("nullable", True)),
                    pk_pos=(pk_cols.index(cname) + 1) if cname in pk_cols else 0,
                    unique=cname in uniques,
                )
            tbl.primary_key = pk_cols
            for fk in insp.get_foreign_keys(tname) or []:
                referred = fk.get("referred_table")
                cons = list(fk.get("constrained_columns") or [])
                refs = list(fk.get("referred_columns") or [])
                if not referred or not cons:
                    continue
                tbl.foreign_keys.append(
                    ForeignKey(tname, cons, referred, refs or cons, "declared")
                )
            tables[tname] = tbl
    finally:
        engine.dispose()

    _pragma_crosscheck(path, tables)

    symbols = SymbolTable(tables=tables)
    symbols.schema_hash = hashlib.sha256(
        _canonical_schema_json(tables).encode("utf-8")
    ).hexdigest()
    return symbols


def _pragma_crosscheck(path: Path, tables: dict[str, Table]) -> None:
    """Fill in rowid aliases, row counts, samples, and demote violated FKs."""
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for tname, tbl in tables.items():
            quoted = _quote(tname)
            try:
                info = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            except sqlite3.Error:
                continue
            pk_rows = [r for r in info if r["pk"]]
            # A single INTEGER PRIMARY KEY is the rowid alias: never NULL.
            if len(pk_rows) == 1 and str(pk_rows[0]["type"]).upper() == "INTEGER":
                col = tbl.columns.get(pk_rows[0]["name"])
                if col is not None:
                    col.rowid_alias = True
            for row in info:
                col = tbl.columns.get(row["name"])
                if col is None:
                    continue
                if row["notnull"]:
                    col.nullable = False
                if row["pk"] and col.pk_pos == 0:
                    col.pk_pos = int(row["pk"])
            if not tbl.primary_key:
                tbl.primary_key = [
                    r["name"] for r in sorted(pk_rows, key=lambda r: r["pk"])
                ]
            try:
                tbl.row_count = conn.execute(
                    f"SELECT COUNT(*) FROM {quoted}"
                ).fetchone()[0]
            except sqlite3.Error:
                tbl.row_count = 0
            _sample_values(conn, tbl)

        _check_fk_integrity(conn, tables)
    finally:
        conn.close()


def _sample_values(conn: sqlite3.Connection, tbl: Table) -> None:
    for col in tbl.columns.values():
        if col.type_class not in ("text", "date"):
            continue
        try:
            rows = conn.execute(
                f"SELECT DISTINCT {_quote(col.name)} FROM {_quote(tbl.name)} "
                f"WHERE {_quote(col.name)} IS NOT NULL LIMIT {MAX_SAMPLES}"
            ).fetchall()
            col.samples = [r[0] for r in rows]
        except sqlite3.Error:
            col.samples = []


def _check_fk_integrity(conn: sqlite3.Connection, tables: dict[str, Table]) -> None:
    """SQLite does not enforce FKs by default; demote any the data breaks."""
    violated: set[tuple[str, int]] = set()
    for tname in tables:
        try:
            rows = conn.execute(
                f"PRAGMA foreign_key_check({_quote(tname)})"
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            violated.add((tname, int(row[3])))
    for tname, tbl in tables.items():
        if not any(t == tname for t, _ in violated):
            continue
        try:
            listing = conn.execute(
                f"PRAGMA foreign_key_list({_quote(tname)})"
            ).fetchall()
        except sqlite3.Error:
            continue
        bad_targets = set()
        for row in listing:
            if (tname, int(row["id"])) in violated:
                bad_targets.add((row["table"], row["from"]))
        for fk in tbl.foreign_keys:
            for target, from_col in bad_targets:
                if fk.to_table == target and from_col in fk.from_columns:
                    fk.status = "declared_violated"


def _detect_dialect(text: str) -> str:
    head = text[:20000].upper()
    if "`" in text[:20000] or "ENGINE=" in head or "AUTO_INCREMENT" in head:
        return "mysql"
    return "sqlite"


def _strip_table_options(stmt: exp.Expression) -> exp.Expression:
    """Remove engine, charset and collation clutter SQLite cannot accept."""
    drop_types = tuple(
        cls
        for cls in (
            getattr(exp, name, None)
            for name in (
                "EngineProperty",
                "CharacterSetProperty",
                "CollateProperty",
                "AutoIncrementProperty",
                "ChecksumProperty",
                "RowFormatProperty",
            )
        )
        if cls is not None
    )
    for prop_node in list(stmt.find_all(exp.Properties)):
        if drop_types:
            prop_node.set(
                "expressions",
                [p for p in prop_node.expressions if not isinstance(p, drop_types)],
            )
    return stmt


def _clean_column_defs(stmt: exp.Expression) -> None:
    """Drop per-column clauses SQLite rejects, and normalise integer widths.

    ``AUTO_INCREMENT`` transpiles to ``AUTOINCREMENT`` in front of
    ``PRIMARY KEY``, which SQLite will not parse; it carries no meaning for
    reading the data, so it goes. ``INTEGER(11)`` is dropped to ``INTEGER``
    so that a single integer primary key is still a rowid alias.
    """
    unwanted = tuple(
        cls
        for cls in (
            getattr(exp, name, None)
            for name in (
                "AutoIncrementColumnConstraint",
                "CollateColumnConstraint",
                "CharacterSetColumnConstraint",
                "OnUpdateColumnConstraint",
                "CommentColumnConstraint",
            )
        )
        if cls is not None
    )
    integer_types = {exp.DataType.Type.INT, exp.DataType.Type.BIGINT,
                     exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT}
    for column in list(stmt.find_all(exp.ColumnDef)):
        if unwanted:
            column.set(
                "constraints",
                [c for c in column.constraints if not isinstance(c.kind, unwanted)],
            )
        kind = column.args.get("kind")
        if isinstance(kind, exp.DataType) and kind.this in integer_types:
            kind.set("expressions", [])


def _extract_indexes(stmt: exp.Expression) -> list[str]:
    """Move table-level ``KEY``/``UNIQUE KEY`` clauses out of the CREATE TABLE.

    SQLite has no inline index syntax, so secondary indexes become separate
    ``CREATE INDEX`` statements and a named ``UNIQUE KEY`` loses its name.
    """
    schema = stmt.this
    if not isinstance(schema, exp.Schema):
        return []
    table_name = schema.this.name if isinstance(schema.this, exp.Table) else "t"
    index_cls = getattr(exp, "IndexColumnConstraint", None)
    unique_cls = getattr(exp, "UniqueColumnConstraint", None)

    keep: list[exp.Expression] = []
    extra: list[str] = []
    for item in schema.expressions:
        if index_cls is not None and isinstance(item, index_cls):
            name = item.this.name if item.this is not None else ""
            columns = [c.this.name if isinstance(c, exp.Ordered) else c.name
                       for c in (item.expressions or [])]
            columns = [c for c in columns if c]
            if name and columns:
                cols = ", ".join(quote_ident(c) for c in columns)
                extra.append(
                    f"CREATE INDEX IF NOT EXISTS {quote_ident(name)} "
                    f"ON {quote_ident(table_name)} ({cols})"
                )
            continue
        if unique_cls is not None and isinstance(item, unique_cls):
            inner = item.this
            if isinstance(inner, exp.Schema) and inner.this is not None:
                # a named UNIQUE KEY: keep the columns, drop the name
                item = unique_cls(this=exp.Schema(expressions=inner.expressions))
        keep.append(item)
    schema.set("expressions", keep)
    return extra


def load_sql_dump(path: Path, target_dir: Path | None = None) -> tuple[Path, list[str]]:
    """Transpile an engine-specific ``.sql`` dump into a temporary SQLite file."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    dialect = _detect_dialect(text)
    for pattern in _DUMP_NOISE:
        text = pattern.sub("", text)

    diagnostics: list[str] = []
    target_dir = Path(target_dir or tempfile.mkdtemp(prefix="nl2sql_dump_"))
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / (Path(path).stem + ".db")
    if out_path.exists():
        out_path.unlink()

    try:
        statements = sqlglot.parse(text, read=dialect)
    except Exception as exc:
        diagnostics.append(f"could not parse dump as {dialect}: {exc}")
        statements = []

    skip_types = tuple(
        c for c in (getattr(exp, n, None) for n in ("Drop", "Set", "Use", "Command"))
        if c is not None
    )

    conn = sqlite3.connect(str(out_path))
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            for stmt in statements:
                if stmt is None or isinstance(stmt, skip_types):
                    continue
                extra: list[str] = []
                try:
                    cleaned = _strip_table_options(stmt.copy())
                    _clean_column_defs(cleaned)
                    if isinstance(cleaned, exp.Create):
                        extra = _extract_indexes(cleaned)
                    sql = cleaned.sql(dialect="sqlite")
                except Exception as exc:
                    diagnostics.append(f"transpile failed: {exc}")
                    continue
                if not sql.strip():
                    continue
                for statement_sql in [sql, *extra]:
                    try:
                        conn.execute(statement_sql)
                    except sqlite3.Error as exc:
                        diagnostics.append(
                            f"statement skipped ({exc}): {statement_sql[:120]}"
                        )
        conn.commit()
    finally:
        conn.close()
    return out_path, diagnostics


def load_database(path: str | Path) -> Database:
    """Load a ``.db``/``.sqlite`` file or a ``.sql`` dump into a Database."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"no such database: {src}")
    diagnostics: list[str] = []
    temp_dir: str | None = None
    if src.suffix.lower() == ".sql":
        temp_dir = tempfile.mkdtemp(prefix="nl2sql_dump_")
        real_path, diagnostics = load_sql_dump(src, Path(temp_dir))
    else:
        real_path = src
    symbols = introspect(real_path)
    infer_missing_edges(symbols)
    db = Database(real_path, symbols, origin=src, diagnostics=diagnostics)
    db._temp_dir = temp_dir
    return db


def singularize(word: str) -> str:
    low = word.lower()
    for suffix, repl in (("ies", "y"), ("ses", "s"), ("xes", "x"), ("hes", "h")):
        if low.endswith(suffix) and len(low) > len(suffix) + 1:
            return low[: -len(suffix)] + repl
    if low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def pluralize_forms(stem: str) -> set[str]:
    stem = stem.lower()
    forms = {stem, stem + "s", stem + "es"}
    if stem.endswith("y"):
        forms.add(stem[:-1] + "ies")
    return forms


def infer_missing_edges(symbols: SymbolTable) -> None:
    """Add ``inferred`` edges for ``<stem>_id`` columns that declare no FK.

    The convention (a key column naming its parent) is part of the
    specification, so the literal ``id`` is the only name this module knows.
    """
    by_stem: dict[str, list[Table]] = {}
    for tbl in symbols.tables.values():
        for form in pluralize_forms(singularize(tbl.name)):
            by_stem.setdefault(form, []).append(tbl)
        by_stem.setdefault(tbl.name.lower(), []).append(tbl)

    for tbl in symbols.tables.values():
        declared_targets = {fk.to_table for fk in tbl.foreign_keys}
        for col in tbl.columns.values():
            stem = fk_stem(col.name)
            if not stem:
                continue
            candidates = (
                by_stem.get(stem.lower())
                or by_stem.get(singularize(stem))
                or []
            )
            targets = {t.name: t for t in candidates if t.name != tbl.name}
            if len(targets) != 1:
                continue
            target = next(iter(targets.values()))
            if target.name in declared_targets:
                continue
            to_col = _infer_target_column(target, col.name)
            if to_col is None:
                continue
            tbl.foreign_keys.append(
                ForeignKey(tbl.name, [col.name], target.name, [to_col], "inferred")
            )
            declared_targets.add(target.name)


def fk_stem(column_name: str) -> str | None:
    """``zorbId`` / ``zorb_id`` -> ``zorb``; a bare key column has no stem."""
    low = column_name.lower()
    if low.endswith("_id") and len(low) > 3:
        return column_name[:-3]
    if low.endswith("id") and len(low) > 2:
        core = column_name[:-2]
        if core and core[-1] != "_":
            return core
    return None


def _infer_target_column(target: Table, from_col: str) -> str | None:
    if target.primary_key:
        return target.primary_key[0]
    for candidate in (from_col, "id", f"{singularize(target.name)}_id"):
        for col in target.columns.values():
            if col.name.lower() == candidate.lower():
                return col.name
    return None
